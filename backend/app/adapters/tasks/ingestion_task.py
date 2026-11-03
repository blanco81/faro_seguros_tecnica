"""Tarea Celery de ingesta (Fase 2 paso 3): el pipeline asíncrono.

La tarea recibe SOLO el ``document_id`` (D15): los bytes del archivo
viven en la base (``document_contents``) y el worker los lee de ahí —
nunca pasan por Redis. Esa es la pieza que hace a la tarea barata de
reintentar y de re-ejecutar: reintentos de Celery, reentrega tras un
reinicio del worker (``acks_late``) y reproceso manual (D04) arrancan
siempre desde el mismo contrato: "dime qué documento".

Anatomía del módulo (regla de la suite: la lógica es pura y testable
sin broker ni base — D01; el cableado real lo prueba el smoke, paso 5):

- ``RetryPolicy`` / ``decide_retry`` — la política D16 como función
  pura: qué excepción se reintenta, con qué espera, y cuándo se
  convierte en fallo permanente del documento.
- ``mark_permanent_failure`` — agotados los reintentos, marca el
  documento ``failed`` con la razón (toma el repositorio: testable
  con el doble).
- ``_process_document`` — el cableado transaccional: sesión → contenido
  → ``run_ingestion``. El ``failed`` de negocio se COMPLETA
  (``commit_on``), las transitorias se revierten (D03).
- ``ingest_document`` — la tarea: ``_dispatch`` decide por excepción.

Política de reintentos (D16), resumen:

===================  =====================  ===========================
Excepción            El documento queda    La tarea
===================  =====================  ===========================
FatalIngestionError  ``failed`` (servicio)  termina sin reintentar
Transitoria          ``processing``         reintenta (10/20/40 s)
Transitoria agotada  ``failed`` (tarea)     termina sin más reintentos
===================  =====================  ===========================
"""

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from app.adapters.db.repository import SqlAlchemyDocumentRepository
from app.adapters.db.session import session_scope
from app.adapters.factory import build_embedder, build_extractor
from app.adapters.tasks.celery_app import celery_app
from app.config import get_settings
from app.domain.document import DocumentStatus
from app.ports.embeddings import EmbeddingProvider
from app.ports.extraction import TextExtractor
from app.ports.repository import DocumentInfo, DocumentRepository
from app.services.ingestion import FatalIngestionError, run_ingestion

__all__ = [
    "RetryDecision",
    "RetryPolicy",
    "decide_retry",
    "ingest_document",
    "mark_permanent_failure",
]

logger = logging.getLogger(__name__)

#: Techo de reintentos y base del backoff exponencial (D16). Constantes a
#: conciencia, no settings: sin evidencia (set de evaluación, Fase 3) que
#: justifique más perillas — mismo criterio que el umbral en D13.
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 10.0

#: Colchones de tiempo (D16): el peor caso razonable es un PDF de 20 MB
#: (~500 páginas → ~400 chunks → 13 lotes × 30 s de timeout del proveedor,
#: D11/D10) más la extracción. El límite BLANDO lanza
#: ``SoftTimeLimitExceeded`` — transitoria como otra: se revierte la
#: sesión (D03) y se reintenta. El DURO (SIGKILL) es la última red: con
#: ``acks_late`` el mensaje se reentrega a otro worker.
SOFT_TIME_LIMIT_SECONDS = 900
TIME_LIMIT_SECONDS = 960


# --- política de reintentos (pura, D16) ----------------------------------


class RetryDecision(Enum):
    """Qué hacer con una excepción de la tarea."""

    GIVE_UP = "give_up"  # fatal: ya está failed, reintentar no recupera nada
    RETRY = "retry"  # transitoria: reintentar con backoff
    FAIL_PERMANENTLY = "fail_permanently"  # agotada: marcar failed y parar


@dataclass(frozen=True)
class RetryPolicy:
    """Parámetros de la política D16 (backoff exponencial: 10, 20, 40 s)."""

    max_retries: int = MAX_RETRIES
    backoff_base_seconds: float = RETRY_BACKOFF_BASE_SECONDS

    def countdown(self, retries_so_far: int) -> float:
        """Espera antes del intento ``retries_so_far + 1``."""
        return self.backoff_base_seconds * (2**retries_so_far)


