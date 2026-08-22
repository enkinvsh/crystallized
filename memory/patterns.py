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
    (HARD_REJECTION, "ru", "ru.no_standalone", 0.55, r"(?:^|[\n.!?]\s*)нет\b"),
    (HARD_REJECTION, "ru", "ru.not_that", 0.80, r"\bне\s+(?:то|так|туда|это)\b"),
    (HARD_REJECTION, "ru", "ru.wrong", 0.80, r"\b(?:неверно|неправильно|ошибочно)\b"),
    (HARD_REJECTION, "ru", "ru.stop", 0.75, r"\b(?:стоп|хватит|прекрати|перестань|отставить)\b"),
    (HARD_REJECTION, "ru", "ru.not_asked", 0.85,
     r"\bя\s+(?:такого|этого)?\s*не\s+(?:просил|просила|хотел|хотела|говорил|говорила)\b"),
    (HARD_REJECTION, "ru", "ru.bad", 0.60, r"\b(?:плохо|фигня|ерунда|мусор)\b"),
    (HARD_REJECTION, "en", "en.no_standalone", 0.55, r"(?:^|[\n.!?]\s*)no\b"),
    (HARD_REJECTION, "en", "en.nope", 0.70, r"\b(?:nope|nah)\b"),
    (HARD_REJECTION, "en", "en.stop", 0.75, r"\bstop\b"),
    (HARD_REJECTION, "en", "en.wrong", 0.80, r"\b(?:wrong|incorrect)\b"),
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
    (UNDO_REQUEST, "en", "en.revert", 0.85, r"\b(?:revert|roll\s?back)\b"),
    (UNDO_REQUEST, "en", "en.put_back", 0.80, r"\bput\s+(?:it|that|them)\s+back\b"),
    (UNDO_REQUEST, "en", "en.take_out", 0.75, r"\b(?:take|rip)\s+(?:that|it|this)\s+out\b"),
    # -- negative constraints / invariants ---------------------------------
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_touch", 0.90, r"\bне\s+трогай\w*\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_use", 0.90, r"\bне\s+исполь?зуй\w*\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_change", 0.90,
     r"\bне\s+(?:меняй\w*|изменяй\w*|правь|редактируй)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.dont_add", 0.85,
     r"\bне\s+(?:добавляй\w*|создавай\w*|плоди|пиши|коммить\w*|пуш\w*)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.never", 0.90, r"\bникогда\s+не\s+\w+"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.stop_adding", 0.85,
     r"\b(?:хватит|перестань|прекрати)\s+(?:добавлять|создавать|писать|трогать|менять)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.forbidden", 0.85, r"\b(?:нельзя|запрещено|запрещаю)\b"),
    (NEGATIVE_CONSTRAINT, "ru", "ru.without", 0.70, r"\bбез\s+(?:комментариев|emoji|эмодзи)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.dont_touch", 0.90,
     r"\b(?:don'?t|do\s+not)\s+touch\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.dont_verb", 0.85,
     r"\b(?:don'?t|do\s+not)\s+(?:use|change|modify|edit|add|create|commit|push|rename)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.never", 0.90, r"\bnever\s+(?:use|touch|do|add|commit|push)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.stop_adding", 0.85,
     r"\bstop\s+(?:adding|creating|writing|touching|changing|using)\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.no_longer", 0.80, r"\bno\s+longer\s+use\b"),
    (NEGATIVE_CONSTRAINT, "en", "en.forbidden", 0.85,
     r"\b(?:forbidden|not\s+allowed|off[-\s]limits)\b"),
    # -- positive requirements / enforcements ------------------------------
    (POSITIVE_REQUIREMENT, "ru", "ru.always", 0.85,
     r"\bвсегда\s+(?:делай|используй|пиши|проверяй|запускай|ставь)\b"),
    (POSITIVE_REQUIREMENT, "ru", "ru.only_via", 0.85, r"\bтолько\s+(?:через|так|из|в)\b"),
    (POSITIVE_REQUIREMENT, "ru", "ru.use_only", 0.85, r"\bиспользуй\s+только\b"),
    (POSITIVE_REQUIREMENT, "ru", "ru.mandatory", 0.80, r"\b(?:обязательно|строго|непременно)\b"),
    (POSITIVE_REQUIREMENT, "ru", "ru.must", 0.75, r"\b(?:должен|должна|надо|нужно)\s+\w+"),
    (POSITIVE_REQUIREMENT, "en", "en.always", 0.85,
     r"\balways\s+(?:use|run|check|write|keep|do|call)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.strictly", 0.85, r"\bstrictly\s+(?:keep|use|follow|stick)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.only_use", 0.85, r"\bonly\s+(?:use|via|through|with)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.must_always", 0.85, r"\bmust\s+always\b"),
    (POSITIVE_REQUIREMENT, "en", "en.make_sure", 0.75, r"\bmake\s+sure\s+(?:you|to|that)\b"),
    (POSITIVE_REQUIREMENT, "en", "en.from_now_on", 0.80, r"\bfrom\s+now\s+on\b"),
    # -- frustration / repetition ------------------------------------------
    (FRUSTRATION, "ru", "ru.i_asked", 0.90,
     r"\bя\s+же\s+(?:просил|просила|говорил|говорила|сказал|сказала)\b"),
    (FRUSTRATION, "ru", "ru.already_told", 0.90,
     r"\bя\s+(?:уже|тебе\s+уже)\s+(?:говорил|говорила|просил|просила|сказал|сказала)\b"),
    (FRUSTRATION, "ru", "ru.again_you", 0.85, r"\b(?:опять|снова)\s+(?:ты|за\s+своё|то\s+же)\b"),
    (FRUSTRATION, "ru", "ru.how_many_times", 0.90,
     r"\b(?:сколько\s+раз|в\s+который\s+раз|который\s+раз)\b"),
    (FRUSTRATION, "ru", "ru.last_time", 0.85, r"\bв\s+последний\s+раз\s+(?:говорю|повторяю)\b"),
    (FRUSTRATION, "en", "en.already_told", 0.90,
     r"\bi\s+(?:already|just)\s+(?:told|said|asked)\b"),
    (FRUSTRATION, "en", "en.how_many_times", 0.90, r"\bhow\s+many\s+times\b"),
    (FRUSTRATION, "en", "en.again_you", 0.85, r"\byou\s+(?:did\s+it\s+again|keep\s+\w+ing)\b"),
    (FRUSTRATION, "en", "en.as_i_said", 0.85, r"\b(?:as|like)\s+i\s+(?:said|told\s+you)\b"),
    (FRUSTRATION, "en", "en.last_time", 0.85, r"\bfor\s+the\s+last\s+time\b"),
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Cheap, lossless-enough normalization applied before matching.

    Folds ``ё`` -> ``е`` (Russian users type both), collapses horizontal
    whitespace and truncates to `MAX_SCAN_CHARS`. Newlines are preserved
    because several patterns anchor on them.
    """
    if not text:
        return ""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    text = text.replace("ё", "е").replace("Ё", "Е")
    return _WS_RE.sub(" ", text).strip()


def is_ephemeral(text: str) -> bool:
    """True when the utterance scopes itself to "just this once"."""
    norm = normalize(text)
    if not norm:
        return False
    return any(p.regex.search(norm) is not None for p in EPHEMERAL_PATTERNS)


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
    carrying an ephemeral marker (a scoped one-off is not a durable rule).
    """
    norm = normalize(text)
    if not norm:
        return None
    if any(p.regex.search(norm) is not None for p in EPHEMERAL_PATTERNS):
        return None

    best: tuple[int, float] = (-1, -1.0)
    best_hit: dict | None = None
    frustrated = False

    for p in PATTERNS:
        m = p.regex.search(norm)
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
