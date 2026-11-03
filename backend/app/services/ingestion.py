"""Caso de uso de ingesta: registrar la subida y correr el pipeline.

Orquesta puertos y dominio sin tocar adapters (regla hexagonal):

    extracción (TextExtractor) → chunking (dominio, D11)
    → embeddings (EmbeddingProvider) → replace_chunks (DocumentRepository)

Las dos idempotencias del enunciado viven aquí (plan §4.2):

- ``register_document`` — idempotencia de SUBIDA (D03): SHA-256 del
  contenido como clave de unicidad. Re-subir un archivo devuelve el
  documento ya registrado sin reprocesar nada. El contenido crudo se
  guarda junto al alta (D15): la tarea solo necesita el ``document_id``.
- ``run_ingestion`` — idempotencia de REINTENTO (D03): el pipeline
  completo puede reejecutarse sobre el mismo documento (reintento de
  Celery, reentrega tras un reinicio del worker, reproceso manual):
  ``replace_chunks`` borra y reinserta en una única transacción.

Clasificación de errores (la política de reintentos decide la tarea,
paso 3; aquí solo se clasifica):

- ``FatalIngestionError`` — reintentar no puede recuperar nada (PDF
  cifrado, documento sin texto extraíble, embeddings de dimensión
  equivocada). Cuando el documento existe, queda marcado ``failed``
  con la razón ANTES de lanzar; quien la capture solo registra.
- Cualquier otra excepción — transitoria o desconocida (red, base,
  5xx del proveedor): se propaga sin marcar ``failed`` para que la
  tarea reintente con backoff.
"""

import hashlib
import uuid
from typing import NoReturn

from app.domain.chunker import chunk_document
from app.domain.document import DocumentStatus, ensure_transition
from app.ports.embeddings import (
    EMBEDDING_DIMS,
    EmbeddingDimensionError,
    EmbeddingProvider,
)
from app.ports.extraction import (
    ExtractionError,
    TextExtractor,
    UnsupportedMimeTypeError,
)
from app.ports.repository import DocumentInfo, DocumentRepository, NewChunk

__all__ = ["FatalIngestionError", "register_document", "run_ingestion"]


class FatalIngestionError(Exception):
    """Fallo del que ningún reintento puede recuperarse.

    Si el documento existe, el servicio lo dejó en ``failed`` con un
    ``failure_reason`` accionable ANTES de lanzar esta excepción.
    """


def register_document(
    repository: DocumentRepository,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
) -> tuple[DocumentInfo, bool]:
    """Registra una subida con deduplicación por hash de contenido (D03).

    Además del alta del documento, persiste el contenido crudo (D15)
    en la MISMA transacción de la sesión inyectada: o queda el documento
    con su archivo, o no queda nada — la tarea Celery nunca encuentra
    un registro sin contenido.

    Args:
        repository: persistencia del agregado Documento.
        filename: nombre declarado por el cliente.
        mime_type: tipo MIME validado por la API (D08).
        data: contenido crudo tal como se subió.

    Returns:
        ``(documento, creado)``: el segundo es ``False`` cuando el hash
        ya existía — en ese caso se devuelve el documento registrado y
        la API no debe encolar ninguna tarea.

    Raises:
        ValueError: si el nombre de archivo está vacío.
    """
    if not filename:
        raise ValueError("filename no puede estar vacío")

    content_hash = hashlib.sha256(data).hexdigest()
    existing = repository.find_by_hash(content_hash)
    if existing is not None:
        # Mismo hash = mismos bytes: el contenido ya está guardado; no se
        # reescribe (la relación es 1:1, D15) ni se encola nada.
        return existing, False

    # String(255) en el esquema: se trunca aquí (no en el adapter) para
    # que el contrato del puerto sea idéntico con cualquier implementación.
    document = repository.create_document(
        filename=filename[:255],
        content_hash=content_hash,
        mime_type=mime_type,
        size_bytes=len(data),
    )
    repository.save_content(document.id, data)
    return document, True


