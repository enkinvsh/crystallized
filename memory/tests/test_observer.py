"""Tests for the observer + pattern extraction engine.

Covers:
* `patterns.detect_friction` — bilingual RU/EN classification, confidence,
  ephemeral suppression, and determinism/latency.
* `observer` — payload parsing, post-tool and session-end extraction,
  idempotent SQLite persistence, lock tolerance, exit-code contract, budget.

Everything is hermetic: a temp SQLite file via `db.set_db_path`, no network,
no ~/.config/opencode.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

MEMORY_DIR = Path(__file__).resolve().parent.parent
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import db  # noqa: E402
import observer  # noqa: E402
import patterns  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.py at a throwaway SQLite file for the duration of a test."""
    path = tmp_path / "memory.db"
    monkeypatch.setenv("OPENCODE_MEMORY_DB", str(path))
    db.set_db_path(path)
    yield path
    db.close_db()


# ===========================================================================
# patterns.py
# ===========================================================================


class TestNormalize:
    def test_empty_input(self):
        assert patterns.normalize("") == ""
        assert patterns.normalize("   ") == ""

    def test_yo_is_folded(self):
        assert "ё" not in patterns.normalize("не трогай ёлку")

    def test_collapses_horizontal_whitespace(self):
        assert patterns.normalize("не    трогай\tэто") == "не трогай это"

    def test_preserves_newlines(self):
        assert "\n" in patterns.normalize("first\nsecond")

    def test_truncates_to_max_scan(self):
        blob = "x" * (patterns.MAX_SCAN_CHARS * 3)
        assert len(patterns.normalize(blob)) <= patterns.MAX_SCAN_CHARS


