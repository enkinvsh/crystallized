"""Pre-prompt memory injection hook tests."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "memory-inject.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("memory_inject", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inject = _load_hook_module()


def _run_hook(env, prompt):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _populated_env(tmp_path, folders=130, docs_per_folder=8):
    notes_dir = tmp_path / "notes"
    for i in range(folders):
        folder = notes_dir / f"folder{i:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for j in range(docs_per_folder):
            (folder / f"doc{j}.md").write_text("body", "utf-8")
    return {
        **os.environ,
        "OPENCODE_MEMORY_DB": str(tmp_path / "memory.db"),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }


def test_hook_runs_without_socket(tmp_path):
    """No /tmp/opencode-memory-query.sock -> hook should still exit 0."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "memory.db"
    env = {
        **os.environ,
        "OPENCODE_MEMORY_DB": str(db_path),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": "what does Oen think of dark mode"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    # Output should contain a [Clock] / [Memory] block or be empty (graceful fallback).
    assert "[Clock]" in result.stdout or "[Memory]" in result.stdout or result.stdout.strip() == ""


def test_hook_handles_empty_prompt(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "memory.db"
    env = {
        **os.environ,
        "OPENCODE_MEMORY_DB": str(db_path),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": ""}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0


class TestDocSection:
    def test_docs_are_summarized_not_enumerated(self, tmp_path):
        env = _populated_env(tmp_path)
        result = _run_hook(env, "what did we decide about dark mode")
        assert result.returncode == 0
        doc_lines = [ln for ln in result.stdout.splitlines() if "Docs" in ln]
        assert len(doc_lines) == 1
        assert "1040" in doc_lines[0] and "130" in doc_lines[0]
        assert "folder000" not in result.stdout

    def test_doc_stats_count_without_listing(self, tmp_path, monkeypatch):
        notes = tmp_path / "notes"
        (notes / "architecture").mkdir(parents=True)
        (notes / "architecture" / "a.md").write_text("x", "utf-8")
        (notes / "architecture" / "b.md").write_text("x", "utf-8")
        (notes / "empty").mkdir(parents=True)
        monkeypatch.setattr(inject, "NOTES_DIR", notes)
        assert inject.get_doc_stats() == (1, 2)

    def test_missing_notes_dir_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inject, "NOTES_DIR", tmp_path / "notes")
        assert inject.get_doc_stats() == (0, 0)


class TestTokenBudget:
    def test_estimate_is_an_upper_bound_on_cyrillic(self):
        text = "не трогай конфиг, я же просил" * 20
        assert inject.estimate_tokens(text) >= len(text) * 0.4

    def test_payload_fits_budget_with_a_large_store(self, tmp_path):
        env = _populated_env(tmp_path)
        result = _run_hook(env, "как мы калибровали observer и что решили по докам")
        assert result.returncode == 0
        assert inject.estimate_tokens(result.stdout) <= inject.MAX_INJECT_TOKENS

    @pytest.mark.parametrize("budget", [120, 300, 450])
    def test_fit_to_budget_trims_the_widest_section(self, budget):
        fitted = inject.fit_to_budget(self._wide_sections(), max_tokens=budget)
        assert inject.estimate_tokens("\n".join(fitted)) <= budget
        assert fitted[0].startswith("[Clock]")
        assert any(ln.startswith("[Memory] Relevant facts") for ln in fitted)
        assert fitted[-1] == inject.TRIM_MARKER

    def test_headers_are_a_floor_never_dropped(self):
        sections = self._wide_sections()
        fitted = inject.fit_to_budget(sections, max_tokens=1)
        assert [s.split("\n")[0] for s in sections] == [s for s in fitted[:-1]]
        assert fitted[-1] == inject.TRIM_MARKER

    @staticmethod
    def _wide_sections() -> list[str]:
        return [
            "[Clock] 2026-08-22 09:00 Sat +05:00 (UTC 04:00)",
            "[Memory] Relevant facts (60):\n"
            + "\n".join(f"  key{i}: value{i}" for i in range(60)),
            "[Memory] recall(query) for deep search.",
        ]

    def test_fit_to_budget_is_a_noop_when_already_small(self):
        sections = ["[Clock] now", "[Memory] tiny"]
        assert inject.fit_to_budget(sections) == sections

    def test_a_long_single_line_section_does_not_stop_the_pass(self):
        """The widest section can be an untrimmable one-liner.

        Treating that as the end of the pass stranded detail lines in every
        other section — the failure the widened clock header exposed.
        """
        sections = [
            "[Clock] " + "x" * 200,
            "[Memory] facts:\n" + "\n".join(f"  key{i}: value{i}" for i in range(40)),
        ]
        fitted = inject.fit_to_budget(sections, max_tokens=1)
        assert fitted[:-1] == [s.split("\n")[0] for s in sections]
        assert fitted[-1] == inject.TRIM_MARKER

    def test_trimming_is_deterministic_across_repeated_runs(self):
        runs = {
            tuple(inject.fit_to_budget(self._wide_sections(), max_tokens=200))
            for _ in range(20)
        }
        assert len(runs) == 1


class TestClockLine:
    """The header must say which clock it is, in both of the store's conventions.

    ``facts``/``docs``/``events`` carry naive local stamps; ``causal_memories``
    and ``belief_state`` carry UTC-aware ones. The header used to print UTC only,
    so a 09:10 fact and the 04:10 causal row written in the same second looked
    five hours apart.
    """

    _INSTANT = datetime(2026, 8, 22, 4, 10, tzinfo=UTC)

    def test_shows_local_wall_clock_and_utc_for_the_same_instant(self):
        line = inject.clock_line(self._INSTANT)
        assert line.startswith("[Clock] ")
        assert f"{self._INSTANT.astimezone():%Y-%m-%d %H:%M %a}" in line
        assert f"(UTC {self._INSTANT:%H:%M})" in line

    def test_offset_is_written_as_signed_hh_mm(self):
        assert re.search(r" [+-]\d{2}:\d{2} \(UTC \d{2}:\d{2}\)$", inject.clock_line(self._INSTANT))

    def test_a_naive_stamp_is_read_as_local_and_never_shifted(self):
        naive = self._INSTANT.astimezone().replace(tzinfo=None)
        assert inject.clock_line(naive) == inject.clock_line(self._INSTANT)

    def test_every_representation_of_one_instant_yields_one_line(self):
        elsewhere = self._INSTANT.astimezone(timezone(timedelta(hours=9)))
        assert inject.clock_line(elsewhere) == inject.clock_line(self._INSTANT)

    def test_defaults_to_the_current_moment(self):
        assert inject.clock_line().startswith("[Clock] ")

    def test_the_header_the_hook_emits_carries_both_clocks(self, tmp_path):
        env = _populated_env(tmp_path)
        result = _run_hook(env, "какая сейчас память")
        assert result.returncode == 0
        header = result.stdout.splitlines()[0]
        assert re.match(r"^\[Clock\] \d{4}-\d{2}-\d{2} \d{2}:\d{2} \w{3} [+-]\d{2}:\d{2} \(UTC \d{2}:\d{2}\)$", header)
