"""Local memory browser: see, edit, retract and delete what the agent remembers.

    uv run python ui.py            # opens http://127.0.0.1:8377/?t=<token>

Anthropic's apps let you inspect and edit memory by topic; this is that for
this store. Binds to loopback only, rejects foreign Host headers (DNS
rebinding) and requires a per-run token on every API call, so a web page open
in the same browser cannot read or rewrite memory.

Writes go through db.py/volume.py, the same code the MCP server uses. Other
servers pick edits up on their own: fact vectors are keyed by updated_at.
Deletes are reversible: the row, its volume and a doc's text are appended to
``ui-trash.jsonl`` next to the database, and "Undo" restores the last one.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import volume

NOTES_DIR = Path(os.environ.get("OPENCODE_MEMORY_NOTES_DIR") or db.DB_PATH.parent / "notes")
TRASH = db.DB_PATH.parent / "ui-trash.jsonl"
PAGE = Path(__file__).with_name("ui.html")
LIST_LIMIT = 400


def _group(key: str) -> str:
    return key.casefold().partition("_")[0] or "other"


def _eff(key: str, parsed: dict) -> float:
    return round(volume.effective_volume("fact", key, parsed.get("last_reinforced_at")), 1)


def api_groups(_q: dict) -> dict:
    counts: dict[str, dict] = {}
    for key, parsed in db.fact_all().items():
        g = counts.setdefault(_group(key), {"name": _group(key), "facts": 0, "latest": ""})
        g["facts"] += 1
        g["latest"] = max(g["latest"], parsed.get("updated_at", ""))
    return {"groups": sorted(counts.values(), key=lambda g: (-g["facts"], g["name"]))}


def api_facts(q: dict) -> dict:
    group = q.get("group", "")
    needle = q.get("q", "").casefold()
    rows = []
    for key, parsed in db.fact_all().items():
        if group and _group(key) != group:
            continue
        value = str(parsed.get("value", ""))
        if needle and needle not in key.casefold() and needle not in value.casefold():
            continue
        rows.append(
            {
                "key": key,
                "updated_at": parsed.get("updated_at", ""),
                "preview": " ".join(value.split())[:160],
                "superseded_by": parsed.get("superseded_by"),
                "vol": _eff(key, parsed),
            }
        )
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"total": len(rows), "facts": rows[:LIST_LIMIT]}


def api_fact(q: dict) -> dict:
    key = q["key"]
    parsed = db.fact_get(key)
    if parsed is None:
        raise KeyError(key)
    return {
        "key": key,
        **parsed,
        "vol": _eff(key, parsed),
        "supersedes": db.fact_predecessors(key),
    }


def _trash(entry: dict) -> None:
    entry["trashed_at"] = datetime.now().isoformat()
    with TRASH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def post_fact_save(body: dict) -> dict:
    key, value = body["key"], body["value"]
    parsed = db.fact_get(key)
    if parsed is None:
        raise KeyError(key)
    parsed["value"] = value
    parsed["updated_at"] = datetime.now().isoformat()
    db.fact_set(key, parsed)
    return api_fact({"key": key})


def post_fact_delete(body: dict) -> dict:
    key = body["key"]
    parsed = db.fact_get(key)
    if parsed is None:
        raise KeyError(key)
    _trash({"kind": "fact", "key": key, "fact": parsed, "volume": volume.get_volume("fact", key),
            "predecessors": db.fact_predecessors(key)})
    with db.write_txn() as txn:
        db.fact_delete(key, conn=txn)
        db.volume_delete(volume.zset_key("fact", key), conn=txn)
    return {"deleted": key}


def post_fact_supersede(body: dict) -> dict:
    old, new = body["old"], body.get("new", "")
    db.fact_supersede(old, new or None)
    if new:
        volume.demote_superseded(old, new)
    return api_fact({"key": old})


def api_docs(q: dict) -> dict:
    folder = q.get("folder", "")
    if folder:
        path = _doc_dir(folder)
        docs = sorted(
            ({"name": f.stem, "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes"),
              "size": f.stat().st_size} for f in path.glob("*.md")),
            key=lambda d: d["mtime"], reverse=True,
        )
        return {"folder": folder, "docs": docs}
    folders = [
        {"name": d.name, "docs": sum(1 for _ in d.glob("*.md"))}
        for d in sorted(NOTES_DIR.iterdir()) if d.is_dir()
    ]
    return {"folders": [f for f in folders if f["docs"]]}


def _doc_dir(folder: str) -> Path:
    path = (NOTES_DIR / folder).resolve()
    if path.parent != NOTES_DIR.resolve():
        raise ValueError("bad folder")
    return path


def _doc_path(folder: str, name: str) -> Path:
    path = (_doc_dir(folder) / f"{name}.md").resolve()
    if path.parent != _doc_dir(folder):
        raise ValueError("bad name")
    return path


def api_doc(q: dict) -> dict:
    path = _doc_path(q["folder"], q["name"])
    if not path.exists():
        raise KeyError(q["name"])
    return {"folder": q["folder"], "name": q["name"], "content": path.read_text("utf-8")}


def post_doc_save(body: dict) -> dict:
    path = _doc_path(body["folder"], body["name"])
    if not path.exists():
        raise KeyError(body["name"])
    path.write_text(body["content"], "utf-8")
    return api_doc(body)


def post_doc_delete(body: dict) -> dict:
    path = _doc_path(body["folder"], body["name"])
    if not path.exists():
        raise KeyError(body["name"])
    _trash({"kind": "doc", "folder": body["folder"], "name": body["name"], "content": path.read_text("utf-8")})
    path.unlink()
    return {"deleted": f"{body['folder']}/{body['name']}"}


def post_undo(_body: dict) -> dict:
    if not TRASH.exists():
        return {"restored": None}
    lines = TRASH.read_text("utf-8").splitlines()
    if not lines:
        return {"restored": None}
    entry = json.loads(lines[-1])
    if entry["kind"] == "fact":
        with db.write_txn() as txn:
            db.fact_set(entry["key"], entry["fact"], conn=txn)
            volume.set_volume("fact", entry["key"], entry["volume"], conn=txn)
            if entry["fact"].get("superseded_by"):
                txn.execute("UPDATE facts SET superseded_by = ? WHERE key = ?",
                            (entry["fact"]["superseded_by"], entry["key"]))
            for old in entry.get("predecessors", []):
                txn.execute("UPDATE facts SET superseded_by = ? WHERE key = ? AND superseded_by IS NULL",
                            (entry["key"], old))
        restored = entry["key"]
    else:
        path = _doc_path(entry["folder"], entry["name"])
        path.write_text(entry["content"], "utf-8")
        restored = f"{entry['folder']}/{entry['name']}"
    TRASH.write_text("".join(line + "\n" for line in lines[:-1]), "utf-8")
    return {"restored": restored, "kind": entry["kind"]}


GET = {"/api/groups": api_groups, "/api/facts": api_facts, "/api/fact": api_fact,
       "/api/docs": api_docs, "/api/doc": api_doc}
POST = {"/api/fact/save": post_fact_save, "/api/fact/delete": post_fact_delete,
        "/api/fact/supersede": post_fact_supersede, "/api/doc/save": post_doc_save,
        "/api/doc/delete": post_doc_delete, "/api/undo": post_undo}


def make_handler(token: str, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:  # quiet terminal
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _guard(self) -> dict | None:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                return None
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                if not secrets.compare_digest(query.get("t", ""), token):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "missing token"})
                    return None
            elif not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing token"})
                return None
            return {"path": url.path, "query": query}

        def _run(self, fn, arg) -> None:
            try:
                self._json(HTTPStatus.OK, fn(arg))
            except KeyError as err:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"not found: {err}"})
            except ValueError as err:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(err)})

        def do_GET(self) -> None:
            req = self._guard()
            if req is None:
                return
            if req["path"] == "/":
                self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif req["path"] in GET:
                self._run(GET[req["path"]], req["query"])
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no route"})

        def do_POST(self) -> None:
            req = self._guard()
            if req is None:
                return
            if req["path"] not in POST:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no route"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            self._run(POST[req["path"]], body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse and edit agent memory")
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    db.init_schema()
    token = secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(token, args.port))
    url = f"http://127.0.0.1:{args.port}/?t={token}"
    print(f"Memory UI: {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
