"""Env-var configuration for runtime (Task 3.1: configurable Redis)."""

import importlib
import sys
from pathlib import Path


def _reload_server():
    server_dir = str(Path(__file__).parent.parent)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    return importlib.reload(server)


def test_redis_url_env_used(monkeypatch, fake_redis):
    """get_redis() must use redis.Redis.from_url when REDIS_URL is set."""
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6380/2")
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    monkeypatch.delenv("REDIS_DB", raising=False)

    import redis

    called: dict = {}

    def spy_from_url(url, **kwargs):
        called["url"] = url
        called["kwargs"] = kwargs
        return fake_redis

    monkeypatch.setattr(redis.Redis, "from_url", spy_from_url, raising=False)

    server = _reload_server()
    r = server.get_redis()

    assert r is not None
    assert called.get("url") == "redis://example.invalid:6380/2"
    assert called.get("kwargs", {}).get("decode_responses") is True


def test_redis_default_unchanged(monkeypatch, fake_redis):
    """Without env vars, get_redis() falls back to localhost:6379 db 0."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    monkeypatch.delenv("REDIS_DB", raising=False)

    import redis

    called: dict = {}

    class TrackingStub:
        def __new__(cls, *args, **kwargs):
            called["args"] = args
            called["kwargs"] = kwargs
            return fake_redis

    monkeypatch.setattr(redis, "Redis", TrackingStub)

    server = _reload_server()
    r = server.get_redis()

    assert r is not None
    kw = called.get("kwargs", {})
    assert kw.get("host", "localhost") == "localhost"
    assert int(kw.get("port", 6379)) == 6379
    assert int(kw.get("db", 0)) == 0
    assert kw.get("decode_responses") is True


def test_redis_host_port_db_env_used(monkeypatch, fake_redis):
    """REDIS_HOST / REDIS_PORT / REDIS_DB override the defaults when REDIS_URL is unset."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "myhost.invalid")
    monkeypatch.setenv("REDIS_PORT", "6381")
    monkeypatch.setenv("REDIS_DB", "5")

    import redis

    called: dict = {}

    class TrackingStub:
        def __new__(cls, *args, **kwargs):
            called["args"] = args
            called["kwargs"] = kwargs
            return fake_redis

    monkeypatch.setattr(redis, "Redis", TrackingStub)

    server = _reload_server()
    r = server.get_redis()

    assert r is not None
    kw = called.get("kwargs", {})
    assert kw.get("host") == "myhost.invalid"
    assert kw.get("port") == 6381
    assert kw.get("db") == 5
    assert kw.get("decode_responses") is True


def test_socket_path_env_override(monkeypatch, fake_redis):
    """OPENCODE_MEMORY_SOCKET overrides QUERY_SOCKET in both server.py and memory-inject.py."""
    import runpy

    custom = "/tmp/custom-opencode-memory-query.sock"
    monkeypatch.setenv("OPENCODE_MEMORY_SOCKET", custom)

    server = _reload_server()
    assert isinstance(server.QUERY_SOCKET, Path)
    assert str(server.QUERY_SOCKET) == custom

    # memory-inject.py has a hyphen in the filename; runpy.run_path lets us
    # read its module-level QUERY_SOCKET without importing.
    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_socket_test__")
    assert ns["QUERY_SOCKET"] == custom


def test_socket_path_default_unchanged(monkeypatch, fake_redis):
    """Without OPENCODE_MEMORY_SOCKET, QUERY_SOCKET keeps its legacy default."""
    import runpy

    monkeypatch.delenv("OPENCODE_MEMORY_SOCKET", raising=False)

    server = _reload_server()
    assert str(server.QUERY_SOCKET) == "/tmp/opencode-memory-query.sock"

    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_socket_default_test__")
    assert ns["QUERY_SOCKET"] == "/tmp/opencode-memory-query.sock"