class TestHardRejection:
    @pytest.mark.parametrize(
        "text",
        [
            "нет, это не то",
            "не так сделал",
            "неправильно, переделай",
            "стоп",
            "я не просил это делать",
        ],
    )
    def test_russian(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None, text
        assert hit["language"] == "ru"

    @pytest.mark.parametrize(
        "text",
        [
            "no, that is not right",
            "stop",
            "wrong file",
            "that's not what i asked",
            "nope",
        ],
    )
    def test_english(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None, text
        assert hit["language"] == "en"

    def test_classified_as_hard_rejection(self):
        assert patterns.detect_friction("не то")["type"] == patterns.HARD_REJECTION
        assert patterns.detect_friction("wrong")["type"] == patterns.HARD_REJECTION

    def test_bare_no_is_weaker_than_explicit(self):
        weak = patterns.detect_friction("no")
        strong = patterns.detect_friction("that's not what i asked")
        assert weak["confidence"] < strong["confidence"]

    def test_no_inside_word_is_not_a_match(self):
        assert patterns.detect_friction("normalize the nodes") is None


class TestUndoRequest:
    @pytest.mark.parametrize(
        "text",
        ["верни как было", "откати изменения", "верни обратно", "убери это"],
    )
    def test_russian(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.UNDO_REQUEST

    @pytest.mark.parametrize(
        "text",
        ["undo that", "revert the commit", "roll back please", "put it back"],
    )
    def test_english(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.UNDO_REQUEST

    def test_high_confidence(self):
        assert patterns.detect_friction("верни как было")["confidence"] >= 0.85


class TestNegativeConstraint:
    @pytest.mark.parametrize(
        "text",
        [
            "не трогай этот файл",
            "не используй redis",
            "не меняй конфиг",
            "никогда не коммить в main",
            "хватит добавлять комментарии",
        ],
    )
    def test_russian(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.NEGATIVE_CONSTRAINT, text

    @pytest.mark.parametrize(
        "text",
        [
            "don't touch the config",
            "do not touch that directory",
            "never use redis here",
            "stop adding emoji",
            "don't modify the schema",
        ],
    )
    def test_english(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.NEGATIVE_CONSTRAINT, text

    def test_outranks_frustration(self):
        hit = patterns.detect_friction("я же просил, не трогай этот файл")
        assert hit["type"] == patterns.NEGATIVE_CONSTRAINT
        assert hit["frustration"] is True


class TestPositiveRequirement:
    @pytest.mark.parametrize(
        "text",
        [
            "всегда используй uv run",
            "только через миграции",
            "используй только sqlite",
            "обязательно прогони тесты",
        ],
    )
    def test_russian(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.POSITIVE_REQUIREMENT, text

    @pytest.mark.parametrize(
        "text",
        [
            "always use uv run",
            "strictly keep the 150ms budget",
            "only use the shared connection",
            "must always exit 0",
            "from now on run ruff",
        ],
    )
    def test_english(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.POSITIVE_REQUIREMENT, text


class TestFrustration:
    @pytest.mark.parametrize(
        "text",
        ["я же просил", "я уже говорил тебе", "опять ты за своё", "сколько раз повторять"],
    )
    def test_russian(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.FRUSTRATION, text

    @pytest.mark.parametrize(
        "text",
        [
            "i already told you",
            "how many times do i have to say it",
            "as i said before",
            "for the last time",
        ],
    )
    def test_english(self, text):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == patterns.FRUSTRATION, text

    def test_boosts_confidence_of_actionable_signal(self):
        plain = patterns.detect_friction("не трогай конфиг")
        boosted = patterns.detect_friction("я же просил, не трогай конфиг")
        assert boosted["confidence"] > plain["confidence"]
        assert boosted["confidence"] <= patterns.MAX_CONFIDENCE

    def test_flag_false_when_absent(self):
        assert patterns.detect_friction("не трогай конфиг")["frustration"] is False


class TestEphemeralSuppression:
    @pytest.mark.parametrize(
        "text",
        [
            "не используй кэш, пропусти сейчас",
            "не трогай тесты, на этот раз",
            "не запускай линт, только для теста",
            "временно не трогай конфиг",
        ],
    )
    def test_russian_suppressed(self, text):
        assert patterns.detect_friction(text) is None, text

    @pytest.mark.parametrize(
        "text",
        [
            "don't run the tests, skip for now",
            "never mind the lint, just for this test",
            "stop adding logs, this time only",
            "don't touch it temporarily",
        ],
    )
    def test_english_suppressed(self, text):
        assert patterns.detect_friction(text) is None, text

    def test_is_ephemeral_helper(self):
        assert patterns.is_ephemeral("skip for now") is True
        assert patterns.is_ephemeral("пропусти сейчас") is True
        assert patterns.is_ephemeral("не трогай конфиг") is False

    def test_detect_all_can_expose_ephemeral(self):
        hits = patterns.detect_all("не трогай конфиг, пока что", include_ephemeral=True)
        assert any(h["type"] == patterns.EPHEMERAL for h in hits)
        assert any(h["type"] == patterns.NEGATIVE_CONSTRAINT for h in hits)

    @pytest.mark.parametrize(
        "text,label",
        [
            ("пропусти сейчас. и никогда не коммить в main", "ru.never"),
            ("skip the lint for now, but never commit to main", "en.never"),
            ("temporarily disable it.\nalways use uv run", "en.always"),
            ("just this once, ok? don't touch the schema", "en.dont_touch"),
            ("не трогай конфиг, но тесты пропусти сейчас", "ru.dont_touch"),
        ],
    )
    def test_durable_rule_in_another_clause_survives(self, text, label):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["label"] == label, text

    @pytest.mark.parametrize(
        "text",
        [
            "не трогай это, только для теста",
            "don't touch it, just this once",
            "skip the tests for now",
        ],
    )
    def test_marker_still_suppresses_its_own_clause(self, text):
        assert patterns.detect_friction(text) is None, text

    def test_suppressed_spans_empty_without_marker(self):
        assert patterns.suppressed_spans(patterns.normalize("не трогай конфиг")) == []


class TestCalibratedFalsePositives:
    """Non-friction phrasings the 2026-08-22 audit measured as live FPs."""

    @pytest.mark.parametrize(
        "text",
        [
            "docker stop the container",
            "stop words list for the tokenizer",
            "something went wrong in the build",
            "ValueError: wrong type",
            "git revert the last commit",
            "403 Forbidden",
            "not allowed by CORS policy",
            "you need Node >= 20 installed",
            "i only use uv here",
        ],
    )
    def test_english_non_friction(self, text):
        assert patterns.detect_friction(text) is None, text

    @pytest.mark.parametrize(
        "text",
        [
            "надо скопировать файл",
            "нужно сделать миграцию",
            "надо понять почему падает",
            "нужно для отчёта",
            "должен вернуть json",
            "только в проде",
            "только из кэша",
            "строго по расписанию",
            "нет времени на это",
            "убрал мусор из билда",
        ],
    )
    def test_russian_non_friction(self, text):
        assert patterns.detect_friction(text) is None, text


class TestCalibratedRecall:
    @pytest.mark.parametrize(
        "text,ptype",
        [
            ("я же просил не трогать этот файл", patterns.NEGATIVE_CONSTRAINT),
            ("я просил не использовать redis", patterns.NEGATIVE_CONSTRAINT),
            ("не менять схему", patterns.NEGATIVE_CONSTRAINT),
            ("i told you not to touch that", patterns.NEGATIVE_CONSTRAINT),
            ("всегда надо прогонять тесты", patterns.POSITIVE_REQUIREMENT),
            ("нужно обязательно чистить кэш", patterns.POSITIVE_REQUIREMENT),
            ("we have been over this", patterns.FRUSTRATION),
            ("did you even read the file", patterns.FRUSTRATION),
            ("мы это уже обсуждали", patterns.FRUSTRATION),
            ("ты вообще читал документацию", patterns.FRUSTRATION),
            ("опять сломал сборку", patterns.FRUSTRATION),
            ("that's the wrong file", patterns.HARD_REJECTION),
        ],
    )
    def test_still_detected(self, text, ptype):
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["type"] == ptype, text


class TestNormalizeFolding:
    @pytest.mark.parametrize("apostrophe", ["\u2019", "\u2018", "\u02bc", "\u00b4", "`"])
    def test_smart_apostrophes_are_folded(self, apostrophe):
        text = f"don{apostrophe}t touch the config"
        assert patterns.normalize(text).count("'") == 1
        hit = patterns.detect_friction(text)
        assert hit is not None and hit["label"] == "en.dont_touch"

    @pytest.mark.parametrize(
        "text",
        ["нe трогай конфиг", "никогда нe используй redis", "нeправильно"],
    )
    def test_latin_homoglyphs_adjacent_to_cyrillic_are_folded(self, text):
        assert patterns.detect_friction(text) is not None, text

    def test_pure_ascii_is_left_alone(self):
        assert patterns.normalize("dont touch the docker cache") == (
            "dont touch the docker cache"
        )

    def test_ascii_words_inside_russian_survive(self):
        assert "docker" in patterns.normalize("не трогай docker")


class TestDetectFrictionContract:
    def test_returns_none_for_empty(self):
        assert patterns.detect_friction("") is None
        assert patterns.detect_friction("   \n ") is None

    def test_returns_none_for_neutral_text(self):
        assert patterns.detect_friction("please add a test for the parser") is None
        assert patterns.detect_friction("добавь тест для парсера") is None

    def test_required_keys_present(self):
        hit = patterns.detect_friction("don't touch the config")
        for key in ("type", "match", "confidence", "label", "language", "span", "frustration"):
            assert key in hit

    def test_match_is_a_substring_of_normalized_text(self):
        text = "ПОЖАЛУЙСТА, НЕ  ТРОГАЙ конфиг"
        hit = patterns.detect_friction(text)
        assert hit["match"] in patterns.normalize(text)

    def test_type_is_a_known_constant(self):
        hit = patterns.detect_friction("always use uv run")
        assert hit["type"] in patterns.PATTERN_TYPES

    def test_confidence_in_range(self):
        for text in ("не трогай", "undo that", "no", "я же просил", "always use uv"):
            hit = patterns.detect_friction(text)
            assert 0.0 < hit["confidence"] <= patterns.MAX_CONFIDENCE

    def test_deterministic(self):
        text = "я же просил, не используй redis"
        first = patterns.detect_friction(text)
        for _ in range(50):
            assert patterns.detect_friction(text) == first

    def test_case_insensitive(self):
        assert patterns.detect_friction("DON'T TOUCH the config") is not None
        assert patterns.detect_friction("НЕ ТРОГАЙ конфиг") is not None

    def test_detect_all_sorted_strongest_first(self):
        hits = patterns.detect_all("я же просил, не трогай конфиг")
        assert len(hits) >= 2
        assert hits[0]["type"] == patterns.NEGATIVE_CONSTRAINT


class TestPatternPerformance:
    def test_scan_is_fast_on_large_hostile_input(self):
        blob = ("a" * 500 + " не трогай " + "b(((((" * 200 + "\n") * 40
        start = time.perf_counter()
        patterns.detect_friction(blob)
        assert (time.perf_counter() - start) * 1000 < 50

    def test_many_scans_stay_within_budget(self):
        corpus = [
            "не трогай этот файл",
            "always use uv run",
            "nothing interesting here at all",
            "i already told you not to commit",
        ]
        start = time.perf_counter()
        for _ in range(250):
            for text in corpus:
                patterns.detect_friction(text)
        assert (time.perf_counter() - start) * 1000 < 1000

    def test_every_pattern_compiles_and_is_labelled(self):
        labels = [p.label for p in patterns.PATTERNS]
        assert len(labels) == len(set(labels)), "pattern labels must be unique"
        for p in patterns.PATTERNS:
            assert p.lang in ("ru", "en")
            assert 0.0 < p.confidence <= 1.0

    def test_both_languages_present_in_every_category(self):
        for ptype in patterns.PATTERN_TYPES:
            langs = {p.lang for p in patterns.PATTERNS if p.type == ptype}
            assert langs == {"ru", "en"}, f"{ptype} missing a language: {langs}"


# ===========================================================================
# observer.py — parsing
# ===========================================================================


class TestParsePayload:
    def test_valid_object(self):
        assert observer.parse_payload('{"tool_name": "Bash"}') == {"tool_name": "Bash"}

    def test_bytes_input(self):
        assert observer.parse_payload(b'{"a": 1}') == {"a": 1}

    @pytest.mark.parametrize("raw", [None, "", "   ", "not json", "[1,2,3]", '{"broken":'])
    def test_garbage_returns_empty_dict(self, raw):
        assert observer.parse_payload(raw) == {}

    def test_never_raises_on_binary(self):
        assert observer.parse_payload(b"\xff\xfe\x00") == {}


class TestStableId:
    def test_deterministic_and_short(self):
        a = observer.stable_id("ref:1", "не трогай конфиг")
        b = observer.stable_id("ref:1", "не трогай конфиг")
        assert a == b and len(a) == 16

    def test_normalization_insensitive(self):
        assert observer.stable_id("ref", "НЕ  ТРОГАЙ") == observer.stable_id("ref", "не трогай")

    def test_differs_by_source_ref(self):
        assert observer.stable_id("ref:1", "x") != observer.stable_id("ref:2", "x")


class TestDeadline:
    def test_not_expired_immediately(self):
        assert observer.Deadline(150.0).expired() is False

    def test_expires(self):
        d = observer.Deadline(0.0)
        assert d.expired() is True

    def test_elapsed_is_monotonic(self):
        d = observer.Deadline()
        first = d.elapsed_ms()
        assert d.elapsed_ms() >= first


# ===========================================================================
# observer.py — post-tool extraction
# ===========================================================================


class TestPostToolObservations:
    def test_clean_output_yields_nothing(self):
        payload = {
            "tool_name": "Read",
            "session_id": "s1",
            "tool_response": {"output": "line one\nline two"},
        }
        assert observer.post_tool_observations(payload) == []

    def test_error_flag_is_captured(self):
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_response": {"is_error": True, "stderr": "boom"},
        }
        obs = observer.post_tool_observations(payload)
        assert len(obs) == 1
        # Telemetry rows are identified by tag, never by cause/effect: they
        # deliberately state no causal pair, so that an episode made of nothing
        # else cannot agree one into existence. Restoring `.effect` here would
        # restore belief:b8991d5e79839166 with it.
        assert "tool-error" in obs[0].tags
        assert "tool:Bash" in obs[0].tags

    def test_nonzero_exit_code_is_an_error(self):
        payload = {"tool_name": "Bash", "tool_response": {"exit_code": 2, "stdout": "nope"}}
        assert "tool-error" in observer.post_tool_observations(payload)[0].tags

    def test_traceback_text_is_an_error(self):
        payload = {
            "tool_name": "Bash",
            "tool_response": "Traceback (most recent call last):\n  ZeroDivisionError",
        }
        assert "tool-error" in observer.post_tool_observations(payload)[0].tags

    @pytest.mark.parametrize(
        "output",
        [
            "не трогай этот файл",
            "don't touch the config",
            "always use uv run",
            "Stop\nNo\nwrong",
            "я же просил",
        ],
    )
    def test_friction_in_tool_output_is_never_captured(self, output):
        payload = {"tool_name": "Bash", "session_id": "s9", "tool_response": {"output": output}}
        obs = observer.post_tool_observations(payload)
        assert all(o.effect not in patterns.PATTERN_TYPES for o in obs), output

    def test_friction_carrying_output_still_reports_a_real_error(self):
        payload = {
            "tool_name": "Bash",
            "tool_response": {"is_error": True, "stderr": "не трогай этот файл"},
        }
        obs = observer.post_tool_observations(payload)
        assert len(obs) == 1
        assert "tool-error" in obs[0].tags

    def test_todowrite_output_produces_nothing(self):
        payload = {
            "tool_name": "TodoWrite",
            "tool_response": {"output": "Stop the ralph loop\nNo pending items"},
        }
        assert observer.post_tool_observations(payload) == []

    def test_camelcase_keys_supported(self):
        payload = {"toolName": "Bash", "sessionId": "s2", "toolResponse": {"error": "x"}}
        obs = observer.post_tool_observations(payload)
        assert obs and obs[0].session_id == "s2"

    def test_empty_payload_is_safe(self):
        assert observer.post_tool_observations({}) == []

    def test_observed_at_prefers_payload_timestamp(self):
        payload = {
            "tool_name": "Bash",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "tool_response": {"error": "x"},
        }
        assert observer.post_tool_observations(payload)[0].observed_at == "2026-01-01T00:00:00+00:00"


# ===========================================================================
# observer.py — session-end extraction
# ===========================================================================


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), "utf-8")
    return path


class TestSessionEndObservations:
    def test_summary_always_emitted(self, tmp_path):
        path = _write_transcript(tmp_path, [{"role": "user", "content": "hello"}])
        obs = observer.session_end_observations({"session_id": "s1"}, path)
        assert "session-summary" in obs[-1].tags

    def test_summary_emitted_without_transcript(self):
        obs = observer.session_end_observations({"session_id": "s1"}, None)
        assert len(obs) == 1 and "session-summary" in obs[0].tags

    def test_user_friction_is_extracted(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "не трогай этот файл"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "i already told you"},
        ])
        obs = observer.session_end_observations({"session_id": "s1"}, path)
        effects = {o.effect for o in obs}
        assert patterns.NEGATIVE_CONSTRAINT in effects
        assert patterns.FRUSTRATION in effects

    def test_friction_states_its_type_but_never_a_cause(self, tmp_path):
        """The effect is a classification; the cause was never written down."""
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "не трогай этот файл"},
        ])
        obs = observer.session_end_observations({}, path)
        friction = [o for o in obs if o.effect == patterns.NEGATIVE_CONSTRAINT]
        assert len(friction) == 1
        assert friction[0].cause is None

    def test_confidence_is_damped_to_l0_range(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "не трогай этот файл"},
        ])
        obs = observer.session_end_observations({}, path)
        friction = [o for o in obs if o.effect == patterns.NEGATIVE_CONSTRAINT][0]
        assert 0.2 <= friction.confidence <= 0.5

    def test_assistant_messages_are_ignored(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "assistant", "content": "don't touch the config"},
        ])
        obs = observer.session_end_observations({}, path)
        assert len(obs) == 1  # summary only

    def test_claude_code_nested_message_shape(self, tmp_path):
        path = _write_transcript(tmp_path, [{
            "type": "user",
            "timestamp": "2026-02-02T10:00:00+00:00",
            "message": {"role": "user", "content": [{"type": "text", "text": "верни как было"}]},
        }])
        obs = observer.session_end_observations({"session_id": "s3"}, path)
        undo = [o for o in obs if o.effect == patterns.UNDO_REQUEST]
        assert undo and undo[0].observed_at == "2026-02-02T10:00:00+00:00"

    def test_source_ref_carries_line_number(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "не трогай конфиг"},
        ])
        obs = observer.session_end_observations({}, path)
        friction = [o for o in obs if o.effect == patterns.NEGATIVE_CONSTRAINT][0]
        assert friction.source_ref.endswith(":2")

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            'not json\n{"role": "user", "content": "не трогай конфиг"}\n{"broken\n',
            "utf-8",
        )
        obs = observer.session_end_observations({}, path)
        assert any(o.effect == patterns.NEGATIVE_CONSTRAINT for o in obs)

    def test_missing_transcript_is_safe(self, tmp_path):
        obs = observer.session_end_observations({}, tmp_path / "nope.jsonl")
        assert len(obs) == 1 and "session-summary" in obs[0].tags

    def test_ephemeral_user_message_does_not_create_a_rule(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "не запускай тесты, пропусти сейчас"},
        ])
        obs = observer.session_end_observations({}, path)
        assert len(obs) == 1  # summary only

    def test_only_tail_of_a_huge_transcript_is_scanned(self, tmp_path):
        entries = [{"role": "user", "content": f"msg {i}"} for i in range(1200)]
        entries.append({"role": "user", "content": "не трогай конфиг"})
        path = _write_transcript(tmp_path, entries)
        obs = observer.session_end_observations({}, path)
        assert any(o.effect == patterns.NEGATIVE_CONSTRAINT for o in obs)

    def test_expired_deadline_still_returns_summary(self, tmp_path):
        path = _write_transcript(tmp_path, [
            {"role": "user", "content": "не трогай конфиг"},
        ])
        obs = observer.session_end_observations({}, path, observer.Deadline(0.0))
        assert "session-summary" in obs[-1].tags
        assert "truncated" in obs[-1].text


