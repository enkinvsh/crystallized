"""Doc layer: filesystem-backed markdown notes (volume-aware).

Tests characterize the actual public surface in server.py at the time of
writing:

    save_doc(folder, name, content) -> str
    read_doc(folder, name) -> str
    list_docs(folder="") -> str
    delete_doc(folder, name) -> str

Behavior gaps versus plan intent (recorded in issues.md if any):

- Plan intent: "save writes markdown, read roundtrips, list finds saved
  docs, delete removes file." All four hold for the current server.py.
- Plan-spec test calls `list_docs()` with no argument and expects both
  document names in the output. Current implementation lists folders +
  their docs in that case, so the names appear in the output and the
  intent matches. No production change required.

Hermeticity: no real Redis, no real ChromaDB, no network, no
~/.config/opencode writes. Shared conftest fixtures (memory_module,
fake_redis, mock_chroma, mock_encoder, temp_memory_root) provide the
sandbox; conftest already sets `OPENCODE_MEMORY_NOTES_DIR` to redirect
server.py's module-level NOTES_DIR (Task 3.4), and each test additionally
monkeypatches `memory_module.NOTES_DIR` as belt-and-suspenders local
isolation since the module is already imported by the time the test runs.
"""

from pathlib import Path

import pytest


@pytest.fixture
def docs_server(memory_module, temp_memory_root, monkeypatch):
    """memory_module with NOTES_DIR redirected into the tempdir.

    Pattern mirrors test_semantic.py's semantic_server: reload server.py
    via the shared memory_module fixture, then point its module-level
    NOTES_DIR at the per-test tempdir so save_doc / read_doc / list_docs
    / delete_doc never touch real ~/.config/opencode/memory/notes.
    """
    notes = temp_memory_root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory_module, "NOTES_DIR", notes)
    return memory_module


# ---------------------------------------------------------------------------
# save_doc()
# ---------------------------------------------------------------------------


def test_save_doc_writes_markdown(docs_server, temp_memory_root):
    """save_doc creates folder/name.md under NOTES_DIR with given content."""
    docs_server.save_doc(
        folder="architecture", name="decision-001", content="# Title\n\nBody."
    )
    notes = Path(temp_memory_root) / "notes"
    found = list(notes.rglob("decision-001.md"))
    assert len(found) == 1
    assert "Body." in found[0].read_text(encoding="utf-8")


def test_save_doc_creates_folder(docs_server, temp_memory_root):
    """save_doc auto-creates the folder if it does not exist."""
    notes = Path(temp_memory_root) / "notes"
    assert not (notes / "fresh-folder").exists()
    docs_server.save_doc(folder="fresh-folder", name="entry", content="hi")
    assert (notes / "fresh-folder").is_dir()
    assert (notes / "fresh-folder" / "entry.md").exists()


def test_save_doc_returns_user_visible_confirmation(docs_server):
    """save_doc returns a string mentioning the path and char count."""
    result = docs_server.save_doc(
        folder="contexts", name="current", content="hello world"
    )
    assert isinstance(result, str)
    assert "contexts/current" in result


def test_save_doc_overwrites(docs_server, temp_memory_root):
    """Re-saving the same folder/name replaces the file contents."""
    docs_server.save_doc(folder="notes", name="memo", content="first")
    docs_server.save_doc(folder="notes", name="memo", content="second")
    text = (Path(temp_memory_root) / "notes" / "notes" / "memo.md").read_text(
        encoding="utf-8"
    )
    assert text == "second"


# ---------------------------------------------------------------------------
# read_doc()
# ---------------------------------------------------------------------------


def test_read_doc_roundtrip(docs_server):
    """Content written by save_doc is recoverable via read_doc."""
    docs_server.save_doc("contexts", "current", "x")
    content = docs_server.read_doc("contexts", "current")
    assert "x" in str(content)


def test_read_doc_missing_returns_message(docs_server):
    """Reading a non-existent doc returns a not-found string, not a crash."""
    result = docs_server.read_doc("nope", "nothing")
    assert isinstance(result, str)
    assert "not found" in result.lower()


def test_read_doc_preserves_content_exactly(docs_server):
    """read_doc returns the exact bytes written (utf-8)."""
    body = "# Heading\n\nParagraph with unicode: тест ✓\n"
    docs_server.save_doc("ru", "note", body)
    assert docs_server.read_doc("ru", "note") == body


# ---------------------------------------------------------------------------
# list_docs()
# ---------------------------------------------------------------------------


def test_list_docs_finds_saved(docs_server):
    """list_docs() with no folder lists all saved doc names across folders."""
    docs_server.save_doc("notes", "a", "1")
    docs_server.save_doc("notes", "b", "2")
    result = docs_server.list_docs()
    text = str(result)
    assert "a" in text and "b" in text


def test_list_docs_specific_folder(docs_server):
    """list_docs(folder) lists only that folder's docs."""
    docs_server.save_doc("alpha", "one", "x")
    docs_server.save_doc("beta", "two", "y")
    result = docs_server.list_docs("alpha")
    assert "one" in result
    assert "two" not in result


def test_list_docs_empty_returns_message(docs_server):
    """With nothing saved, list_docs() returns a recognizable empty message."""
    result = docs_server.list_docs()
    assert isinstance(result, str)
    assert "no documents" in result.lower()


def test_list_docs_unknown_folder_returns_message(docs_server):
    """list_docs('missing') returns a not-found message, not a crash."""
    result = docs_server.list_docs("missing")
    assert isinstance(result, str)
    assert "not found" in result.lower() or "missing" in result.lower()


# ---------------------------------------------------------------------------
# delete_doc()
# ---------------------------------------------------------------------------


def test_delete_doc_removes_file(docs_server, temp_memory_root):
    """delete_doc removes the file from disk."""
    docs_server.save_doc("trash", "tmp", "content")
    docs_server.delete_doc("trash", "tmp")
    notes = Path(temp_memory_root) / "notes"
    assert list(notes.rglob("tmp.md")) == []


def test_delete_doc_missing_returns_message(docs_server):
    """Deleting a non-existent doc returns a not-found string."""
    result = docs_server.delete_doc("nope", "nothing")
    assert isinstance(result, str)
    assert "not found" in result.lower()


def test_delete_doc_returns_confirmation(docs_server):
    """delete_doc returns a string confirming what was removed."""
    docs_server.save_doc("trash", "doomed", "x")
    result = docs_server.delete_doc("trash", "doomed")
    assert isinstance(result, str)
    assert "trash/doomed" in result
