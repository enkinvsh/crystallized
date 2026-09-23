"""Fact selection of the UserPromptSubmit hook (memory-inject.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "memory-inject.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("memory_inject", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def _fact(key: str, score: float, value: str = "v", **extra) -> dict:
    return {"key": key, "value": value, "score": score, "volume": 50.0, "expired": False, **extra}


# --- extract_terms ---------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ranetka сифон на NL", ["ranetka", "nl"]),
        ("какая модель стоит в code-execution", ["code execution"]),
        ("нода w1 опять течёт", ["w1"]),
        ("команда /info не работает", ["info"]),
        ("1 и 3", []),
        ("эм", []),
    ],
)
def test_extract_terms_anchors_latin_entities_in_russian(message, expected):
    assert hook.extract_terms(message) == expected


def test_extract_terms_drops_english_stopwords():
    assert hook.extract_terms("Why is the railway deploy not working for me?") == [
        "railway",
        "deploy",
        "working",
    ]


def test_extract_terms_dedupes_and_caps():
    message = " ".join(f"term{i}" for i in range(40)) + " term0 term1"
    terms = hook.extract_terms(message)
    assert len(terms) == hook.MAX_TERMS
    assert len(set(terms)) == len(terms)


# --- anchor_strengths ------------------------------------------------------


def _live(*pairs: tuple[str, str]) -> list[tuple[str, str, str]]:
    return [(k, hook._norm(k), hook._norm(v)) for k, v in pairs]


LIVE = _live(
    ("ranetka_nodes", "three nodes"),
    ("tattoo_studio", "ranetka mentioned here"),
    ("misc_online", "service is online"),
    ("other_a", "nothing"),
)


def test_anchor_key_match_gets_full_idf():
    strengths = hook.anchor_strengths(["ranetka"], LIVE)
    assert strengths["ranetka_nodes"] == pytest.approx(math.log(4 / 2))


def test_anchor_value_only_match_gets_half_idf():
    strengths = hook.anchor_strengths(["ranetka"], LIVE)
    assert strengths["tattoo_studio"] == pytest.approx(math.log(4 / 2) / 2)


def test_anchor_unknown_term_ignored():
    assert hook.anchor_strengths(["zzzunknown"], LIVE) == {}


def test_anchor_respects_word_boundaries():
    assert hook.anchor_strengths(["nl"], LIVE) == {}


def test_anchor_matches_normalized_multiword_key():
    live = _live(("omo_code_execution_model", "x"), ("a", "b"))
    strengths = hook.anchor_strengths(["code execution"], live)
    assert strengths["omo_code_execution_model"] == pytest.approx(math.log(2))


# --- rank_facts ------------------------------------------------------------


def test_rank_skips_superseded():
    facts = [_fact("a", 0.9, value="[superseded by b] old")]
    assert hook.rank_facts(facts, {}, [], {}) == []


def test_rank_skips_expired():
    facts = [_fact("a", 0.9, expired=True)]
    assert hook.rank_facts(facts, {}, [], {}) == []


def test_rank_skips_seen():
    facts = [_fact("a", 0.9)]
    assert hook.rank_facts(facts, {}, [], {"a": time.time()}) == []


def test_rank_rejects_bare_semantic_below_floor():
    assert hook.rank_facts([_fact("a", 0.70)], {}, [], {}) == []


def test_rank_accepts_bare_semantic_above_floor():
    ranked = hook.rank_facts([_fact("a", 0.73)], {}, [], {})
    assert [f["key"] for f in ranked] == ["a"]
    assert ranked[0]["final"] == pytest.approx(0.73)


def test_rank_strong_anchor_lifts_weak_semantic():
    ranked = hook.rank_facts([_fact("a", 0.45)], {"a": 5.0}, [], {})
    assert ranked[0]["final"] == pytest.approx(0.80)


def test_rank_project_bonus_applied():
    ranked = hook.rank_facts([_fact("proj_x", 0.70)], {}, ["proj"], {})
    assert ranked[0]["final"] == pytest.approx(0.75)


def test_rank_caps_at_max_facts_in_descending_order():
    facts = [_fact(f"k{i}", 0.73 + i * 0.01) for i in range(hook.MAX_FACTS + 3)]
    ranked = hook.rank_facts(facts, {}, [], {})
    finals = [f["final"] for f in ranked]
    assert len(ranked) == hook.MAX_FACTS
    assert finals == sorted(finals, reverse=True)
    assert ranked[0]["key"] == f"k{hook.MAX_FACTS + 2}"


def test_rank_does_not_mutate_input():
    facts = [_fact("a", 0.9)]
    hook.rank_facts(facts, {}, [], {})
    assert "final" not in facts[0]


# --- per-session dedupe ------------------------------------------------------


@pytest.fixture
def injected_dir(tmp_path, monkeypatch):
    target = tmp_path / "injected"
    monkeypatch.setattr(hook, "INJECTED_DIR", target)
    return target


def test_dedupe_roundtrip(injected_dir):
    now = time.time()
    hook.save_injected("ses_1", {"a": now})
    assert hook.load_injected("ses_1") == {"a": pytest.approx(now)}


def test_dedupe_drops_entries_past_ttl(injected_dir):
    now = time.time()
    hook.save_injected("ses_1", {"old": now - hook.DEDUPE_TTL_S - 10, "new": now})
    assert set(hook.load_injected("ses_1")) == {"new"}


def test_dedupe_corrupt_file_reads_empty(injected_dir):
    injected_dir.mkdir(parents=True)
    (injected_dir / "ses_1.json").write_text("{not json")
    assert hook.load_injected("ses_1") == {}


def test_dedupe_empty_session_is_noop(injected_dir):
    hook.save_injected("", {"a": time.time()})
    assert hook.load_injected("") == {}
    assert not injected_dir.exists()


def test_dedupe_session_id_cannot_escape_dir(injected_dir):
    hook.save_injected("../evil", {"a": time.time()})
    written = list(injected_dir.iterdir())
    assert [p.name for p in written] == ["___evil.json"]
    assert not (injected_dir.parent / "evil.json").exists()


# --- main() end to end -------------------------------------------------------

SCHEMA = (
    "CREATE TABLE facts (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, last_reinforced_at TEXT NOT NULL, "
    "ttl_days INTEGER NOT NULL, expires_at TEXT NOT NULL, superseded_by TEXT)"
)


@pytest.fixture
def store(tmp_path, monkeypatch, injected_dir):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    now = datetime.now()
    future = (now + timedelta(days=30)).isoformat()
    rows = [(f"filler_{i}", f"unrelated value {i}") for i in range(200)]
    rows.append(("ranetka_PSIPHON_all_three_NL_nodes", "psiphon on every NL node"))
    conn.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?, 30, ?, NULL)",
        [(k, v, now.isoformat(), now.isoformat(), future) for k, v in rows],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(hook, "MEMORY_DB", db)
    semantic = {
        "facts": [
            _fact("ranetka_PSIPHON_all_three_NL_nodes", 0.47, value="psiphon on every NL node"),
            _fact("filler_3", 0.50),
        ],
        "semantic": [],
        "time_ms": 12,
    }
    monkeypatch.setattr(hook, "query_semantic", lambda message, n_facts=10, n_semantic=5: semantic)
    return db


def _run_main(monkeypatch, capsys, prompt: str, session_id: str) -> str:
    payload = json.dumps({"prompt": prompt, "cwd": "", "session_id": session_id})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    hook.main()
    return capsys.readouterr().out


def test_main_injects_anchored_fact_without_timing_line(store, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, "ranetka сифон на NL", "ses_main")
    assert "ranetka_PSIPHON_all_three_NL_nodes" in out
    assert "filler_3" not in out
    assert "Semantic search:" not in out


def test_main_does_not_repeat_fact_in_same_session(store, monkeypatch, capsys):
    _run_main(monkeypatch, capsys, "ranetka сифон на NL", "ses_main")
    out = _run_main(monkeypatch, capsys, "ranetka сифон на NL", "ses_main")
    assert "ranetka_PSIPHON_all_three_NL_nodes" not in out


# --- notifications, path/url noise, anchor gate ---------------------------------


def test_main_ignores_dcp_notification(store, injected_dir, monkeypatch, capsys):
    def no_query(*_args, **_kwargs):
        pytest.fail("a notification must not query the socket")

    monkeypatch.setattr(hook, "query_semantic", no_query)
    out = _run_main(
        monkeypatch, capsys, "▣ DCP | -160.2K removed, +18.2K summary\n\n│░░░█⣿⣿│", "ses_dcp"
    )
    assert out == ""
    assert not injected_dir.exists()


def test_extract_terms_drops_path_noise():
    terms = hook.extract_terms("/Users/mac/Documents/projects/dropweb-neya/dropweb.universal.wl.yml")
    assert terms == ["dropweb neya", "dropweb.universal.wl.yml"]


def test_extract_terms_drops_url_and_image_noise():
    terms = hook.extract_terms("[Image 1] https://github.com/ServerTechnologies/proxy-via-russian-cdn")
    assert terms == ["servertechnologies", "proxy via russian cdn"]


def test_anchor_terms_keeps_names_in_russian():
    assert hook.anchor_terms("ranetka сифон на NL") == ["ranetka", "nl"]


def test_anchor_terms_drops_long_latin_paste():
    paste = (
        "Configure the proxy server with tls certificates, rotate keys daily, "
        "monitor latency metrics, restart nginx gracefully, backup postgres database, "
        "enable firewall rules, audit kernel logs"
    )
    assert len(hook.extract_terms(paste)) >= 12
    assert hook.anchor_terms(paste) == []


def test_anchor_terms_keeps_many_names_in_russian_message():
    names = ["dropweb", "ranetka", "nl", "w1", "railway", "tailscale", "mihomo", "pena", "thready", "newsbot"]
    message = (
        "проверь сегодня вечером " + " и ".join(names)
        + " потом напиши коротко что сломалось где именно почему опять"
    )
    assert hook.anchor_terms(message) == names


# --- anchor_strengths rarest-terms cap ------------------------------------------

RARE_TERMS = [f"t{k}" for k in range(10)]
RARE_N = 20


def _rare_live() -> list[tuple[str, str, str]]:
    # Fact j mentions every tK with K >= j, so tK hits exactly K + 1 facts.
    pairs = [(f"k_{j}", " ".join(f"t{k}" for k in range(j, 10))) for j in range(10)]
    pairs += [(f"filler_{j}", "x") for j in range(RARE_N - 10)]
    return _live(*pairs)


def _expected_rare(ks: list[int]) -> dict[str, float]:
    expected: dict[str, float] = {}
    for k in ks:
        for j in range(k + 1):
            expected[f"k_{j}"] = expected.get(f"k_{j}", 0.0) + math.log(RARE_N / (k + 1)) / 2
    return expected


def test_anchor_strengths_keeps_rarest_terms():
    strengths = hook.anchor_strengths(RARE_TERMS[::-1], _rare_live(), max_terms=3)
    assert set(strengths) == {"k_0", "k_1", "k_2"}
    assert strengths == pytest.approx(_expected_rare([0, 1, 2]))


def test_anchor_strengths_under_cap_unchanged():
    terms = ["t5", "t2", "t7"]
    strengths = hook.anchor_strengths(terms, _rare_live(), max_terms=3)
    assert strengths == pytest.approx(_expected_rare([5, 2, 7]))


def test_anchor_strengths_ignores_zero_hit_terms_before_cap():
    strengths = hook.anchor_strengths(["zzz", "yyy", "t9"], _rare_live(), max_terms=1)
    assert strengths == pytest.approx(_expected_rare([9]))
