"""Puerto DocumentRepository: persistencia de documentos y sus chunks.

Contrato de la Fase 2 (ingesta) que reutiliza la Fase 3 (consulta). Los
servicios hablan de documentos y chunks a través de este puerto; el
adapter SQLAlchemy (``adapters/db/repository.py``) lo implementa contra
PostgreSQL + pgvector. Así la regla hexagonal llega hasta el final: ni
el servicio de ingesta ni la API tocan el ORM.

Decisiones que fija el contrato:

- **Las lecturas devuelven ``DocumentInfo``**, un registro inmutable del
  agregado con su ``chunk_count``: una sola consulta, sin N+1, y sin
  filtrar objetos ORM hacia la capa de negocio.
- **``replace_chunks`` ES la idempotencia de reintento (D03)**: borra
  los chunks previos del documento e inserta los nuevos dentro de la
  transacción de la sesión inyectada. Reejecutar la ingesta deja
  siempre el mismo estado final — nunca chunks duplicados ni huérfanos.
- **Las transiciones de estado NO se validan aquí**: las valida el
  dominio (``ensure_transition``, D04) antes de llamar a ``set_status``.
  El puerto persiste lo que le mandan; el dominio decide qué es legal.
- **El contenido crudo vive en el agregado** (D15): ``save_content`` /
  ``get_content`` lo guardan y lo leen. La tarea Celery solo recibe el
  ``document_id`` — los bytes jamás pasan por el broker.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.document import DocumentStatus
from app.ports.embeddings import Embedding

__all__ = ["DocumentInfo", "DocumentRepository", "NewChunk"]


@dataclass(frozen=True)
class DocumentInfo:
    """Vista persistida de un documento, con el conteo de sus chunks.

    ``page_count`` es ``None`` hasta que la ingesta termina; ``status``
    y ``failure_reason`` reflejan el ciclo de vida del enunciado (D04).
    """

    id: uuid.UUID
    filename: str
    content_hash: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    chunk_count: int = 0


@dataclass(frozen=True)
class NewChunk:
    """Chunk listo para persistir: página, contenido y su embedding.

    ``ordinal`` es la posición global del chunk en el documento (D11):
    ordena la inserción de forma estable en ``replace_chunks``. El
    esquema no guarda columna de orden — la recuperación RAG cita por
    página, no por posición.
    """

    page_number: int
    ordinal: int
    content: str
    embedding: Embedding


@runtime_checkable
class DocumentRepository(Protocol):
    """Lee y escribe el agregado Documento (tabla + chunks)."""

    def create_document(
        self,
        filename: str,
        content_hash: str,
        mime_type: str,
        size_bytes: int,
    ) -> DocumentInfo:
        """Registra un documento nuevo en estado ``pending``.

        Raises:
            ValueError: si ya existe un documento con ese ``content_hash``
                (el UNIQUE del esquema, D03) o faltan campos obligatorios.
        """
        ...

    def find_by_hash(self, content_hash: str) -> DocumentInfo | None:
        """Busca por hash de contenido (idempotencia de subida, D03)."""
        ...

    def get(self, document_id: uuid.UUID) -> DocumentInfo | None:
        """Detalle de un documento, con su ``chunk_count``."""
        ...

    def list_documents(self) -> list[DocumentInfo]:
        """Todos los documentos, el más reciente primero."""
        ...

    def set_status(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        *,
        failure_reason: str | None = None,
        page_count: int | None = None,
    ) -> DocumentInfo:
        """Actualiza el estado (y opcionalmente motivo/páginas).

        Asignar ``failure_reason=None`` limpia un motivo anterior al
        volver a procesar; ``page_count`` solo se toca si llega un valor.

        Raises:
            ValueError: si el documento no existe.
        """
        ...

    def replace_chunks(
        self, document_id: uuid.UUID, chunks: Sequence[NewChunk]
    ) -> int:
        """Sustituye ATÓMICAMENTE todos los chunks del documento.

        Borra los previos e inserta los nuevos (ordenados por
        ``ordinal``) en la transacción de la sesión inyectada. Es la
        pieza que hace reintentable la ingesta (D03).

        Returns:
            La cantidad de chunks insertados.

        Raises:
            ValueError: si el documento no existe.
        """
        ...

    def save_content(self, document_id: uuid.UUID, data: bytes) -> None:
        """Guarda el contenido crudo del archivo (D15).

        Se llama UNA vez, en la misma transacción que ``create_document``
        (lo hace ``register_document``): si la transacción aborta, no
        queda documento sin contenido ni contenido huérfano. La relación
        es 1:1 — no hay caso de uso para un segundo contenido.

        Raises:
            ValueError: si el documento no existe o ya tenía contenido.
        """
        ...

    def get_content(self, document_id: uuid.UUID) -> bytes | None:
        """Lee el contenido crudo guardado en ``save_content``.

        La tarea Celery solo recibe el ``document_id`` (D15): con este
        método el worker recupera los bytes sin que hayan pasado por el
        broker, y reintentos/reprocesos (D04) no exigen re-subir el
        archivo. ``None`` si el documento no existe o se registró sin
        contenido.
        """
        ...
