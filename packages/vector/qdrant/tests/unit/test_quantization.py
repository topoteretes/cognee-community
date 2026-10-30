"""Pure-unit tests for the Qdrant quantization config helpers. No server needed.

Moved out of tests/test_qdrant.py (the integration script) so they run in the
always-on unit tier.
"""

import pytest


def test_build_quantization_config_default(monkeypatch):
    from cognee_community_vector_adapter_qdrant.quantization import build_quantization_config

    monkeypatch.delenv("QDRANT_QUANTIZATION", raising=False)
    assert build_quantization_config() is None


@pytest.mark.parametrize(
    "kind,expected_bits",
    [
        ("tq4", "bits4"),
        ("tq2", "bits2"),
        ("tq1.5", "bits1_5"),
        ("tq1", "bits1"),
    ],
)
def test_build_turboquant_config(monkeypatch, kind, expected_bits):
    from cognee_community_vector_adapter_qdrant.quantization import build_quantization_config
    from qdrant_client import models

    monkeypatch.setenv("QDRANT_QUANTIZATION", kind)
    cfg = build_quantization_config()
    assert isinstance(cfg, models.TurboQuantization)
    assert cfg.turbo.bits == expected_bits
    assert cfg.turbo.always_ram is True


def test_build_quantization_config_unknown(monkeypatch):
    from cognee_community_vector_adapter_qdrant.quantization import build_quantization_config

    monkeypatch.setenv("QDRANT_QUANTIZATION", "garbage")
    with pytest.raises(ValueError):
        build_quantization_config()


def test_build_search_params_disabled(monkeypatch):
    from cognee_community_vector_adapter_qdrant.quantization import build_search_params

    monkeypatch.delenv("QDRANT_QUANTIZATION", raising=False)
    assert build_search_params() is None


def test_build_search_params_enabled(monkeypatch):
    from cognee_community_vector_adapter_qdrant.quantization import build_search_params

    monkeypatch.setenv("QDRANT_QUANTIZATION", "tq4")
    monkeypatch.setenv("QDRANT_QUANTIZATION_OVERSAMPLING", "3.0")
    params = build_search_params()
    assert params is not None
    assert params.quantization.rescore is True
    assert params.quantization.oversampling == 3.0
    assert params.quantization.ignore is False
