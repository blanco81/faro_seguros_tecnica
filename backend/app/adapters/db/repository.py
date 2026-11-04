"""Adapter SQLAlchemy del puerto DocumentRepository.

Detalles que fija este adapter (y que la suite no puede probar sin
PostgreSQL — los cubre el smoke test del stack real, Fase 2 paso 5):

- **``chunk_count`` en una sola consulta**: LEFT JOIN + COUNT con
  GROUP BY por la PK (PostgreSQL permite seleccionar el resto de
  columnas de la tabla agrupada por su clave). El listado no hace N+1.
- **``replace_chunks``**: DELETE + INSERT dentro de la transacción de
  la sesión inyectada. Quien abre la sesión (request HTTP o tarea
  Celery) decide el commit: un fallo intermedio no deja estado parcial
  (D03).
- **``delete``**: UN ``DELETE`` del documento; chunks y contenido
  caen por la FK ``ON DELETE CASCADE`` de la base (D30) — el smoke
  verifica esa cascada contra PostgreSQL de verdad.
- **El enum viaja por valor** (``values_callable``, migración 0001):
  lo que se lee de la base es un ``DocumentStatus`` del dominio, no un
  string suelto.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.adapters.db.models import Chunk, Document, DocumentContent
from app.domain.document import DocumentStatus
from app.ports.repository import DocumentInfo, NewChunk

__all__ = ["SqlAlchemyDocumentRepository"]


class SqlAlchemyDocumentRepository:
    """Implementa DocumentRepository contra PostgreSQL + pgvector."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- escrituras -----------------------------------------------------

    def create_document(
        self,
        filename: str,
        content_hash: str,
        mime_type: str,
        size_bytes: int,
    ) -> DocumentInfo:
        document = Document(
            filename=filename,
            content_hash=content_hash,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
        self._session.add(document)
        self._session.flush()  # sin commit: decide la sesión que envuelve
        return self._to_info(document, chunk_count=0)

    def set_status(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        *,
        failure_reason: str | None = None,
        page_count: int | None = None,
    ) -> DocumentInfo:
        document = self._session.get(Document, document_id)
        if document is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        document.status = DocumentStatus(status)
        # Volver a procesar limpia el motivo del fallo anterior.
        document.failure_reason = failure_reason
        if page_count is not None:
            document.page_count = page_count
        self._session.flush()
        return self._to_info(document, chunk_count=self._count_chunks(document_id))

    def replace_chunks(
        self, document_id: uuid.UUID, chunks: Sequence[NewChunk]
    ) -> int:
        if self._session.get(Document, document_id) is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        self._session.execute(
            delete(Chunk).where(Chunk.document_id == document_id)
        )
        ordenados = sorted(chunks, key=lambda chunk: chunk.ordinal)
        self._session.add_all(
            Chunk(
                document_id=document_id,
                page_number=chunk.page_number,
                content=chunk.content,
                embedding=chunk.embedding,
            )
            for chunk in ordenados
        )
        self._session.flush()
        return len(ordenados)

    def delete(self, document_id: uuid.UUID) -> bool:
        """Un solo ``DELETE FROM documents`` — el resto cae por FK CASCADE.

        Los chunks y el contenido no se tocan aquí: la FK
        ``ON DELETE CASCADE`` del esquema (migraciones 0001/0002) los
        tumba en la misma sentencia, sin N consultas ni estado parcial
        (D30). Lo que la suite no puede probar sin PostgreSQL — que la
        cascada de verdad dispare en la base real — lo cubre el smoke
        (sección 17: 0 chunks del documento tras el borrado).
        """
        resultado = self._session.execute(
            delete(Document).where(Document.id == document_id)
        )
        return bool(resultado.rowcount)

    def save_content(self, document_id: uuid.UUID, data: bytes) -> None:
        if self._session.get(Document, document_id) is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        if self._session.get(DocumentContent, document_id) is not None:
            raise ValueError(
                f"el documento {document_id} ya tiene contenido (relación 1:1, D15)"
            )
        self._session.add(DocumentContent(document_id=document_id, data=data))
        self._session.flush()

    def get_content(self, document_id: uuid.UUID) -> bytes | None:
        row = self._session.get(DocumentContent, document_id)
        return None if row is None else row.data

    # --- lecturas -------------------------------------------------------

    def find_by_hash(self, content_hash: str) -> DocumentInfo | None:
        statement = self._select_with_count().where(
            Document.content_hash == content_hash
        )
        row = self._session.execute(statement).first()
        return self._row_to_info(row) if row else None

    def get(self, document_id: uuid.UUID) -> DocumentInfo | None:
        statement = self._select_with_count().where(Document.id == document_id)
        row = self._session.execute(statement).first()
        return self._row_to_info(row) if row else None

    def list_documents(self) -> list[DocumentInfo]:
        statement = self._select_with_count().order_by(
            Document.created_at.desc(), Document.id
        )
        return [self._row_to_info(row) for row in self._session.execute(statement)]

    # --- helpers ----------------------------------------------------------

    def _select_with_count(self):
        """SELECT documento + COUNT(chunks) en una sola ida a la base."""
        return (
            select(Document, func.count(Chunk.id).label("chunk_count"))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .group_by(Document.id)
        )

    def _count_chunks(self, document_id: uuid.UUID) -> int:
        return int(
            self._session.scalar(
                select(func.count()).select_from(Chunk).where(
                    Chunk.document_id == document_id
                )
            )
        )

    def _row_to_info(self, row) -> DocumentInfo:
        document, chunk_count = row
        return self._to_info(document, chunk_count=int(chunk_count))

    def _to_info(self, document: Document, *, chunk_count: int) -> DocumentInfo:
        return DocumentInfo(
            id=document.id,
            filename=document.filename,
            content_hash=document.content_hash,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            status=DocumentStatus(document.status),
            page_count=document.page_count,
            failure_reason=document.failure_reason,
            created_at=document.created_at,
            updated_at=document.updated_at,
            chunk_count=chunk_count,
        )