class TestTelemetryCarriesNoCausalClaim:
    """Telemetry rows state no cause/effect, so no belief can be minted from them.

    Both rows are emitted unconditionally — one per failing tool call, one per
    session end — so an episode built out of them agrees with itself trivially.
    ``dream._coherent_pair`` inherits a pair when EVERY member states the same
    one, and Pass 3 then promotes that agreement: ``belief:b8991d5e79839166``
    ("tool_call_bash causes tool_error", evidence ``l1:ec3384b5c665449b``) and
    ``belief:950649f22e74f369`` ("session_end causes session_summary") are both
    live in the store, asserted from a heartbeat. A row that claims nothing
    cannot be agreed with.
    """

    def test_a_tool_error_states_no_cause_or_effect(self):
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_response": {"is_error": True, "stderr": "boom"},
        }
        obs = observer.post_tool_observations(payload)
        assert len(obs) == 1
        assert obs[0].cause is None and obs[0].effect is None

    def test_a_session_summary_states_no_cause_or_effect(self):
        obs = observer.session_end_observations({"session_id": "s1"}, None)
        assert len(obs) == 1
        assert obs[0].cause is None and obs[0].effect is None

    def test_the_telemetry_tags_are_unchanged(self):
        """The exclusion in `dream` keys off these tags — they are the contract."""
        error = observer.post_tool_observations({
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_response": {"is_error": True, "stderr": "boom"},
        })[0]
        summary = observer.session_end_observations({"session_id": "s1"}, None)[0]

        assert error.tags == ("observer", "post-tool", "tool-error", "tool:Bash")
        assert summary.tags == ("observer", "session-end", "session-summary")
        assert observer.TELEMETRY_TAGS <= set(error.tags) | set(summary.tags)


