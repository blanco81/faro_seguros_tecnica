"""Rutas HTTP de documentos (Fase 2 paso 4): subida y seguimiento.

Adapter primario: traduce HTTP ↔ puertos. La lógica de negocio vive en el
servicio (``register_document``); aquí solo se valida la subida (D08), se
registra y se despacha la tarea asíncrona.

Contrato:

- ``POST /api/documents`` (multipart, campo ``file``) — valida nombre, tipo
  MIME y tamaño (D08); registra documento + contenido en una transacción
  (D03/D15) y responde ``201`` con el documento ``pending``. Re-subir los
  mismos bytes responde ``200`` con el documento YA registrado, sin
  despachar tarea nueva (idempotencia de subida, D03).
- ``GET /api/documents`` — listado para el seguimiento (el más reciente
  primero, orden del repositorio).
- ``GET /api/documents/{document_id}`` — detalle: estado, páginas, chunks,
  motivo de fallo (el polling de la Fase 4 lee esto).

La pieza no obvia — **orden commit → dispatch**: la tarea Celery se
despacha SOLO después de que el commit persistió el documento. Se verificó
empíricamente (FastAPI 0.128) que las ``BackgroundTasks`` corren ANTES del
código de salida de las dependencias con ``yield``, así que el commit de
``get_session`` llegaría tarde: el worker podría levantar la tarea sin ver
la fila y marcarla fatal sin reintento ("documento no existe") — quedando
``pending`` para siempre. Por eso el endpoint abre su propia unidad de
trabajo con ``with`` (el mismo ``session_scope`` del worker) y despacha al
salir del bloque, con la fila ya commiteada (decisión D17).
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict

from app.adapters.db.repository import SqlAlchemyDocumentRepository
from app.adapters.db.session import session_scope
from app.config import Settings, get_settings
from app.domain.document import DocumentStatus
from app.ports.repository import DocumentRepository
from app.services.ingestion import register_document

__all__ = ["documents_router", "get_ingest_dispatcher", "get_repository_uow"]


# --- composición (D01): aquí y solo aquí la ruta conoce el adapter ORM ----


@contextmanager
def _sqlalchemy_repository() -> Iterator[SqlAlchemyDocumentRepository]:
    """Unidad de trabajo real: sesión transaccional + repositorio.

    ``session_scope`` commitea al salir bien del ``with`` y revierte con
    cualquier excepción — el endpoint despacha la tarea DESPUÉS del bloque,
    con el alta ya persistida (D17).
    """
    with session_scope() as session:
        yield SqlAlchemyDocumentRepository(session)


def get_repository_uow() -> Callable[[], AbstractContextManager[DocumentRepository]]:
    """Factory inyectable de unidades de trabajo (tests: doble sin base)."""
    return _sqlalchemy_repository


def _celery_dispatcher(document_id: uuid.UUID) -> None:
    """Despacha la tarea al broker (import lazy: la API no arranca al worker).

    El import diferido evita que el proceso API cargue la maquinaria del
    worker (fail-fast de credenciales incluido) solo por importar la ruta.
    """
    from app.adapters.tasks.ingestion_task import ingest_document

    ingest_document.delay(str(document_id))


def get_ingest_dispatcher() -> Callable[[uuid.UUID], None]:
    """Dispatcher inyectable (tests: lista que registra los despachos)."""
    return _celery_dispatcher


# --- modelo de respuesta ---------------------------------------------------


class DocumentOut(BaseModel):
    """Documento serializado: lo que lee el seguimiento y la Fase 4."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_hash: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    chunk_count: int = 0


# --- rutas -----------------------------------------------------------------


documents_router = APIRouter(prefix="/api/documents", tags=["documents"])


def _tipo_base(mime: str) -> str:
    """Normaliza el MIME declarado (``text/plain; charset=utf-8`` → ``text/plain``)."""
    return mime.split(";")[0].strip().lower()


@documents_router.post("", status_code=201, response_model=DocumentOut)
def upload_document(
    response: Response,
    file: UploadFile,
    repository_uow: Callable[[], AbstractContextManager[DocumentRepository]] = Depends(
        get_repository_uow
    ),
    dispatch_ingestion: Callable[[uuid.UUID], None] = Depends(get_ingest_dispatcher),
    settings: Settings = Depends(get_settings),
) -> DocumentOut:
    """Registra una subida y despacha la ingesta asíncrona.

    Status codes:

    - ``201``: documento nuevo (``pending``) y tarea despachada.
    - ``200``: contenido ya registrado (mismo hash, D03) — nada despachado.
    - ``413`` / ``415`` / ``422``: subida rechazada por tamaño / tipo / nombre.
    """
    filename = file.filename or ""
    if not filename.strip():
        raise HTTPException(
            status_code=422,
            detail="La subida necesita un nombre de archivo no vacío.",
        )

    mime = _tipo_base(file.content_type or "")
    if mime not in settings.allowed_mime_types:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Tipo no permitido: '{mime}'. Tipos aceptados: "
                f"{', '.join(settings.allowed_mime_types)}."
            ),
        )

    data = file.file.read()

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=(
                f"El archivo pesa {len(data)} bytes; el máximo es "
                f"{settings.max_upload_mb} MB."
            ),
        )
    if not data:
        raise HTTPException(
            status_code=422,
            detail="El archivo está vacío: no hay nada que ingestar.",
        )

    # Transacción completa ANTES del dispatch (D17): al salir del with,
    # documento y contenido (D15) ya son visibles para el worker.
    with repository_uow() as repository:
        document, created = register_document(
            repository, filename=filename, mime_type=mime, data=data
        )

    if created:
        dispatch_ingestion(document.id)
    else:
        # Re-subida del mismo contenido (D03): ya está registrado; el
        # estado a devolver es el que YA tiene (ready, failed, ...).
        response.status_code = 200

    response.headers["Location"] = f"/api/documents/{document.id}"
    return document


@documents_router.get("", response_model=list[DocumentOut])
def list_documents(
    repository_uow: Callable[[], AbstractContextManager[DocumentRepository]] = Depends(
        get_repository_uow
    ),
) -> list[DocumentOut]:
    """Todos los documentos, el más reciente primero (seguimiento)."""
    with repository_uow() as repository:
        return repository.list_documents()


@documents_router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: uuid.UUID,
    repository_uow: Callable[[], AbstractContextManager[DocumentRepository]] = Depends(
        get_repository_uow
    ),
) -> DocumentOut:
    """Detalle de un documento (estado, páginas, chunks, motivo de fallo)."""
    with repository_uow() as repository:
        document = repository.get(document_id)
    if document is None:
        raise HTTPException(
            status_code=404,
            detail=f"Documento no encontrado: {document_id}",
        )
    return document