@dataclass(frozen=True)
class RetryPlan:
    """Veredicto de ``decide_retry`` para una excepción."""

    decision: RetryDecision
    countdown_seconds: float = 0.0


def decide_retry(
    exc: Exception, *, retries_so_far: int, policy: RetryPolicy
) -> RetryPlan:
    """Clasifica una excepción de la tarea según D16.

    Args:
        exc: lo que rompió el intento actual.
        retries_so_far: reintentos ya consumidos (``task.request.retries``).
        policy: techo de reintentos y base del backoff.

    Returns:
        El plan: renunciar (fatal), reintentar (transitoria con
        presupuesto) o fallar para siempre (presupuesto agotado).
    """
    if isinstance(exc, FatalIngestionError):
        # run_ingestion ya dejó el documento failed con la razón ANTES de
        # lanzar (contrato del servicio): reintentar un PDF cifrado mil
        # veces sigue siendo un PDF cifrado.
        return RetryPlan(RetryDecision.GIVE_UP)
    if retries_so_far >= policy.max_retries:
        # La transitoria no se curó en todo el presupuesto. run_ingestion
        # no marcó failed (las transitorias no lo hacen): la CONVIERTE la
        # tarea, que es quien sabe que ya no quedan intentos.
        return RetryPlan(RetryDecision.FAIL_PERMANENTLY)
    return RetryPlan(
        RetryDecision.RETRY, countdown_seconds=policy.countdown(retries_so_far)
    )


def mark_permanent_failure(
    repository: DocumentRepository,
    document_id: uuid.UUID,
    reason: str,
) -> DocumentInfo | None:
    """Marca ``failed`` el documento tras agotar los reintentos (D16).

    Se llama desde una sesión NUEVA: la del último intento pudo morir
    con la propia base caída. El estado alcanzable aquí es ``pending``
    (falló antes de ``_begin_processing``) o ``processing`` — ambos con
    transición legal a ``failed`` (D04).

    Returns:
        El documento actualizado, o ``None`` si el documento desapareció
        (borrado concurrente): no hay fila que marcar y no es un error
        de la tarea.
    """
    document = repository.get(document_id)
    if document is None:
        return None
    return repository.set_status(
        document_id, DocumentStatus.FAILED, failure_reason=reason
    )


# --- cableado del pipeline (lo prueba el smoke, Fase 2 paso 5) -----------


@lru_cache(maxsize=1)
def _extractor() -> TextExtractor:
    """Extractor por proceso del worker (pypdf no mantiene estado)."""
    return build_extractor()


@lru_cache(maxsize=1)
def _embedder() -> EmbeddingProvider:
    """Embedder por proceso: un cliente HTTP para toda la vida del worker."""
    return build_embedder()


# Fail-fast (D14): al arrancar, el worker muere con un mensaje accionable
# si faltan las credenciales del proveedor — no a mitad de la primera
# tarea. (En la suite no dispara red: conftest fija el modo fake.)
get_settings().validate_provider_credentials()


def _process_document(document_id: uuid.UUID) -> DocumentInfo:
    """Sesión → contenido (D15) → pipeline completo.

    El ``failed`` de negocio se COMPLETA (``commit_on``): es el resultado
    correcto de la tarea y debe sobrevivir. Las transitorias revierten —
    sin estado parcial (D03) — y suben para que la política reintente.
    """
    settings = get_settings()
    with session_scope(commit_on=(FatalIngestionError,)) as session:
        repository = SqlAlchemyDocumentRepository(session)

        data = repository.get_content(document_id)
        if data is None:
            document = repository.get(document_id)
            if document is None:
                # No hay fila: nada que marcar (lo cubre run_ingestion,
                # pero aquí ni siquiera hay contenido con qué intentarlo).
                raise FatalIngestionError(
                    f"El documento {document_id} no existe en la base."
                )
            # Invariante roto (register_document escribe ambos en la misma
            # transacción): se documenta en la propia fila y no se reintenta.
            reason = (
                "El documento fue registrado sin su contenido "
                "(inconsistencia de datos)."
            )
            repository.set_status(
                document_id, DocumentStatus.FAILED, failure_reason=reason
            )
            raise FatalIngestionError(reason)

        return run_ingestion(
            repository,
            _extractor(),
            _embedder(),
            document_id,
            data,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )


