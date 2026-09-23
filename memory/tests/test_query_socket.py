"""Who answers the prompt hook, and when.

Every opencode window runs a server; one of them binds the hook socket. These
tests pin the three properties the hook depends on: a waiting server takes
over when the owner dies, the socket only appears once the owner is warm (so
an early hook fails at once instead of burning its 3 s timeout), and racing
first uses load the model once.
"""

import fcntl
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import db
import server


class _Encoder:
    def encode(self, texts, **_kwargs):
        if isinstance(texts, str):
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([[1.0, 0.0]] * len(texts), dtype=np.float32)


@pytest.fixture
def hook_socket(srv, monkeypatch):
    # tmp_path is too long for an AF_UNIX path on macOS (104 bytes).
    path = Path(f"/tmp/ocm-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
    lock = path.with_name(path.name + ".lock")
    monkeypatch.setattr(server, "QUERY_SOCKET", path)
    monkeypatch.setattr(server, "QUERY_SOCKET_RETRY_S", 0.05)
    monkeypatch.setattr(server, "get_encoder", _Encoder)
    stop = threading.Event()
    threads = []
    yield SimpleNamespace(
        path=path, lock=lock, start=lambda: threads.append(server._start_query_socket(stop))
    )
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    path.unlink(missing_ok=True)
    lock.unlink(missing_ok=True)


def _query(path: Path, text: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3.0)
        conn.connect(str(path))
        conn.sendall(json.dumps({"query": text, "n_facts": 5, "n_semantic": 0}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def _answer(path: Path, text: str, timeout: float = 5.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _query(path, text)
        except OSError:
            time.sleep(0.02)
    return None


def test_a_waiting_server_takes_over_when_the_owner_dies(hook_socket):
    db.fact_set("takeover_fact", {"value": "served by whoever holds the lock"})
    owner = os.open(hook_socket.lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a live server in another window
    try:
        hook_socket.start()
        time.sleep(0.3)  # six retry periods
        assert not hook_socket.path.exists()
    finally:
        os.close(owner)  # that window closes; the kernel drops its lock

    reply = _answer(hook_socket.path, "who serves")
    assert reply is not None
    assert [f["key"] for f in reply["facts"]] == ["takeover_fact"]



def test_the_socket_refuses_at_once_until_the_owner_is_warm(hook_socket, monkeypatch):
    db.fact_set("warm_fact", {"value": "answered only once the vectors exist"})
    # A dead owner's socket file is left behind, as after a crash or restart.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(hook_socket.path))
    stale.close()

    warming, release = threading.Event(), threading.Event()
    real_warm = server._warm_hook_path

    def slow_warm():
        warming.set()
        release.wait(5)
        real_warm()

    monkeypatch.setattr(server, "_warm_hook_path", slow_warm)
    hook_socket.start()
    assert warming.wait(5)

    started = time.monotonic()
    with pytest.raises(OSError):
        _query(hook_socket.path, "too early")
    assert time.monotonic() - started < 0.5

    release.set()
    reply = _answer(hook_socket.path, "now")
    assert reply is not None
    assert [f["key"] for f in reply["facts"]] == ["warm_fact"]


def test_racing_first_uses_load_the_model_once(srv, monkeypatch):
    loads = []

    class _SlowModel:
        def __init__(self, *_args, **_kwargs):
            loads.append(self)
            time.sleep(0.2)

    monkeypatch.setattr(server, "SentenceTransformer", _SlowModel)
    monkeypatch.setattr(server, "_encoder", None)
    got = []
    threads = [threading.Thread(target=lambda: got.append(server.get_encoder())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(loads) == 1
    assert all(model is loads[0] for model in got)
