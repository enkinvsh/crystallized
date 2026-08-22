"""Bilingual (RU/EN) friction pattern table shared by the injector and the observer.

This module is the single source of truth for "the user pushed back" detection.
It is deliberately dependency-free, pure, and deterministic: `observer.py` runs
it inside a <150 ms hook budget on every PostToolUse event, so nothing here may
touch the filesystem, the network, or the clock.

Detected categories
-------------------
``hard_rejection``       "нет", "не то", "no", "wrong", "that's not right"
``undo_request``         "верни как было", "откати", "undo that", "revert"
``negative_constraint``  "не трогай", "не используй", "don't touch", "never use"
``positive_requirement`` "всегда делай", "только через", "always use", "strictly keep"
``frustration``          "я же просил", "опять ты", "i already told you", "how many times"
``ephemeral``            "пропусти сейчас", "skip for now", "just for this test"

`ephemeral` is a *suppressor*, not a signal: when an ephemeral marker is present
the utterance describes a one-off exception, not a durable rule, so
`detect_friction()` returns ``None``. That guard is what keeps "skip the tests
for now" from crystallizing into a permanent "never run tests" belief.

Performance notes
-----------------
* Every pattern is a flat alternation — no nested quantifiers, so no
  catastrophic backtracking on adversarial tool output.
* Input is truncated to `MAX_SCAN_CHARS` before scanning.
* Regexes are compiled once at import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "EPHEMERAL",
    "FRUSTRATION",
    "HARD_REJECTION",
    "MAX_SCAN_CHARS",
    "NEGATIVE_CONSTRAINT",
    "PATTERNS",
    "PATTERN_TYPES",
    "POSITIVE_REQUIREMENT",
    "Pattern",
    "UNDO_REQUEST",
    "detect_all",
    "detect_friction",
    "is_ephemeral",
    "normalize",
]

# ---------------------------------------------------------------------------
# Pattern type constants
# ---------------------------------------------------------------------------

HARD_REJECTION: Final = "hard_rejection"
UNDO_REQUEST: Final = "undo_request"
NEGATIVE_CONSTRAINT: Final = "negative_constraint"
POSITIVE_REQUIREMENT: Final = "positive_requirement"
FRUSTRATION: Final = "frustration"
EPHEMERAL: Final = "ephemeral"

PATTERN_TYPES: Final[tuple[str, ...]] = (
    HARD_REJECTION,
    UNDO_REQUEST,
    NEGATIVE_CONSTRAINT,
    POSITIVE_REQUIREMENT,
    FRUSTRATION,
)

#: Longest slice of text we are willing to scan. Tool outputs can be megabytes;
#: friction lives in the first few paragraphs of a human utterance.
MAX_SCAN_CHARS: Final = 4000

#: Selection rank when several categories match the same utterance. Actionable
#: rules outrank raw emotion: "опять ты трогаешь конфиг, не трогай его" must
#: crystallize as a negative constraint, with frustration only boosting
#: confidence.
_SPECIFICITY: Final[dict[str, int]] = {
    NEGATIVE_CONSTRAINT: 50,
    POSITIVE_REQUIREMENT: 45,
    UNDO_REQUEST: 40,
    HARD_REJECTION: 30,
    FRUSTRATION: 20,
}

#: Confidence bonus applied to an actionable category when the same utterance
#: also carries a frustration marker (a repeated instruction is a stronger rule).
FRUSTRATION_BONUS: Final = 0.10
MAX_CONFIDENCE: Final = 0.99

_FLAGS: Final = re.IGNORECASE | re.UNICODE


@dataclass(frozen=True, slots=True)
class Pattern:
    """One compiled trigger."""

    type: str
    lang: str  # "ru" | "en"
    label: str
    confidence: float
    regex: re.Pattern[str]


# ---------------------------------------------------------------------------
# Raw pattern table: (type, lang, label, confidence, regex source)
# ---------------------------------------------------------------------------
# Confidence is per-pattern, not per-category: a bare "нет" is far weaker
# evidence than "верни как было".

_RAW: Final[tuple[tuple[str, str, str, float, str], ...]] = (
    # -- hard rejections / corrections -------------------------------------
    # Bare "нет"/"no" only count as rejection when the utterance stops there:
    # "нет времени" and "no such file" are statements, not pushback.
    (HARD_REJECTION, "ru", "ru.no_standalone", 0.55,
     r"(?:^|[\n.!?]\s*)нет\b(?=\s*(?:[,.!?…—-]|$))"),
    (HARD_REJECTION, "ru", "ru.not_that", 0.80, r"\bне\s+(?:то|так|туда|это)\b"),
    (HARD_REJECTION, "ru", "ru.wrong", 0.80, r"\b(?:неверно|неправильно|ошибочно)\b"),
    (HARD_REJECTION, "ru", "ru.stop", 0.75, r"\b(?:стоп|хватит|прекрати|перестань|отставить)\b"),
    (HARD_REJECTION, "ru", "ru.not_asked", 0.85,
     r"\bя\s+(?:такого|этого)?\s*не\s+(?:просил|просила|хотел|хотела|говорил|говорила)\b"),
    # "мусор" dropped: it is ordinary vocabulary in build/GC tool output.
    (HARD_REJECTION, "ru", "ru.bad", 0.60,
     r"\b(?:фигня|ерунда|хрень|шляпа)\b"
     r"|(?:^|[\n.!?]\s*)плохо\b"
     r"|\b(?:это|всё|все|получилось|сделал|сделано|вышло)\s+плохо\b"),
    (HARD_REJECTION, "en", "en.no_standalone", 0.55,
     r"(?:^|[\n.!?]\s*)no\b(?=\s*(?:[,.!?…—-]|$))"),
    (HARD_REJECTION, "en", "en.nope", 0.70, r"\b(?:nope|nah)\b"),
    # Clause-initial only, and never the "stop word"/"stop loss" noun compounds.
    (HARD_REJECTION, "en", "en.stop", 0.75,
     r"(?:^|[\n.!?]\s*)stop\b(?![-\s]+(?:words?|word|list|loss|gap|sign|watch|order))"),
    # Bare "wrong" matches "something went wrong" in every stack trace, so it
    # needs either a copular frame, a noun it qualifies, or clause-initial position.
    (HARD_REJECTION, "en", "en.wrong", 0.80,
     r"\b(?:that'?s|thats|this\s+is|it'?s|you'?re|you\s+are)\s+(?:just\s+)?(?:the\s+)?"
     r"(?:wrong|incorrect)\b"
     r"|\bwrong\s+(?:file|place|approach|direction|one|way|answer|thing|order|branch)\b"
     r"|(?:^|[\n.!?]\s*)wrong\b"),
    (HARD_REJECTION, "en", "en.thats_not", 0.85,
     r"\bthat(?:'s|s| is)\s+not\s+(?:right|correct|what|it)\b"),
    (HARD_REJECTION, "en", "en.not_what_i", 0.85,
     r"\bnot\s+what\s+i\s+(?:asked|wanted|meant|said)\b"),
    # -- undo / revert -----------------------------------------------------
    (UNDO_REQUEST, "ru", "ru.revert_as_was", 0.90, r"\bверни\s+(?:как\s+было|обратно|назад)\b"),
    (UNDO_REQUEST, "ru", "ru.rollback", 0.85, r"\b(?:откати|откатись|откатывай|отмени)\b"),
    (UNDO_REQUEST, "ru", "ru.remove_that", 0.75, r"\b(?:убери|удали)\s+(?:это|то|всё|все)\b"),
    (UNDO_REQUEST, "ru", "ru.as_was", 0.80, r"\bкак\s+было\s+(?:раньше|до)\b"),
    (UNDO_REQUEST, "en", "en.undo", 0.90, r"\bundo\s+(?:that|it|this|the)\b"),
    # Needs an object the user owns; the `git ` lookbehind excludes the command.
    (UNDO_REQUEST, "en", "en.revert", 0.85,
     r"(?<!git\s)\brevert\s+(?:that|it|this|your|the\s+\w+|those|my)\b"
     r"|(?<!git\s)\broll\s?back\s+(?:that|it|this|your|the\s+\w+|those|my|please|now)\b"
     r"|(?:^|[\n.!?]\s*)roll\s+back\b"),
    (UNDO_REQUEST, "en", "en.put_back", 0.80, r"\bput\s+(?:it|that|them)\s+back\b"),
    (UNDO_REQUEST, "en", "en.take_out", 0.75, r"\b(?:take|rip)\s+(?:that|it|this)\s+out\b"),
    # -- negative constraints / invariants ---------------------------------
    # Imperative *and* infinitive: "я просил не трогать" states the rule too.
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_touch", 0.90, r"\bне\s+трога(?:й\w*|ть)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_use", 0.90, r"\bне\s+исполь?з(?:уй\w*|овать)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_change", 0.90,
     r"\bне\s+(?:меняй\w*|менять|изменяй\w*|изменять|правь|править"
     r"|редактируй\w*|редактировать)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_add", 0.85,
     r"\bне\s+(?:добавляй\w*|добавлять|создавай\w*|создавать|плоди|плодить"
     r"|пиши|писать|коммить\w*|пуш\w*|пушить)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.never", 0.90, r"\bникогда\s+не\s+\w+"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.stop_adding", 0.85,
     r"\b(?:хватит|перестань|прекрати)\s+\w+(?:ать|ять|ить|еть|ти|чь)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.forbidden", 0.85, r"\b(?:нельзя|запрещено|запрещаю)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.without", 0.70, r"\bбез\s+(?:комментариев|emoji|эмодзи)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.dont_touch", 0.90,
     r"\b(?:don'?t|do\s+not)\s+touch\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.dont_verb", 0.85,
     r"\b(?:don'?t|do\s+not)\s+(?:use|change|modify|edit|add|create|commit|push|rename)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.not_to_verb", 0.85,
     r"\bnot\s+to\s+(?:touch|use|change|modify|edit|add|create|commit|push|rename|do)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.never", 0.90, r"\bnever\s+(?:use|touch|do|add|commit|push)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.stop_adding", 0.85, r"\bstop\s+\w+ing\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.no_longer", 0.80, r"\bno\s+longer\s+use\b"),
    # HTTP "403 Forbidden" and "not allowed by CORS" are diagnostics, not rules.
    (NEGATIVE_CONSTRAINT, "en", "en.forbidden", 0.85,
     r"(?<!\d\d\d\s)\b(?:forbidden|not\s+allowed|off[-\s]limits)\b(?!\s+(?:by|for|to)\b)"),
    # -- positive requirements / enforcements ------------------------------
    (POSITIVE_REQUIREMENT, "ru", "ru.always", 0.85,
     r"\bвсегда\s+(?:делай|используй|пиши|проверяй|запускай|ставь)\b"),
    # "только в проде" / "только из кэша" are scoping, not policy.
    (POSITIVE_REQUIREMENT, "ru", "ru.only_via", 0.85, r"\bтолько\s+(?:через|так)\b"),
    (POSITIVE_REQUIREMENT, "ru", "ru.use_only", 0.85, r"\bиспользуй\s+только\b"),
    # "строго" alone matches "строго по расписанию"; it needs an imperative.
    (POSITIVE_REQUIREMENT, "ru", "ru.mandatory", 0.80,
     r"\b(?:обязательно|непременно)\b|\bстрого\s+(?:следуй|соблюдай|по\s+инструкции)\b"),
    # Bare "надо/нужно/должен" is how Russian states any ordinary task. Only the
    # explicitly universalized forms express a durable requirement.
    (POSITIVE_REQUIREMENT, "ru", "ru.must", 0.75,
     r"\b(?:всегда|каждый\s+раз|обязательно)\s+(?:надо|нужно|должен|должна)\b"
     r"|\b(?:надо|нужно)\s+(?:всегда|обязательно)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.always", 0.85,
     r"\balways\s+(?:use|run|check|write|keep|do|call)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.strictly", 0.85, r"\bstrictly\s+(?:keep|use|follow|stick)\b"),
    # The lookbehind drops "i only use X" — a self-report, not an instruction.
    (POSITIVE_REQUIREMENT, "en", "en.only_use", 0.85,
     r"(?<!i\s)\bonly\s+(?:use|via|through|with)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.must_always", 0.85, r"\bmust\s+always\b"),
    (POSITIVE_REQUIREMENT, "en", "en.make_sure", 0.75, r"\bmake\s+sure\s+(?:you|to|that)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.from_now_on", 0.80, r"\bfrom\s+now\s+on\b"),
    # -- frustration / repetition ------------------------------------------
    (FRUSTRATION, "ru", "ru.i_asked", 0.90,
     r"\bя\s+(?:же\s+)?(?:просил|просила|говорил|говорила|сказал|сказала)\b"),
    (FRUSTRATION, "ru", "ru.already_told", 0.90,
     r"\bя\s+(?:уже|тебе\s+уже)\s+(?:говорил|говорила|просил|просила|сказал|сказала)\b"),
    (FRUSTRATION, "ru", "ru.again_you", 0.85,
     r"\b(?:опять|снова)\s+(?:ты\b|за\s+своё|то\s+же|\w+л\b|\w+ил\b)"),
    (FRUSTRATION, "ru", "ru.how_many_times", 0.90,
     r"\b(?:сколько\s+раз|в\s+который\s+раз|который\s+раз)\b"),
    (FRUSTRATION, "ru", "ru.last_time", 0.85, r"\bв\s+последний\s+раз\s+(?:говорю|повторяю)\b"),
    (FRUSTRATION, "ru", "ru.rhetorical", 0.85,
     r"\bты\s+(?:вообще\s+)?(?:читал|читала|видел|видела|смотрел|смотрела|понял|поняла)\b"
     r"|\bмы\s+это\s+уже\s+(?:проходили|обсуждали)\b"),
    (FRUSTRATION, "en", "en.already_told", 0.90,
     r"\bi\s+(?:already|just)\s+(?:told|said|asked)\b"),
    (FRUSTRATION, "en", "en.how_many_times", 0.90, r"\bhow\s+many\s+times\b"),
    (FRUSTRATION, "en", "en.again_you", 0.85,
     r"\byou\s+(?:did\s+it\s+again|keep\s+\w+ing)\b"
     r"|\byou\s+\w+ed\s+(?:it|that|the\s+\w+|my\s+\w+)\s+again\b"),
    (FRUSTRATION, "en", "en.as_i_said", 0.85, r"\b(?:as|like)\s+i\s+(?:said|told\s+you)\b"),
    (FRUSTRATION, "en", "en.last_time", 0.85, r"\bfor\s+the\s+last\s+time\b"),
    (FRUSTRATION, "en", "en.rhetorical", 0.85,
     r"\bdid\s+you\s+even\s+(?:read|look|check|see)\b"
     r"|\bi\s+didn'?t\s+ask\s+for\b"
     r"|\bwe(?:'ve|\s+have)\s+been\s+(?:over|through)\s+this\b"),
)

#: Ephemeral markers. Presence of any of these suppresses friction detection.
_EPHEMERAL_RAW: Final[tuple[tuple[str, str, str], ...]] = (
    ("ru", "ru.skip_now", r"\b(?:пропусти|пропустим|скипни)\s+(?:сейчас|пока|это\s+пока)\b"),
    ("ru", "ru.this_time", r"\b(?:на\s+этот\s+раз|в\s+этот\s+раз|разово|один\s+раз)\b"),
    ("ru", "ru.for_test", r"\b(?:только\s+для\s+теста|для\s+теста|ради\s+теста)\b"),
    ("ru", "ru.temporary", r"\b(?:временно|пока\s+что|пока\s+не\s+надо|на\s+время)\b"),
    ("en", "en.skip_for_now", r"\bskip\s+(?:it\s+|this\s+|that\s+)?for\s+now\b"),
    ("en", "en.for_now", r"\bfor\s+now\b"),
    ("en", "en.just_this_test", r"\bjust\s+for\s+(?:this|the)\s+(?:test|run|case|time|once)\b"),
    ("en", "en.this_time_only", r"\b(?:this\s+time\s+only|just\s+this\s+once|one[-\s]off)\b"),
    ("en", "en.temporarily", r"\b(?:temporarily|temporary|for\s+the\s+moment)\b"),
)

PATTERNS: Final[tuple[Pattern, ...]] = tuple(
    Pattern(type=t, lang=lang, label=label, confidence=conf, regex=re.compile(src, _FLAGS))
    for (t, lang, label, conf, src) in _RAW
)

EPHEMERAL_PATTERNS: Final[tuple[Pattern, ...]] = tuple(
    Pattern(type=EPHEMERAL, lang=lang, label=label, confidence=1.0,
            regex=re.compile(src, _FLAGS))
    for (lang, label, src) in _EPHEMERAL_RAW
)

_WS_RE: Final = re.compile(r"[ \t\r\f\v]+")

#: Typographic apostrophes macOS substitutes while typing. Without folding these
#: every ``don't`` pattern silently misses.
_APOSTROPHE_TRANS: Final = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'",
                                          "\u00b4": "'", "`": "'"})

#: Cheap gate: homoglyph folding only runs on text that contains Cyrillic at all.
_CYRILLIC_PROBE: Final = re.compile(r"[\u0400-\u04FF]")

_HOMOGLYPH_MAP: Final[dict[str, str]] = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}

#: A Latin homoglyph is only folded when it touches Cyrillic, so ASCII words
#: embedded in Russian text ("не трогай docker") survive untouched.
_HOMOGLYPH_RE: Final = re.compile(
    r"(?<=[\u0400-\u04FF])[aceopxyABCEHKMOPTXY]"
    r"|[aceopxyABCEHKMOPTXY](?=[\u0400-\u04FF])"
)

#: Suppression granularity. An ephemeral marker cancels only the clause it sits
#: in, so "skip the lint for now, but never commit to main" keeps its rule.
#: A plain comma is NOT a boundary — "don't touch it, just this once" is one
#: clause and must stay suppressed.
_CLAUSE_SPLIT_RE: Final = re.compile(
    r"\n+|(?<=[.!?;])\s+|,\s+(?=(?:но|а|but|however)\b)", _FLAGS
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Cheap, lossless-enough normalization applied before matching.

    Folds ``ё`` -> ``е``, typographic apostrophes -> ``'``, and Latin/Cyrillic
    homoglyphs -> Cyrillic; collapses horizontal whitespace and truncates to
    `MAX_SCAN_CHARS`. Newlines are preserved because several patterns anchor
    on them.
    """
    if not text:
        return ""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    text = text.translate(_APOSTROPHE_TRANS)
    text = text.replace("ё", "е").replace("Ё", "Е")
    if _CYRILLIC_PROBE.search(text) is not None:
        text = _fold_homoglyphs(text)
    return _WS_RE.sub(" ", text).strip()


