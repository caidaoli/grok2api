from __future__ import annotations

from app.services.grok.model import ModelService


def test_model_service_aliases_grok_420_to_grok_4_2():
    canonical = ModelService.get("grok-4.2")
    alias = ModelService.get("grok-420")

    assert canonical is not None
    assert alias is not None
    assert canonical.model_id == "grok-4.2"
    assert alias.model_id == "grok-4.2"
    assert alias.grok_model == "grok-4.2"
    assert alias.rate_limit_model == "grok-4.2"
    assert alias.model_mode == "MODEL_MODE_AUTO"
    assert ModelService.valid("grok-420")
    assert ModelService.rate_limit_model_for("grok-420") == "grok-4.2"


def test_model_service_drops_grok_4_heavy():
    assert not ModelService.valid("grok-4-heavy")
    assert ModelService.get("grok-4-heavy") is None

    listed_ids = {m.model_id for m in ModelService.list()}
    assert "grok-4-heavy" not in listed_ids


def test_model_service_supports_grok_4_2_fast():
    model = ModelService.get("grok-4.2-fast")

    assert model is not None
    assert model.model_id == "grok-4.2-fast"
    assert model.grok_model == "grok-4.2"
    assert model.rate_limit_model == "grok-4.2"
    assert model.model_mode == "MODEL_MODE_FAST"
    assert ModelService.valid("grok-4.2-fast")
    assert ModelService.to_grok("grok-4.2-fast") == ("grok-4.2", "MODEL_MODE_FAST")
