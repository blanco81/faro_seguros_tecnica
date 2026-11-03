"""Configuración central: secretos y parámetros SOLO por variables de entorno.

Por defecto los proveedores son "fake": el sistema completo (y sus tests)
funciona sin claves ni red. Ver .env.example en la raíz del repo.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Infraestructura ---
    database_url: str = "postgresql+psycopg://faro:faro@localhost:5432/faro"
    redis_url: str = "redis://localhost:6379/0"

    # --- Proveedores (puertos): "fake" | "openai" | "cloudflare" ---
    # fake: sin red ni claves (tests y desarrollo local).
    # cloudflare: Workers AI via endpoint OpenAI-compatible (decision D10).
    llm_provider: str = "fake"
    embedding_provider: str = "fake"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: float = 30.0

    # --- Cloudflare Workers AI (endpoint OpenAI-compatible) ---
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    # Opcional: cabecera cf-aig-gateway-id si se enruta via AI Gateway
    cloudflare_gateway_id: str | None = None

    @property
    def cloudflare_ai_base_url(self) -> str:
        """Endpoint OpenAI-compatible de Workers AI (chat + embeddings)."""
        if not self.cloudflare_account_id:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID no esta configurado")
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.cloudflare_account_id}/ai/v1"
        )

    # --- RAG ---
    chunk_size: int = 800
    chunk_overlap: int = 120
    rejection_threshold: float = 0.75  # se calibra con el set de evaluación (Fase 3)
    max_upload_mb: int = 20
    allowed_mime_types: list[str] = ["application/pdf", "text/plain"]

    # --- Observabilidad ---
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"

    # --- CORS (solo desarrollo; en producción nginx proxifica) ---
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
