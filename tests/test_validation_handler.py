"""
tests/test_validation_handler.py — Regression tests for the 422 handler.

Pydantic v2 embeds the raw exception object raised inside a field_validator
in the error's ``ctx`` (e.g. ``{'ctx': {'error': ValueError(...)}}``).
The validation handler must sanitize errors before JSON-encoding them,
otherwise the client gets a 500 TypeError instead of a clean 422.

Runs with:  poetry run pytest tests/test_validation_handler.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator

from fastapi_flare.handlers import make_validation_exception_handler


def _make_config():
    cfg = MagicMock()
    cfg.max_request_body_bytes = 8192
    cfg.capture_response_body = False
    cfg.storage_instance = None
    return cfg


class BatchRequest(BaseModel):
    shipment_uuids: list[str] = Field(..., min_length=1)

    @field_validator("shipment_uuids", mode="before")
    @classmethod
    def _validate_max_size(cls, v):
        if isinstance(v, list) and len(v) > 100:
            raise ValueError(
                f"Limite máximo de 100 envios por requisição excedido "
                f"(recebido: {len(v)}). Divida em lotes menores."
            )
        return v


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("fastapi_flare.queue.push_log", AsyncMock())

    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError, make_validation_exception_handler(_make_config())
    )

    @app.post("/batch")
    async def batch(payload: BatchRequest):
        return {"count": len(payload.shipment_uuids)}

    return app


def test_validator_valueerror_returns_422_not_500(app):
    """A ValueError raised in a field_validator must yield a serializable 422."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/batch", json={"shipment_uuids": ["u"] * 102})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("Limite máximo de 100" in e["msg"] for e in detail)
    # ctx.error must be a string now, not a repr artifact of a crash
    ctx_errors = [e["ctx"]["error"] for e in detail if "ctx" in e]
    assert all(isinstance(v, str) for v in ctx_errors)


def test_plain_validation_error_still_works(app):
    """Standard type errors (no ctx exception) keep working."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/batch", json={"shipment_uuids": "not-a-list"})

    assert resp.status_code == 422
    assert resp.json()["detail"]