class TestResolveTranscript:
    def test_from_payload_snake_case(self):
        p = observer._resolve_transcript({"transcript_path": "/tmp/a.jsonl"}, [])
        assert p == Path("/tmp/a.jsonl")

    def test_from_payload_camel_case(self):
        p = observer._resolve_transcript({"transcriptPath": "/tmp/b.jsonl"}, [])
        assert p == Path("/tmp/b.jsonl")

    def test_cli_flag_wins(self):
        p = observer._resolve_transcript(
            {"transcript_path": "/tmp/a.jsonl"}, ["--transcript", "/tmp/c.jsonl"]
        )
        assert p == Path("/tmp/c.jsonl")

    def test_absent(self):
        assert observer._resolve_transcript({}, []) is None


# ===========================================================================
# observer.py — persistence
# ===========================================================================


class TestRecord:
    def test_writes_l0_rows(self, temp_db):
        obs = [observer.Observation(
            text="не трогай конфиг", source_ref="ref:1", session_id="s1",
            cause="user_message", effect=patterns.NEGATIVE_CONSTRAINT, confidence=0.3,
            tags=("observer", "friction"),
        )]
        assert observer.record(obs) == 1
        rows = db.causal_query(layer=0, session_id="s1")
        assert len(rows) == 1
        assert rows[0]["text"] == "не трогай конфиг"
        assert rows[0]["layer"] == 0
        assert rows[0]["tags"] == "observer,friction"

    def test_empty_list_is_a_noop(self, temp_db):
        assert observer.record([]) == 0

    def test_idempotent_on_rerun(self, temp_db):
        obs = [observer.Observation(text="не трогай", source_ref="ref:1", session_id="s1")]
        observer.record(obs)
        observer.record(obs)
        observer.record(list(obs))
        assert len(db.causal_query(layer=0, session_id="s1")) == 1

    def test_bitemporal_fields_populated(self, temp_db):
        observer.record([observer.Observation(
            text="x", source_ref="r", session_id="s",
            observed_at="2026-03-03T12:00:00+00:00",
        )])
        row = db.causal_query(session_id="s")[0]
        assert row["observed_at"] == "2026-03-03T12:00:00+00:00"
        assert row["recorded_at"] and row["recorded_at"] != row["observed_at"]

    def test_locked_database_is_swallowed(self, temp_db, monkeypatch):
        def boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "causal_insert", boom)
        obs = [observer.Observation(text="x", source_ref="r")]
        assert observer.record(obs) == 0

    def test_expired_deadline_stops_writing(self, temp_db):
        obs = [observer.Observation(text=f"t{i}", source_ref=f"r{i}") for i in range(5)]
        assert observer.record(obs, observer.Deadline(0.0)) == 0