def run_ingestion(
    repository: DocumentRepository,
    extractor: TextExtractor,
    embedder: EmbeddingProvider,
    document_id: uuid.UUID,
    data: bytes,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> DocumentInfo:
    """Corre el pipeline completo sobre un documento registrado.

    Re-ejecutable (D03): cada pasada deja el mismo estado final.

    Args:
        repository: persistencia del agregado Documento.
        extractor: adapter de extracción (PDF / texto plano).
        embedder: adapter de embeddings (real o fake, D01).
        document_id: documento a procesar (creado por ``register_document``).
        data: contenido crudo del documento.
        chunk_size: presupuesto de caracteres por chunk (D11).
        chunk_overlap: solapamiento intra-página (D11).

    Returns:
        El documento actualizado (``ready`` con ``page_count``).

    Raises:
        FatalIngestionError: fallo sin reintento posible (el documento,
            si existe, ya quedó ``failed`` con la razón).
        Otras excepciones: transitorias — propagadas para que la tarea
            Celery decida reintentar (nunca marcan ``failed`` aquí).
    """
    document = repository.get(document_id)
    if document is None:
        # Nada que marcar: la tarea solo registra el suceso.
        raise FatalIngestionError(
            f"El documento {document_id} no existe en la base."
        )

    _begin_processing(repository, document)

    try:
        pages = extractor.extract(data, document.mime_type)
    except (ExtractionError, UnsupportedMimeTypeError) as exc:
        _fail(repository, document_id, str(exc))

    chunks = chunk_document(pages, chunk_size, chunk_overlap)
    if not chunks:
        _fail(
            repository,
            document_id,
            "El documento no contiene texto extraíble (¿es un PDF escaneado "
            "o un archivo solo con imágenes?). Aplique OCR y vuelva a "
            "subirlo, o suba directamente el texto.",
        )

    try:
        embeddings = embedder.embed([chunk.content for chunk in chunks])
    except EmbeddingDimensionError as exc:
        _fail(repository, document_id, str(exc))

    # El puerto promete un vector por texto (puerto EmbeddingProvider),
    # pero no se confía ciegamente en el proveedor: la base es vector(1024)
    # y un desajuste aquí corrompería la recuperación (D02/D10).
    if len(embeddings) != len(chunks):
        _fail(
            repository,
            document_id,
            f"El proveedor devolvió {len(embeddings)} embeddings para "
            f"{len(chunks)} chunks: no se puede persistir un mapeo parcial.",
        )
    if any(len(vector) != EMBEDDING_DIMS for vector in embeddings):
        _fail(
            repository,
            document_id,
            f"El proveedor devolvió embeddings con dimensión distinta a "
            f"{EMBEDDING_DIMS}: verifique EMBEDDING_MODEL (@cf/baai/bge-m3, "
            "decisión D10).",
        )

    repository.replace_chunks(
        document_id,
        [
            # zip strict: chunk i ↔ embedding i, mismo orden (puerto).
            NewChunk(
                page_number=chunk.page_number,
                ordinal=chunk.ordinal,
                content=chunk.content,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, embeddings, strict=True)
        ],
    )
    return repository.set_status(
        document_id, DocumentStatus.READY, page_count=len(pages)
    )


def _begin_processing(
    repository: DocumentRepository, document: DocumentInfo
) -> None:
    """Pasa el documento a ``processing`` validando la transición (D04).

    Reentrada permitida si ya está ``processing``: es el reintento de
    Celery reejecutando la misma tarea (D03) — no es una transición de
    negocio nueva, es la misma en curso.
    """
    if document.status == DocumentStatus.PROCESSING:
        return
    ensure_transition(document.status, DocumentStatus.PROCESSING)
    repository.set_status(document.id, DocumentStatus.PROCESSING)


def _fail(
    repository: DocumentRepository,
    document_id: uuid.UUID,
    reason: str,
) -> NoReturn:
    """Marca ``failed`` con la razón y lanza FatalIngestionError (no retorna)."""
    repository.set_status(
        document_id, DocumentStatus.FAILED, failure_reason=reason
    )
    raise FatalIngestionError(reason)
