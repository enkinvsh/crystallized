"""Cross-layer orientation snapshot: memory_context().

Tests characterize the compact-output behavior of memory_context() in
server.py: long fact values are truncated to a single-line preview, and
documents are summarized one line per folder with a name teaser instead
of one line per document.

Hermeticity: no real Redis, no real ChromaDB, no network, no
~/.config/opencode writes. The shared conftest fixtures (memory_module,
fake_redis, mock_chroma, mock_encoder, temp_memory_root) provide the
sandbox; this module redirects server.py's module-level NOTES_DIR at the
per-test tempdir, mirroring test_docs.py's docs_server fixture, so the
facts (Redis) and documents (filesystem) layers are both exercised
without touching real state. The semantic layer is left to fall back to
its "ChromaDB unavailable" branch and is not asserted on here.
"""

import pytest

PREVIEW_LIMIT = 160


@pytest.fixture
def context_server(memory_module, temp_memory_root, monkeypatch):
    """memory_module with NOTES_DIR redirected into the tempdir.

    Same wiring as test_docs.py's docs_server: reload server.py via the
    shared memory_module fixture (which neutralizes Redis/Chroma/encoder),
    then point module-level NOTES_DIR at the per-test tempdir so
    save_fact / save_doc / memory_context never touch real state.
    """
    notes = temp_memory_root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory_module, "NOTES_DIR", notes)
    return memory_module


# ---------------------------------------------------------------------------
# Fact previews (PATCH A)
# ---------------------------------------------------------------------------


def test_long_fact_value_is_truncated_to_preview(context_server):
    """A fact value over the limit shows a bounded preview plus a chars suffix."""
    long_value = "x" * 500
    context_server.save_fact("big_fact", long_value)
    text = str(context_server.memory_context())

    line = next(ln for ln in text.splitlines() if ln.strip().startswith("big_fact:"))
    value_part = line.split("big_fact:", 1)[1].strip()

    assert "[+340 chars, recall/list_facts for full]" in value_part
    # Preview body (before the suffix) is capped at the limit.
    body = value_part.split("…", 1)[0]
    assert len(body) <= PREVIEW_LIMIT
    assert long_value not in text


def test_fact_preview_collapses_newlines(context_server):
    """Newlines in a long fact value become spaces in the preview (single line)."""
    multiline = "first line\nsecond line\r\nthird line " + ("y" * 300)
    context_server.save_fact("multi_fact", multiline)
    text = str(context_server.memory_context())

    line = next(ln for ln in text.splitlines() if ln.strip().startswith("multi_fact:"))
    value_part = line.split("multi_fact:", 1)[1]
    assert "\n" not in value_part
    assert "first line second line third line" in value_part


def test_short_fact_value_is_unchanged(context_server):
    """A fact value at or under the limit appears verbatim, no suffix."""
    short_value = "Oen, prefers terse output"
    context_server.save_fact("user_pref", short_value)
    text = str(context_server.memory_context())

    line = next(ln for ln in text.splitlines() if ln.strip().startswith("user_pref:"))
    assert short_value in line
    assert "chars, recall/list_facts" not in line


# ---------------------------------------------------------------------------
# Compact document tree (PATCH B)
# ---------------------------------------------------------------------------


def test_folder_with_many_docs_is_one_line_with_teaser(context_server):
    """A folder with >3 docs is summarized on one line: 3 names + '+N more'."""
    for i in range(7):
        context_server.save_doc("journal", f"entry-{i:02d}", "body")
    text = str(context_server.memory_context())

    folder_lines = [ln for ln in text.splitlines() if ln.strip().startswith("journal/")]
    assert len(folder_lines) == 1
    line = folder_lines[0]
    assert "journal/ (7 docs):" in line
    assert "entry-00, entry-01, entry-02" in line
    assert "+4 more" in line
    # Only the 3 teaser names are shown; later docs are not enumerated.
    assert "entry-05" not in text
    assert "entry-06" not in text


def test_documents_section_has_hint_line(context_server):
    """The documents section ends with the list_docs/read_doc hint."""
    context_server.save_doc("notes", "a", "1")
    text = str(context_server.memory_context())
    assert "(use list_docs(folder) for full listing, read_doc to open)" in text


def test_documents_header_shows_total_count(context_server):
    """The Documents header counts every doc across folders, not folders."""
    context_server.save_doc("alpha", "one", "x")
    context_server.save_doc("alpha", "two", "y")
    context_server.save_doc("beta", "three", "z")
    text = str(context_server.memory_context())
    assert "Documents (3):" in text
