"""Rutas HTTP de consulta (Fase 3): POST /api/query.

Adapter primario del agente RAG (D19): traduce HTTP ↔ ``AgenticQueryService``.
Sin lógica de negocio aquí — el bucle, el umbral y la verificación de citas
viven en el servicio; la ruta compone dependencias y serializa.

Contrato:

- ``200`` con ``answer`` + ``citations`` — la respuesta con su cita
  verificada (documento, página, fragmento literal).
- ``200`` con ``refused: true`` — el rechazo explícito del enunciado
  (D13): sin evidencia suficiente, sin cita verificable. El ``message``
  dice por qué y sugiere reformular.
- ``200`` con ``clarification`` — el planificador detectó ambigüedad
  irresoluble y pide aclaración antes de gastar recuperación.
- ``422`` — la pregunta no cumple el contrato (vacía/corta/larga).
- ``503`` — el proveedor LLM/embeddings no está disponible: fallo de
  transporte, no un rechazo del dominio (nunca se confunden).

La sesión de base viva solo durante la request (``session_scope`` como
dependencia generadora): el retriever consulta y la sesión se cierra al
salir — aquí no hay dispatch posterior, así que el orden D17 no aplica.
"""

import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.db.retrieval import PgVectorRetriever
from app.adapters.db.session import session_scope
from app.adapters.factory import build_embedder, build_llm
from app.config import Settings, get_settings
from app.services.query import AgenticQueryService, QueryAnswer

__all__ = ["get_query_service", "query_router"]


# --- composición (D01): aquí y solo aquí la ruta conoce los adapters -------


def get_query_service(
    settings: Settings = Depends(get_settings),
) -> Iterator[AgenticQueryService]:
    """Dependencia generadora: agente con adapters reales, sesión por request.

    ``session_scope`` mantiene la sesión viva durante la request y la
    cierra al salir — el retriever consulta dentro de la request, sin
    estado que sobreviva. Inyectable para los tests (dobles sin base).
    """
    with session_scope() as session:
        yield AgenticQueryService(
            llm=build_llm(settings),
            embedder=build_embedder(settings),
            retriever=PgVectorRetriever(session),
            rejection_threshold=settings.rejection_threshold,
        )


# --- modelos ------------------------------------------------------------


class QueryIn(BaseModel):
    """La pregunta del analista; ``document_id`` acota opcionalmente."""

    question: str = Field(min_length=3, max_length=500)
    document_id: uuid.UUID | None = None


class CitationOut(BaseModel):
    """Cita verificada: documento, página y fragmento literal (D12)."""

    model_config = ConfigDict(from_attributes=True)

    document: str
    page: int
    quote: str


class QueryOut(BaseModel):
    """Respuesta, rechazo o aclaración — siempre 200 salvo fallo de proveedor."""

    answer: str | None
    citations: list[CitationOut]
    refused: bool
    reason: str | None
    message: str | None
    clarification: str | None
    iterations: int


# --- la ruta -------------------------------------------------------------


query_router = APIRouter(prefix="/api/query", tags=["query"])


def _a_salida(resultado: QueryAnswer) -> QueryOut:
    return QueryOut(
        answer=resultado.answer,
        citations=[
            CitationOut(document=c.document, page=c.page, quote=c.quote)
            for c in resultado.citations
        ],
        refused=resultado.refused,
        reason=resultado.reason,
        message=resultado.message,
        clarification=resultado.clarification,
        iterations=resultado.iterations,
    )


@query_router.post("", response_model=QueryOut)
def ask_query(
    payload: QueryIn,
    service: AgenticQueryService = Depends(get_query_service),
) -> QueryOut:
    """Pregunta al asistente; responde con cita verificada o se rehúsa."""
    try:
        resultado = service.answer(payload.question, document_id=payload.document_id)
    except Exception as exc:
        # la consulta es interactiva, no hay Celery que reintente; se
        # devuelve 503 accionable y el analista reintenta cuando quiera.
        raise HTTPException(
            status_code=503,
            detail=(
                "El proveedor de IA no está disponible en este momento. "
                f"Detalle: {exc}"
            ),
        ) from exc
    return _a_salida(resultado)