# ===========================================================================
# observer.py — CLI contract
# ===========================================================================


class TestCliContract:
    def test_post_tool_persists(self, temp_db):
        payload = json.dumps({
            "tool_name": "Bash", "session_id": "cli1",
            "tool_response": {"is_error": True, "stderr": "boom"},
        })
        assert observer.main(["--post-tool"], stdin_text=payload) == 0
        assert db.causal_query(session_id="cli1")

    def test_session_end_persists(self, temp_db, tmp_path):
        path = _write_transcript(tmp_path, [{"role": "user", "content": "не трогай конфиг"}])
        payload = json.dumps({"session_id": "cli2", "transcript_path": str(path)})
        assert observer.main(["--session-end"], stdin_text=payload) == 0
        rows = db.causal_query(session_id="cli2")
        assert patterns.NEGATIVE_CONSTRAINT in {r["effect"] for r in rows}
        assert any("session-summary" in r["tags"].split(",") for r in rows)

    @pytest.mark.parametrize(
        "argv,stdin",
        [
            (["--post-tool"], ""),
            (["--post-tool"], "garbage"),
            (["--post-tool"], "{}"),
            (["--session-end"], "not json"),
            (["--session-end"], '{"transcript_path": "/nonexistent/x.jsonl"}'),
            ([], ""),
            (["--unknown-flag"], "{}"),
        ],
    )
    def test_always_exit_zero(self, temp_db, argv, stdin):
        assert observer.main(argv, stdin_text=stdin) == 0

    def test_exit_zero_even_when_db_explodes(self, temp_db, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("catastrophic")

        monkeypatch.setattr(observer, "record", boom)
        payload = json.dumps({"tool_name": "Bash", "tool_response": {"error": "x"}})
        assert observer.main(["--post-tool"], stdin_text=payload) == 0

    def test_post_tool_prints_nothing(self, temp_db, capsys):
        payload = json.dumps({"tool_name": "Bash", "tool_response": {"error": "x"}})
        observer.main(["--post-tool"], stdin_text=payload)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_session_end_prints_nothing(self, temp_db, tmp_path, capsys):
        path = _write_transcript(tmp_path, [{"role": "user", "content": "не трогай конфиг"}])
        observer.main(["--session-end"],
                      stdin_text=json.dumps({"transcript_path": str(path)}))
        assert capsys.readouterr().out == ""


class TestObserverBudget:
    def test_post_tool_under_budget(self, temp_db):
        payload = json.dumps({
            "tool_name": "Bash", "session_id": "budget1",
            "tool_response": {"output": "x" * 50_000 + " не трогай конфиг"},
        })
        observer.main(["--post-tool"], stdin_text=payload)  # warm the connection
        start = time.perf_counter()
        observer.main(["--post-tool"], stdin_text=payload)
        assert (time.perf_counter() - start) * 1000 < observer.BUDGET_MS

    def test_session_end_under_budget(self, temp_db, tmp_path):
        entries = [{"role": "user", "content": f"обычное сообщение {i}"} for i in range(500)]
        entries.append({"role": "user", "content": "не трогай конфиг"})
        path = _write_transcript(tmp_path, entries)
        payload = json.dumps({"session_id": "budget2", "transcript_path": str(path)})
        observer.main(["--session-end"], stdin_text=payload)  # warm
        start = time.perf_counter()
        observer.main(["--session-end"], stdin_text=payload)
        assert (time.perf_counter() - start) * 1000 < observer.BUDGET_MS * 2
