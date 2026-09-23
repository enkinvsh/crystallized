"""Project scoping: which facts belong to the directory a session runs in.

Claude Code keeps one memory directory per repository and loads only that
one. This store is a single pool, so the project is derived instead: the
session's working directory names a repository, and the repository name
maps onto fact key prefixes (``dropweb-app`` -> ``dropweb_app_*``, plus the
``dropweb_*`` family it belongs to).

Stdlib only and read-only: the prompt hook and the opencode plugin call this
on the hot path, before the MCP server (and its encoder) is ever involved.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

MEMORY_DB = Path(
    os.environ.get(
        "OPENCODE_MEMORY_DB",
        str(Path.home() / ".config" / "opencode" / "memory" / "memory.db"),
    )
)
PROJECTS_ROOT = Path(
    os.environ.get("OPENCODE_PROJECTS_ROOT", str(Path.home() / "Documents" / "projects"))
)

#: One-time cost per session, paid in every later request of it.
INDEX_MAX_CHARS = 5000
_PREVIEW_CHARS = 90

_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalize(name: str) -> str:
    """Directory name -> key prefix: ``Dropweb-App.v2`` -> ``dropweb_app_v2``."""
    return _NON_WORD.sub("_", name.casefold()).strip("_")


def project_dir(cwd: str | Path) -> Path | None:
    """The directory that names the project, or None for a non-project cwd.

    Nearest ancestor that is a git root or sits directly under PROJECTS_ROOT;
    failing both, the cwd itself. Home, the projects root and / are not
    projects: a session there is a cross-project one.
    """
    path = Path(cwd).expanduser().resolve()
    blocked = {Path("/"), Path.home().resolve(), PROJECTS_ROOT.resolve()}
    if path in blocked:
        return None
    for candidate in (path, *path.parents):
        if candidate in blocked:
            break
        if (candidate / ".git").exists() or candidate.parent == PROJECTS_ROOT.resolve():
            return candidate
    return path


def _count(conn: sqlite3.Connection, prefix: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE key = ? OR key LIKE ? ESCAPE '\\'",
        (prefix, prefix.replace("_", "\\_") + "\\_%"),
    ).fetchone()
    return int(row[0])


def resolve_prefixes(cwd: str | Path, conn: sqlite3.Connection) -> list[str]:
    """Key prefixes of the cwd's project, most specific first; [] if none."""
    directory = project_dir(cwd)
    if directory is None:
        return []
    core = normalize(directory.name)
    if not core:
        return []
    family = core.split("_", 1)[0]
    return [p for p in dict.fromkeys((core, family)) if _count(conn, p) > 0]


def matches(key: str, prefixes: list[str]) -> bool:
    return any(key == p or key.startswith(p + "_") for p in prefixes)


def ro_conn(db_path: Path | None = None) -> sqlite3.Connection | None:
    path = db_path or MEMORY_DB
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def build_index(
    conn: sqlite3.Connection, prefixes: list[str], max_chars: int = INDEX_MAX_CHARS
) -> str:
    """One line per live fact of the project, newest first, within a budget.

    Superseded facts are left out: the fact that replaced them is listed, and
    its text names them. The core prefix is listed before the family so that
    ``dropweb-app`` facts are not crowded out by 1000 other ``dropweb`` ones.
    """
    if not prefixes:
        return ""
    try:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM facts WHERE superseded_by IS NULL "
            "ORDER BY updated_at DESC, key"
        ).fetchall()
    except sqlite3.OperationalError:  # store not migrated to v6 yet
        rows = conn.execute(
            "SELECT key, value, updated_at FROM facts ORDER BY updated_at DESC, key"
        ).fetchall()
    ranked: list[sqlite3.Row] = []
    for i in range(len(prefixes)):
        ranked += [r for r in rows if matches(r["key"], [prefixes[i]]) and not matches(r["key"], prefixes[:i])]
    if not ranked:
        return ""

    header = f"Project memory index: {', '.join(p + '_*' for p in prefixes)} · {len(ranked)} facts, newest first"
    lines: list[str] = []
    used = len(header)
    for r in ranked:
        line = f"- {r['key']} ({(r['updated_at'] or '')[5:10]}): {_one_line(r['value'] or '', _PREVIEW_CHARS)}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    rest = len(ranked) - len(lines)
    if rest:
        lines.append(f"… {rest} older: list_facts(prefix=\"{prefixes[0]}_\")")
    return "\n".join([header, *lines])


def main(argv: list[str]) -> int:
    """``projects.py index <cwd>`` / ``projects.py prefixes <cwd>``. Never fails loudly."""
    if len(argv) != 3 or argv[1] not in {"index", "prefixes"}:
        print("usage: projects.py index|prefixes <cwd>", file=sys.stderr)
        return 2
    conn = ro_conn()
    if conn is None:
        return 0
    try:
        prefixes = resolve_prefixes(argv[2], conn)
        out = build_index(conn, prefixes) if argv[1] == "index" else " ".join(prefixes)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
