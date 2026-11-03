"""Puerto RetrievalPort: búsqueda semántica sobre los chunks (Fase 3).

La Fase 1 definió la POLÍTICA de recuperación (``decide_retrieval``, D13:
todo-o-nada sobre el mejor resultado); este puerto es la PIEZA que faltaba:
de dónde salen los candidatos. La separación es la misma de siempre — el
puerto entrega candidatos con su similitud, el dominio decide si alcanzan
para responder.

El tipo de retorno es el ``RetrievedChunk`` del dominio (Fase 1): el
adapter resuelve el JOIN con ``documents`` para entregar el nombre del
archivo, porque la cita (D12) cita documentos legibles, no UUIDs.
"""

import uuid
from typing import Protocol, runtime_checkable

from app.domain.retrieval import RetrievedChunk
from app.ports.embeddings import Embedding

__all__ = ["RetrievalPort"]


@runtime_checkable
class RetrievalPort(Protocol):
    """Busca los chunks más parecidos a un embedding de consulta."""

    def search(
        self,
        query_embedding: Embedding,
        *,
        top_k: int,
        document_id: uuid.UUID | None = None,
    ) -> list[RetrievedChunk]:
        """Top-``top_k`` chunks por similitud coseno (mayor primero).

        Args:
            query_embedding: embedding de la consulta. Debe venir del
                MISMO modelo con el que se ingirieron los chunks
                (bge-m3, D10): un espacio de embeddings distinto haría
                la similitud incomparable (D02).
            top_k: máximo de resultados a devolver.
            document_id: si llega, acota la búsqueda a ese documento
                (el analista puede preguntar sobre una póliza concreta).

        Returns:
            Los chunks con su similitud coseno ya convertida (el adapter
            traduce la distancia de pgvector: ``1 - distancia``), sin
            filtrar por umbral — eso es decisión del dominio (D13).
        """
        ...
