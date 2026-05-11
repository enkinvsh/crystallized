"""Volume decay: V_eff = V_stored * (1 + t_hours / tau)^(-alpha).

Tests the pure decay helper extracted from server.py. The helper must be a
top-level function so it can be tested without touching Redis, ChromaDB,
or the filesystem.
"""

import math


def test_decay_at_t_zero_returns_input(memory_module):
    # If the helper is named differently, adjust the import after first failure.
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume", None
    )
    assert fn is not None, "expected a decay helper in server.py"
    v = fn(stored=50.0, t_hours=0.0, layer="fact")
    assert math.isclose(v, 50.0, rel_tol=1e-6)


def test_decay_monotonic_decreasing(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    v1 = fn(stored=50.0, t_hours=24.0, layer="fact")
    v2 = fn(stored=50.0, t_hours=240.0, layer="fact")
    assert v2 < v1 < 50.0


def test_decay_respects_floor(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    v = fn(stored=50.0, t_hours=10_000_000.0, layer="fact")
    assert v >= 0.01  # MIN_VOLUME


def test_decay_layers_differ(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    # docs decay slower than facts at the same elapsed time.
    fact_v = fn(stored=60.0, t_hours=720.0, layer="fact")
    doc_v = fn(stored=60.0, t_hours=720.0, layer="doc")
    assert doc_v > fact_v