def _fold_homoglyphs(text: str) -> str:
    # Two passes: a run like "нeo" only exposes its second Latin char as
    # Cyrillic-adjacent once the first has been folded.
    for _ in range(2):
        folded = _HOMOGLYPH_RE.sub(lambda m: _HOMOGLYPH_MAP[m.group(0)], text)
        if folded == text:
            break
        text = folded
    return text


def is_ephemeral(text: str) -> bool:
    """True when the utterance scopes itself to "just this once"."""
    norm = normalize(text)
    if not norm:
        return False
    return any(p.regex.search(norm) is not None for p in EPHEMERAL_PATTERNS)


def _clause_spans(norm: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _CLAUSE_SPLIT_RE.finditer(norm):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(norm):
        spans.append((pos, len(norm)))
    return spans or [(0, len(norm))]


def suppressed_spans(norm: str) -> list[tuple[int, int]]:
    """Clause ranges of `norm` cancelled by an ephemeral marker.

    `norm` must already be normalized. Returns ``[]`` when nothing is scoped
    away, which lets the caller take the cheap `re.search` path.
    """
    marks = [
        m.span()
        for p in EPHEMERAL_PATTERNS
        for m in p.regex.finditer(norm)
    ]
    if not marks:
        return []
    return [
        clause
        for clause in _clause_spans(norm)
        if any(ms < clause[1] and clause[0] < me for ms, me in marks)
    ]


def _first_live_match(
    regex: re.Pattern[str], norm: str, dead: list[tuple[int, int]]
) -> re.Match[str] | None:
    if not dead:
        return regex.search(norm)
    for m in regex.finditer(norm):
        start, end = m.span()
        if not any(start < de and ds < end for ds, de in dead):
            return m
    return None


def detect_all(text: str, *, include_ephemeral: bool = False) -> list[dict]:
    """Return every matching pattern, strongest first.

    Unlike `detect_friction` this does NOT apply the ephemeral suppressor;
    pass ``include_ephemeral=True`` to also see which ephemeral markers fired.
    """
    norm = normalize(text)
    if not norm:
        return []
    hits: list[dict] = []
    for p in PATTERNS:
        m = p.regex.search(norm)
        if m is not None:
            hits.append(_hit(p, m, p.confidence))
    if include_ephemeral:
        for p in EPHEMERAL_PATTERNS:
            m = p.regex.search(norm)
            if m is not None:
                hits.append(_hit(p, m, p.confidence))
    hits.sort(key=lambda h: (_SPECIFICITY.get(h["type"], 0), h["confidence"]), reverse=True)
    return hits


def detect_friction(text: str) -> dict | None:
    """Classify an utterance as friction, or return ``None``.

    Returns a dict with:

    ``type``        one of `PATTERN_TYPES`
    ``match``       the matched substring (from the normalized text)
    ``confidence``  0.0-0.99, boosted when frustration co-occurs
    ``label``       stable pattern id, e.g. ``ru.dont_touch``
    ``language``    ``"ru"`` | ``"en"``
    ``span``        (start, end) offsets into the *normalized* text
    ``frustration`` True when a frustration marker was also present

    Returns ``None`` for empty input, for text with no trigger, and for text
    whose only triggers sit inside a clause carrying an ephemeral marker (a
    scoped one-off is not a durable rule). Suppression is clause-scoped, not
    document-scoped: a durable rule stated in a different clause survives.

    Patterns are matched against the FULL normalized text and clauses are used
    only to bound suppression. Slicing before matching would inject artificial
    ``^`` anchors mid-document and make clause-initial patterns fire on
    fragments like ``ValueError: wrong type``.
    """
    norm = normalize(text)
    if not norm:
        return None
    dead = suppressed_spans(norm)

    best: tuple[int, float] = (-1, -1.0)
    best_hit: dict | None = None
    frustrated = False

    for p in PATTERNS:
        m = _first_live_match(p.regex, norm, dead)
        if m is None:
            continue
        if p.type == FRUSTRATION:
            frustrated = True
        rank = (_SPECIFICITY.get(p.type, 0), p.confidence)
        if rank > best:
            best = rank
            best_hit = _hit(p, m, p.confidence)

    if best_hit is None:
        return None

    if frustrated and best_hit["type"] != FRUSTRATION:
        best_hit["confidence"] = round(
            min(MAX_CONFIDENCE, best_hit["confidence"] + FRUSTRATION_BONUS), 2
        )
    best_hit["frustration"] = frustrated
    return best_hit


def _hit(p: Pattern, m: re.Match[str], confidence: float) -> dict:
    return {
        "type": p.type,
        "match": m.group(0).strip(),
        "confidence": round(confidence, 2),
        "label": p.label,
        "language": p.lang,
        "span": m.span(),
        "frustration": p.type == FRUSTRATION,
    }
