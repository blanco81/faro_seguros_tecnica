"""Tests de humo de la Fase 0: app, configuración y metadatos del esquema.

Corren sin red ni claves (proveedores fake por defecto).
"""

import pytest
from fastapi.testclient import TestClient

from app.adapters.db.models import EMBEDDING_DIMS, Base
from app.config import Settings, get_settings
from app.main import create_app


def test_health_ok():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_default_to_fake_providers():
    settings = get_settings()
    assert settings.llm_provider == "fake"
    assert settings.embedding_provider == "fake"
    assert settings.rejection_threshold > 0


def test_models_metadata_complete():
    tables = Base.metadata.tables
    assert set(tables) == {"documents", "chunks"}
    assert EMBEDDING_DIMS == 1024  # @cf/baai/bge-m3 (decisión D10)
    assert "content_hash" in tables["documents"].columns
    assert "embedding" in tables["chunks"].columns
    # La FK con borrado en cascada sostiene la idempotencia del reproceso
    fk = next(iter(tables["chunks"].foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_cloudflare_base_url_requires_account_id():
    settings = Settings()
    if settings.cloudflare_account_id is None:
        with pytest.raises(ValueError):
            _ = settings.cloudflare_ai_base_url


def test_cloudflare_base_url_format():
    settings = Settings(cloudflare_account_id="abc123")
    assert settings.cloudflare_ai_base_url == (
        "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    )
