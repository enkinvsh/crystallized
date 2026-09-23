"""Tests for cwd -> project scoping and the per-project index."""

import sqlite3

import pytest

import projects


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(projects, "PROJECTS_ROOT", root)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE facts (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, superseded_by TEXT)")

    def add(key, value="v", updated="2026-09-01T00:00:00", superseded_by=None):
        conn.execute("INSERT INTO facts VALUES (?, ?, ?, ?)", (key, value, updated, superseded_by))

    return root, conn, add


def test_normalize():
    assert projects.normalize("Dropweb-App.v2") == "dropweb_app_v2"
    assert projects.normalize("PENA") == "pena"


def test_project_dir_walks_up_to_the_repo(store):
    root, _, _ = store
    repo = root / "dropweb-app"
    (repo / ".git").mkdir(parents=True)
    (repo / "lib" / "src").mkdir(parents=True)
    assert projects.project_dir(repo / "lib" / "src") == repo.resolve()


def test_project_dir_non_git_folder_under_root(store):
    root, _, _ = store
    (root / "dropweb" / "public").mkdir(parents=True)
    assert projects.project_dir(root / "dropweb" / "public") == (root / "dropweb").resolve()


def test_projects_root_and_home_are_not_projects(store):
    root, _, _ = store
    assert projects.project_dir(root) is None
    assert projects.project_dir(projects.Path.home()) is None


def test_prefixes_core_then_family_only_when_present(store):
    root, conn, add = store
    (root / "dropweb-app").mkdir()
    (root / "thready-prod").mkdir()
    (root / "nothing-here").mkdir()
    add("dropweb_app_x")
    add("dropweb_y")
    add("thready_z")
    assert projects.resolve_prefixes(root / "dropweb-app", conn) == ["dropweb_app", "dropweb"]
    assert projects.resolve_prefixes(root / "thready-prod", conn) == ["thready"]
    assert projects.resolve_prefixes(root / "nothing-here", conn) == []
    assert projects.resolve_prefixes(root, conn) == []


def test_prefix_is_a_whole_segment(store):
    root, conn, add = store
    (root / "pena").mkdir()
    add("penalty_rules")
    assert projects.resolve_prefixes(root / "pena", conn) == []


def test_index_core_first_skips_superseded_and_respects_budget(store):
    _, conn, add = store
    add("dropweb_old_news", "family", "2026-09-10T00:00:00")
    add("dropweb_app_new", "core fact", "2026-09-01T00:00:00")
    add("dropweb_app_gone", "retracted", "2026-09-05T00:00:00", superseded_by="dropweb_app_new")
    idx = projects.build_index(conn, ["dropweb_app", "dropweb"])
    lines = idx.splitlines()
    assert lines[1].startswith("- dropweb_app_new (09-01): core fact")
    assert lines[2].startswith("- dropweb_old_news")
    assert "dropweb_app_gone" not in idx

    for i in range(200):
        add(f"dropweb_app_bulk_{i:03d}", "x" * 200, f"2026-08-{(i % 28) + 1:02d}T00:00:00")
    big = projects.build_index(conn, ["dropweb_app"], max_chars=2000)
    assert len(big) <= 2000 + 80
    assert big.splitlines()[-1].startswith("… ")


def test_index_empty_without_prefixes(store):
    _, conn, _ = store
    assert projects.build_index(conn, []) == ""
