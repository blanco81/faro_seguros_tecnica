"""Tests de humo: app, configuración y metadatos del esquema.

La configuración es el contrato de la decisión D14: stack real
(Cloudflare Workers AI) por defecto, modo fake explícito para los tests,
y precedencia variable de entorno > .env > default.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.db.models import EMBEDDING_DIMS, Base
from app.config import Settings, get_settings
from app.main import create_app


def test_health_ok():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- D14: el modo de los tests es el proveedor simulado ------------------


def test_suite_corre_en_modo_fake():
    # Lo fija tests/conftest.py: el enunciado exige probar sin red ni claves.
    settings = get_settings()
    assert settings.llm_provider == "fake"
    assert settings.embedding_provider == "fake"


# --- D14: los defaults son el stack real (D10), no fake -------------------


def test_config_por_defecto_es_el_stack_real(monkeypatch):
    for var in ("LLM_PROVIDER", "EMBEDDING_PROVIDER", "LLM_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # sin .env local: puros defaults

    assert settings.llm_provider == "cloudflare"
    assert settings.embedding_provider == "cloudflare"
    assert settings.llm_model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert settings.embedding_model == "@cf/baai/bge-m3"
    assert settings.rejection_threshold == 0.45  # D13


def test_variable_de_entorno_gana_sobre_el_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=cloudflare\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    settings = Settings(_env_file=env_file)
    assert settings.llm_provider == "fake"  # entorno > .env


def test_dotenv_sobrescribe_el_default(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("EMBEDDING_PROVIDER=openai\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)
    assert settings.embedding_provider == "openai"  # .env > default


@pytest.mark.parametrize(
    ("campo", "valor_dotenv"),
    [
        ("database_url", "postgresql+psycopg://demo:demo@ejemplo:5432/demo"),
        ("llm_model", "modelo-de-prueba"),
        ("llm_timeout_seconds", "12.5"),
        ("rejection_threshold", "0.9"),
        ("chunk_size", "123"),
    ],
)
def test_el_dotenv_pisa_el_valor_literal(campo, valor_dotenv, tmp_path, monkeypatch):
    # La literal de config.py es SOLO fallback: definida en .env, gana.
    monkeypatch.delenv(campo.upper(), raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{campo.upper()}={valor_dotenv}\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)
    assert str(getattr(settings, campo)) == valor_dotenv


def test_env_file_anclado_a_la_raiz_del_repo():
    # El .env leído es SIEMPRE el de la raíz del repo, sin importar el
    # directorio desde donde se lance uvicorn/alembic/celery/pytest.
    from app.config import ENV_FILE, REPO_ROOT

    assert ENV_FILE == REPO_ROOT / ".env"
    # La raíz detectada es la real: contiene el compose y la plantilla.
    assert (REPO_ROOT / "docker-compose.yml").exists()
    assert (REPO_ROOT / ".env.example").exists()


# --- D14: fail-fast con mensajes accionables ------------------------------


def test_proveedor_con_typo_rechazado_al_arrancar():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="cloudfare")  # typo


def test_credenciales_faltantes_fallan_rapido():
    settings = Settings(
        _env_file=None,
        llm_provider="cloudflare",
        embedding_provider="cloudflare",
        cloudflare_account_id=None,
        cloudflare_api_token=None,
    )
    with pytest.raises(ValueError) as exc:
        settings.validate_provider_credentials()

    mensaje = str(exc.value)
    assert "CLOUDFLARE_ACCOUNT_ID" in mensaje
    assert "CLOUDFLARE_API_TOKEN" in mensaje
    assert ".env" in mensaje  # dice dónde arreglarlo


def test_credenciales_completas_validan():
    settings = Settings(
        _env_file=None,
        cloudflare_account_id="abc123",
        cloudflare_api_token="token-secreto",
    )
    settings.validate_provider_credentials()  # no debe lanzar


def test_fake_no_exige_credenciales():
    settings = Settings(
        _env_file=None, llm_provider="fake", embedding_provider="fake"
    )
    settings.validate_provider_credentials()  # no debe lanzar


# --- Esquema y endpoint de Cloudflare (Fase 0) -----------------------------


def test_models_metadata_complete():
    tables = Base.metadata.tables
    assert set(tables) == {"documents", "chunks", "document_contents"}
    assert EMBEDDING_DIMS == 1024  # @cf/baai/bge-m3 (decisión D10)
    assert "content_hash" in tables["documents"].columns
    assert "embedding" in tables["chunks"].columns
    # La FK con borrado en cascada sostiene la idempotencia del reproceso
    fk = next(iter(tables["chunks"].foreign_keys))
    assert fk.ondelete == "CASCADE"
    # D15: contenido crudo en tabla 1:1 aparte — PK = FK con CASCADE
    # (muere con el documento) y columna binaria, sin tocar documents.
    contenido = tables["document_contents"]
    assert set(contenido.columns.keys()) == {"document_id", "data"}
    assert contenido.primary_key.columns.keys() == ["document_id"]
    fk_contenido = next(iter(contenido.foreign_keys))
    assert fk_contenido.column.table.name == "documents"
    assert fk_contenido.ondelete == "CASCADE"


def test_cloudflare_base_url_requires_account_id():
    settings = Settings(_env_file=None, cloudflare_account_id=None)
    with pytest.raises(ValueError):
        _ = settings.cloudflare_ai_base_url


def test_cloudflare_base_url_format():
    settings = Settings(_env_file=None, cloudflare_account_id="abc123")
    assert settings.cloudflare_ai_base_url == (
        "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    )
