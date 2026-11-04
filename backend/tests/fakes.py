"""Dobles de test del puerto DocumentRepository.

La suite corre sin PostgreSQL (D01): los tests de ingesta usan este doble
en lugar del adapter SQLAlchemy. Reproduce fielmente las piezas del
contrato que en la base real garantizan el esquema y el ORM:

- ``content_hash`` UNIQUE (migración 0001, D03): crear con un hash
  existente lanza ``ValueError``, como la ``IntegrityError`` de
  PostgreSQL — el doble no encubre duplicados.
- ``replace_chunks`` SUSTITUYE: una asignación de lista hace el papel
  del DELETE + INSERT del adapter (idempotencia de reintento).
- ``chunk_count`` se calcula al vuelo en cada lectura, como el COUNT
  del adapter.
- ``save_content`` es 1:1 (D15): segundo contenido para el mismo
  documento lanza ``ValueError``, como la PK duplicada de la tabla
  ``document_contents``.
- ``delete`` modela la FK ``ON DELETE CASCADE`` (D30): la fila entera
  — documento, chunks y contenido — vive junta, así que sacarla del
  dict es el borrado en cascada del adapter. Y como en la base real,
  el ``content_hash`` UNIQUE (D03) queda libre: los mismos bytes pueden
  registrarse de nuevo tras borrar.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.domain.document import DocumentStatus
from app.domain.retrieval import RetrievedChunk
from app.ports.repository import DocumentInfo, NewChunk

__all__ = ["FakeDocumentRepository", "FakeRetriever", "ScriptedLLM", "StubEmbedder"]


def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Fila:
    """Fila en memoria: el documento persistido, sus chunks y su contenido."""

    info: DocumentInfo
    chunks: list[NewChunk] = field(default_factory=list)
    contenido: bytes | None = None


class FakeDocumentRepository:
    """DocumentRepository en memoria (dict por UUID del documento)."""

    def __init__(self) -> None:
        self._filas: dict[uuid.UUID, _Fila] = {}

    # --- escrituras -----------------------------------------------------

    def create_document(
        self,
        filename: str,
        content_hash: str,
        mime_type: str,
        size_bytes: int,
    ) -> DocumentInfo:
        if self.find_by_hash(content_hash) is not None:
            raise ValueError(
                f"content_hash duplicado: {content_hash} (UNIQUE, decisión D03)"
            )
        ahora = _ahora()
        info = DocumentInfo(
            id=uuid.uuid4(),
            filename=filename,
            content_hash=content_hash,
            mime_type=mime_type,
            size_bytes=size_bytes,
            status=DocumentStatus.PENDING,
            page_count=None,
            failure_reason=None,
            created_at=ahora,
            updated_at=ahora,
        )
        self._filas[info.id] = _Fila(info=info)
        return self._info(info.id)

    def set_status(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        *,
        failure_reason: str | None = None,
        page_count: int | None = None,
    ) -> DocumentInfo:
        fila = self._filas.get(document_id)
        if fila is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        nuevo_page_count = (
            page_count if page_count is not None else fila.info.page_count
        )
        fila.info = replace(
            fila.info,
            status=status,
            failure_reason=failure_reason,
            page_count=nuevo_page_count,
            updated_at=_ahora(),
        )
        return self._info(document_id)

    def replace_chunks(
        self, document_id: uuid.UUID, chunks: Sequence[NewChunk]
    ) -> int:
        fila = self._filas.get(document_id)
        if fila is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        # DELETE + INSERT: sustituye la lista, nunca concatena (D03).
        fila.chunks = list(chunks)
        return len(fila.chunks)

    def delete(self, document_id: uuid.UUID) -> bool:
        # ON DELETE CASCADE (D30): chunks y contenido viven DENTRO de la
        # fila — sacarla del dict tumba el agregado entero, como la FK
        # de la base real tumba chunks y document_contents.
        return self._filas.pop(document_id, None) is not None

    def save_content(self, document_id: uuid.UUID, data: bytes) -> None:
        fila = self._filas.get(document_id)
        if fila is None:
            raise ValueError(f"documento no encontrado: {document_id}")
        if fila.contenido is not None:
            # PK duplicada en document_contents (1:1, D15).
            raise ValueError(
                f"el documento {document_id} ya tiene contenido "
                "(relación 1:1, D15)"
            )
        fila.contenido = data

    def get_content(self, document_id: uuid.UUID) -> bytes | None:
        fila = self._filas.get(document_id)
        return None if fila is None else fila.contenido

    # --- lecturas ---------------------------------------------------------

    def find_by_hash(self, content_hash: str) -> DocumentInfo | None:
        for fila in self._filas.values():
            if fila.info.content_hash == content_hash:
                return self._info(fila.info.id)
        return None

    def get(self, document_id: uuid.UUID) -> DocumentInfo | None:
        return self._info(document_id) if document_id in self._filas else None

    def chunks_of(self, document_id: uuid.UUID) -> list[NewChunk]:
        """Inspección para tests: los chunks persistidos, en orden de inserción."""
        return list(self._filas[document_id].chunks)

    def list_documents(self) -> list[DocumentInfo]:
        # El más reciente primero, como el adapter (created_at DESC):
        # dict conserva el orden de inserción, reversed() lo invierte.
        return [self._info(id_) for id_ in reversed(self._filas)]

    # --- helpers ------------------------------------------------------------

    def _info(self, document_id: uuid.UUID) -> DocumentInfo:
        """DocumentInfo con ``chunk_count`` vivo (como el COUNT del adapter)."""
        fila = self._filas[document_id]
        return replace(fila.info, chunk_count=len(fila.chunks))


# --- Fase 3: dobles de consulta -------------------------------------------


class ScriptedLLM:
    """LLMProvider con respuestas encoladas: una por llamada, en orden.

    Permite programar el desenlace de cada paso del bucle agéntico
    (planificar / responder / refinar) sin red ni proveedor real. Si el
    servicio hace una llamada de más, estalla en vez de colarse.
    """

    def __init__(self, *respuestas: str) -> None:
        self.respuestas: list[str] = list(respuestas)
        self.prompts: list[tuple[str | None, str]] = []

    def chat(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append((system, prompt))
        if not self.respuestas:
            raise AssertionError("ScriptedLLM se quedó sin respuestas encoladas")
        return self.respuestas.pop(0)


class FakeRetriever:
    """RetrievalPort con resultados fijos e inspección de llamadas."""

    def __init__(self, resultados: Sequence[RetrievedChunk]) -> None:
        self.resultados = list(resultados)
        self.llamadas: list[dict] = []

    def search(
        self, query_embedding, *, top_k: int, document_id=None
    ) -> list[RetrievedChunk]:
        self.llamadas.append({"top_k": top_k, "document_id": document_id})
        return self.resultados[:top_k]


class StubEmbedder:
    """EmbeddingProvider trivial: un vector distinto por posición, texto da igual."""

    def __init__(self, dims: int = 4) -> None:
        self.dims = dims
        self.textos: list[str] = []

    def embed(self, texts):
        self.textos.extend(texts)
        return [[0.1 * (i + 1)] * self.dims for i, _ in enumerate(texts)]
