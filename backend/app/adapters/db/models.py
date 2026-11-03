"""Modelos ORM (adapter de persistencia).

Vive en la capa de adapters: puede importar del dominio, nunca al revés.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.document import DocumentStatus

#: Dimensión fija de los embeddings (@cf/baai/bge-m3 en Workers AI, decision D10).
#: Multilingüe (español incluido). Cambiarla exige una migración explícita (D02).
EMBEDDING_DIMS = 1024


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mime_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DocumentStatus.PENDING,
        server_default="pending",
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMS))

    document: Mapped[Document] = relationship(back_populates="chunks")
