"""Factory de adapters (D01): Settings → instancias concretas de los puertos.

Es el ÚNICO lugar del código que sabe qué adapter corresponde a cada
proveedor; API y worker piden aquí sus dependencias y reciben el puerto.

Fail-fast (D14): ``build_embedder`` valida las credenciales ANTES de
construir — si falta ``CLOUDFLARE_ACCOUNT_ID``, el proceso muere en el
arranque con un mensaje que dice qué variable y dónde arreglarla, no a
mitad de la primera tarea. La validación cubre el conjunto de proveedores
configurado (llm + embeddings): en la Fase 2 el worker solo embediza, pero
dejar pasar un LLM mal configurado solo aplanta el error hasta la Fase 3.
"""

from app.adapters.extraction.extractors import build_default_extractor
from app.adapters.llm.embeddings import OpenAICompatEmbeddingProvider
from app.adapters.llm.fake_embeddings import FakeEmbeddingProvider
from app.config import Settings, get_settings
from app.ports.embeddings import EmbeddingProvider
from app.ports.extraction import TextExtractor

__all__ = ["build_embedder", "build_extractor"]


def build_extractor() -> TextExtractor:
    """Extractor con despacho por MIME (PDF + texto plano; local, sin claves)."""
    return build_default_extractor()


def build_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    """Construye el EmbeddingProvider del proveedor configurado.

    Raises:
        ValueError: credenciales incompletas para el proveedor elegido
            (mensaje con las variables que faltan y dónde definirlas).
    """
    settings = settings or get_settings()
    settings.validate_provider_credentials()  # D14: fail-fast al arrancar

    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider()

    if settings.embedding_provider == "cloudflare":
        # Preset Cloudflare Workers AI (D10): mismo protocolo OpenAI,
        # base_url propia y token como Bearer. AI Gateway opcional.
        cabeceras: dict[str, str] | None = None
        if settings.cloudflare_gateway_id:
            cabeceras = {"cf-aig-gateway-id": settings.cloudflare_gateway_id}
        return OpenAICompatEmbeddingProvider(
            model=settings.embedding_model,
            api_key=settings.cloudflare_api_token or "",
            base_url=settings.cloudflare_ai_base_url,
            extra_headers=cabeceras,
            batch_size=settings.embedding_batch_size,
            timeout=settings.embedding_timeout_seconds,
        )

    # Preset OpenAI (o cualquier endpoint compatible auto-alojado).
    return OpenAICompatEmbeddingProvider(
        model=settings.embedding_model,
        api_key=settings.openai_api_key or "",
        base_url=settings.openai_base_url,
        batch_size=settings.embedding_batch_size,
        timeout=settings.embedding_timeout_seconds,
    )