def _mark_failed_in_new_session(document_id: uuid.UUID, reason: str) -> None:
    """Marca ``failed`` desde una sesión nueva (la del intento pudo morir)."""
    with session_scope() as session:
        mark_permanent_failure(
            SqlAlchemyDocumentRepository(session), document_id, reason
        )


# --- la tarea ------------------------------------------------------------


def _task_result(
    document_id: uuid.UUID,
    *,
    info: DocumentInfo | None = None,
    failure_reason: str | None = None,
    attempt: int,
) -> dict:
    """Resultado uniforme de la tarea (backend Redis / smoke / Fase 4)."""
    return {
        "document_id": str(document_id),
        "status": info.status.value if info else DocumentStatus.FAILED.value,
        "page_count": info.page_count if info else None,
        "chunk_count": info.chunk_count if info else 0,
        "failure_reason": failure_reason,
        "attempt": attempt,
    }


def _dispatch(
    task,
    document_id: uuid.UUID,
    *,
    process=_process_document,
    mark_failed=_mark_failed_in_new_session,
    policy: RetryPolicy | None = None,
) -> dict:
    """Cuerpo de la tarea, separado del decorador para la suite (D01).

    Recibe las piezas cableables como parámetros con default real: los
    tests inyectan dobles sin broker ni base. ``task`` es la instancia
    Celery (``bind=True``); la suite usa un doble que registra el
    reintento y lanza ``Retry`` como la real.
    """
    policy = policy or RetryPolicy()
    attempt = task.request.retries + 1

    try:
        info = process(document_id)
    except Exception as exc:  # noqa: BLE001 — deliberado: la política
        # reintenta TODO lo que no sea fatal (D16); filtrar aquí por
        # tipo dejaría pasar transitorias nuevas sin clasificar.
        plan = decide_retry(exc, retries_so_far=task.request.retries, policy=policy)

        if plan.decision is RetryDecision.GIVE_UP:
            logger.warning(
                "Ingesta fatal de %s (no se reintenta): %s", document_id, exc
            )
            return _task_result(
                document_id, failure_reason=str(exc), attempt=attempt
            )

        if plan.decision is RetryDecision.FAIL_PERMANENTLY:
            reason = (
                f"Reintentos agotados ({policy.max_retries + 1} intentos): {exc}"
            )
            mark_failed(document_id, reason)
            logger.error("Ingesta de %s: %s", document_id, reason)
            return _task_result(document_id, failure_reason=reason, attempt=attempt)

        logger.warning(
            "Ingesta transitoria de %s (intento %d/%d): %s — reintento "
            "en %.0f s",
            document_id,
            attempt,
            policy.max_retries + 1,
            exc,
            plan.countdown_seconds,
        )
        raise task.retry(exc=exc, countdown=plan.countdown_seconds)

    logger.info(
        "Ingesta de %s lista: %d página(s), %d chunk(s)",
        document_id,
        info.page_count or 0,
        info.chunk_count,
    )
    return _task_result(document_id, info=info, attempt=attempt)


@celery_app.task(
    bind=True,
    name="tasks.ingest_document",
    # Redundancia deliberada con decide_retry: la política decide ANTES
    # de llamar a retry() y nunca deja llegar al techo de Celery; si un
    # refactor la saltara, el techo del decorador corta igual.
    max_retries=MAX_RETRIES,
    soft_time_limit=SOFT_TIME_LIMIT_SECONDS,
    time_limit=TIME_LIMIT_SECONDS,
)
def ingest_document(self, document_id: str) -> dict:
    """Pipeline asíncrono de ingesta: pending → processing → ready/failed.

    Args:
        document_id: UUID del documento registrado por la API (register_
            document). Los bytes no viajan en el mensaje (D15).

    Returns:
        Resumen del desenlace (``ready`` o ``failed`` con su razón).
    """
    return _dispatch(self, uuid.UUID(document_id))
