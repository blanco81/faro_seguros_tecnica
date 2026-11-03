"""Adapter de recuperación: PostgreSQL + pgvector (Fase 3).

La pieza que faltaba del rompecabezas D13: la Fase 1 definió la política
(``decide_retrieval``) y este adapter le da candidatos reales. Un solo
SELECT con JOIN — el índice HNSW (D02) hace el trabajo pesado.

La traducción de métricas ocurre AQUÍ, en el borde: pgvector devuelve
distancia coseno (``<=>``, 0 = idéntico) y el puerto promete similitud
(``1 - distancia``), que es lo que el dominio compara contra el umbral.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.models import Chunk, Document
from app.domain.retrieval import RetrievedChunk
from app.ports.embeddings import Embedding

__all__ = ["PgVectorRetriever"]


class PgVectorRetriever:
    """RetrievalPort sobre la tabla ``chunks`` (vector 1024 + HNSW, D02).

    Args:
        session: sesión SQLAlchemy inyectada (la abre quien compone: la
            API en el endpoint de consulta, con su propio ``session_scope``).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(
        self,
        query_embedding: Embedding,
        *,
        top_k: int,
        document_id: uuid.UUID | None = None,
    ) -> list[RetrievedChunk]:
        """Top-``top_k`` por similitud coseno; el umbral lo decide el dominio."""
        distancia = Chunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(
                Document.filename,
                Chunk.page_number,
                Chunk.content,
                (1.0 - distancia).label("score"),
            )
            .join(Document, Chunk.document_id == Document.id)
            .order_by(distancia)
            .limit(top_k)
        )
        if document_id is not None:
            stmt = stmt.where(Chunk.document_id == document_id)

        filas = self._session.execute(stmt).all()
        return [
            RetrievedChunk(
                document=nombre, page=pagina, content=contenido, score=float(puntaje)
            )
            for nombre, pagina, contenido, puntaje in filas
        ]
