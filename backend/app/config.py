"""Configuración central: TODO llega por variables de entorno o por .env.

Cadena de precedencia (pydantic-settings, decisión D14):

    variable de entorno real  >  archivo .env  >  valor por defecto

Los valores "literales" de esta clase son SOLO el último eslabón: aplican
únicamente cuando la variable no está ni en el entorno ni en el .env
(lo prueban los tests de precedencia de tests/test_smoke.py).

El .env se resuelve ANCLADO a la raíz del repo (ENV_FILE), independiente
del directorio desde donde se lance el proceso (uvicorn, alembic, celery,
pytest): siempre se lee EL MISMO archivo. Si no existe (p. ej. dentro del
contenedor, donde compose inyecta variables reales), se ignora sin error.

Por defecto el sistema arranca con el proveedor REAL elegido en D10
(Cloudflare Workers AI: llama-3.3-70b para chat, bge-m3 para embeddings).
El modo "fake" (proveedor simulado, sin red ni claves) sigue existiendo
porque el enunciado lo exige, pero YA NO es el valor por defecto: lo fijan
explícitamente los tests (tests/conftest.py) o quien quiera probar sin
claves, con LLM_PROVIDER=fake / EMBEDDING_PROVIDER=fake.

Nota de diseño: pydantic-settings YA usa python-dotenv por dentro (es una
de sus dependencias obligatorias); llamar a load_dotenv() a mano sería
redundante y perdería tipado y control de precedencia. No se usa
``extra="allow"`` porque absorbería TODAS las variables del proceso (PATH
incluida) y ``model_dump()`` volcaría secretos a los logs. Las variables
nuevas se declaran aquí: una línea, tipada y validada.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Proveedores permitidos para los puertos LLMProvider / EmbeddingProvider.
#: Al ser un Literal, un typo ("cloudfare") falla al arrancar, no en producción.
ProviderName = Literal["cloudflare", "openai", "fake"]

#: Raíz del repo (backend/app/config.py -> dos niveles arriba).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: El .env de SIEMPRE: el de la raíz del repo, esté donde esté el CWD del
#: proceso. Si no existe (contenedores: compose inyecta variables reales),
#: pydantic-settings lo ignora sin error.
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # --- Infraestructura ---
    database_url: str = "postgresql+psycopg://faro:faro@localhost:5432/faro"
    redis_url: str = "redis://localhost:6379/0"

    # --- Proveedores (puertos) ---
    # Stack real por defecto (D10): Cloudflare Workers AI.
    # "fake" es el modo explícito de tests/CI (enunciado: probar con
    # proveedor simulado, sin red ni claves); "openai" es el preset
    # alternativo del mismo adapter (endpoint OpenAI-compatible).
    llm_provider: ProviderName = "cloudflare"
    embedding_provider: ProviderName = "cloudflare"
    llm_model: str = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    embedding_model: str = "@cf/baai/bge-m3"
    llm_timeout_seconds: float = 30.0

    # --- Cloudflare Workers AI (endpoint OpenAI-compatible, D10) ---
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    # Opcional: cabecera cf-aig-gateway-id si se enruta via AI Gateway
    cloudflare_gateway_id: str | None = None

    # --- OpenAI (preset alternativo del mismo adapter) ---
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    @property
    def cloudflare_ai_base_url(self) -> str:
        """Endpoint OpenAI-compatible de Workers AI (chat + embeddings)."""
        if not self.cloudflare_account_id:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID no está configurado")
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.cloudflare_account_id}/ai/v1"
        )

    def validate_provider_credentials(self) -> None:
        """Fail-fast: credenciales completas para el proveedor elegido.

        La llama el factory de adapters al arrancar API y worker (Fase 2),
        para morir en el arranque con un mensaje accionable y no a mitad
        de una consulta.
        """
        missing: list[str] = []
        providers = {self.llm_provider, self.embedding_provider}

        if "cloudflare" in providers:
            if not self.cloudflare_account_id:
                missing.append("CLOUDFLARE_ACCOUNT_ID")
            if not self.cloudflare_api_token:
                missing.append("CLOUDFLARE_API_TOKEN")
        if "openai" in providers and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")

        if missing:
            raise ValueError(
                "Configuración incompleta para el proveedor: faltan "
                f"{', '.join(missing)}. Defínelos en .env (ver .env.example) "
                "o usa LLM_PROVIDER=fake y EMBEDDING_PROVIDER=fake para "
                "trabajar sin claves."
            )

    # --- RAG ---
    chunk_size: int = 800
    chunk_overlap: int = 120
    # Umbral de similitud coseno para responder (D13). 0.45 es el punto de
    # partida para bge-m3 (rara vez supera ~0.7 incluso con paráfrasis);
    # se calibrará con el set de evaluación en la Fase 3.
    rejection_threshold: float = 0.45
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
