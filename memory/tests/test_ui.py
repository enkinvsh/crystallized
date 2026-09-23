"""HTTP tests for the local memory browser (ui.py)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import db
import ui
import volume

TOKEN = "test-token"


@pytest.fixture
def app(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "memory.db")
    db.init_schema()
    notes = tmp_path / "notes"
    (notes / "plans").mkdir(parents=True)
    (notes / "plans" / "roadmap.md").write_text("step one", "utf-8")
    monkeypatch.setattr(ui, "NOTES_DIR", notes)
    monkeypatch.setattr(ui, "TRASH", tmp_path / "ui-trash.jsonl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(TOKEN, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = ui.make_handler(TOKEN, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    db.close_db()


def call(base, path, body=None, token=TOKEN, host=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode())
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_rejects_missing_token_and_foreign_host(app):
    assert call(app, "/api/groups", token="")[0] == 403
    assert call(app, "/api/groups", token="wrong")[0] == 403
    assert call(app, "/api/groups", host="evil.example:80")[0] == 403
    assert call(app, "/?t=nope", token="")[0] == 403


def test_page_needs_token_in_url(app):
    with urllib.request.urlopen(app + f"/?t={TOKEN}") as res:
        assert b"<title>" in res.read()


def test_list_edit_supersede_delete_undo(app):
    db.fact_set("proj_old", {"value": "old claim"})
    db.fact_set("proj_new", {"value": "the correction"})
    volume.set_volume("fact", "proj_old", 60.0)

    status, groups = call(app, "/api/groups")
    assert status == 200 and groups["groups"][0] == {"name": "proj", "facts": 2, "latest": groups["groups"][0]["latest"]}
    assert call(app, "/api/facts?q=correction")[1]["total"] == 1

    status, fact = call(app, "/api/fact/save", {"key": "proj_new", "value": "the correction, v2"})
    assert status == 200 and fact["value"] == "the correction, v2"

    status, fact = call(app, "/api/fact/supersede", {"old": "proj_old", "new": "proj_new"})
    assert fact["superseded_by"] == "proj_new"
    assert call(app, "/api/fact?key=proj_new")[1]["supersedes"] == ["proj_old"]
    assert call(app, "/api/fact/supersede", {"old": "proj_old", "new": "missing_key"})[0] == 400

    assert call(app, "/api/fact/delete", {"key": "proj_new"})[0] == 200
    assert db.fact_get("proj_new") is None
    assert "superseded_by" not in db.fact_get("proj_old")  # deleting the correction revives it
    status, undo = call(app, "/api/undo", {})
    assert undo["restored"] == "proj_new"
    assert db.fact_get("proj_new")["value"] == "the correction, v2"
    assert db.fact_get("proj_old")["superseded_by"] == "proj_new"


def test_docs_edit_delete_undo_and_no_traversal(app):
    assert call(app, "/api/docs")[1]["folders"] == [{"name": "plans", "docs": 1}]
    assert call(app, "/api/doc?folder=plans&name=roadmap")[1]["content"] == "step one"
    assert call(app, "/api/doc/save", {"folder": "plans", "name": "roadmap", "content": "step two"})[0] == 200
    assert call(app, "/api/doc?folder=..&name=memory")[0] in (400, 404)
    assert call(app, "/api/doc?folder=plans&name=../../x")[0] in (400, 404)
    assert call(app, "/api/doc/delete", {"folder": "plans", "name": "roadmap"})[0] == 200
    assert call(app, "/api/undo", {})[1]["restored"] == "plans/roadmap"
    assert call(app, "/api/doc?folder=plans&name=roadmap")[1]["content"] == "step two"