def test_inject_honors_redis_env(monkeypatch, fake_redis):
    """memory-inject.py reads REDIS_URL too and exits cleanly when Redis is unreachable."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6380/3")
    hook = Path(__file__).parent.parent / "memory-inject.py"
    result = subprocess.run(
        [sys.executable, str(hook)],
        input='{"prompt":"hi"}',
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "OPENCODE_MEMORY_SOCKET": "/tmp/nope.sock"},
    )
    # With Redis unreachable in subprocess and no socket, the hook must still
    # exit cleanly (best-effort fallback).
    assert result.returncode == 0


def test_notes_dir_env_override(monkeypatch, tmp_path, fake_redis):
    """OPENCODE_MEMORY_NOTES_DIR overrides NOTES_DIR in server.py and memory-inject.py."""
    import runpy

    custom = tmp_path / "alt-notes"
    monkeypatch.setenv("OPENCODE_MEMORY_NOTES_DIR", str(custom))

    server = _reload_server()
    assert isinstance(server.NOTES_DIR, Path)
    assert str(server.NOTES_DIR) == str(custom)

    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_notes_test__")
    assert str(ns["NOTES_DIR"]) == str(custom)


def test_notes_dir_default_unchanged(monkeypatch, fake_redis):
    """Without OPENCODE_MEMORY_NOTES_DIR, NOTES_DIR keeps its legacy default."""
    import runpy

    monkeypatch.delenv("OPENCODE_MEMORY_NOTES_DIR", raising=False)

    default = Path.home() / ".config" / "opencode" / "memory" / "notes"

    server = _reload_server()
    assert str(server.NOTES_DIR) == str(default)

    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_notes_default_test__")
    assert str(ns["NOTES_DIR"]) == str(default)


def test_chroma_dir_env_override(monkeypatch, tmp_path, fake_redis):
    """OPENCODE_MEMORY_CHROMA_DIR overrides CHROMA_DIR in server.py and memory-inject.py."""
    import runpy

    custom = tmp_path / "alt-chroma"
    monkeypatch.setenv("OPENCODE_MEMORY_CHROMA_DIR", str(custom))

    server = _reload_server()
    assert isinstance(server.CHROMA_DIR, Path)
    assert str(server.CHROMA_DIR) == str(custom)

    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_chroma_test__")
    assert str(ns["CHROMA_DIR"]) == str(custom)


def test_chroma_dir_default_unchanged(monkeypatch, fake_redis):
    """Without OPENCODE_MEMORY_CHROMA_DIR, CHROMA_DIR keeps its legacy default."""
    import runpy

    monkeypatch.delenv("OPENCODE_MEMORY_CHROMA_DIR", raising=False)

    default = Path.home() / ".config" / "opencode" / "memory" / "chroma_db"

    server = _reload_server()
    assert str(server.CHROMA_DIR) == str(default)

    hook_path = Path(__file__).parent.parent / "memory-inject.py"
    ns = runpy.run_path(str(hook_path), run_name="__memory_inject_chroma_default_test__")
    assert str(ns["CHROMA_DIR"]) == str(default)


def test_own_voice_notes_dir_env_override(monkeypatch, tmp_path):
    """own-voice.py derives NOTES_DIR / SELF_DIR / JOURNAL_DIR from OPENCODE_MEMORY_NOTES_DIR."""
    import runpy

    custom = tmp_path / "alt-notes"
    monkeypatch.setenv("OPENCODE_MEMORY_NOTES_DIR", str(custom))

    hook_path = Path(__file__).parent.parent / "own-voice.py"
    ns = runpy.run_path(str(hook_path), run_name="__own_voice_notes_test__")
    assert str(ns["NOTES_DIR"]) == str(custom)
    assert str(ns["SELF_DIR"]) == str(custom / "self")
    assert str(ns["JOURNAL_DIR"]) == str(custom / "journal")


def test_own_voice_notes_dir_default_unchanged(monkeypatch):
    """Without OPENCODE_MEMORY_NOTES_DIR, own-voice.py keeps its legacy default."""
    import runpy

    monkeypatch.delenv("OPENCODE_MEMORY_NOTES_DIR", raising=False)

    default = Path.home() / ".config" / "opencode" / "memory" / "notes"

    hook_path = Path(__file__).parent.parent / "own-voice.py"
    ns = runpy.run_path(str(hook_path), run_name="__own_voice_notes_default_test__")
    assert str(ns["NOTES_DIR"]) == str(default)
    assert str(ns["SELF_DIR"]) == str(default / "self")
    assert str(ns["JOURNAL_DIR"]) == str(default / "journal")
