"""Contenido crudo del documento: tabla propia 1:1 (decisión D15).

El worker necesita los bytes para procesar, pero NO viajan por el broker
(20 MB en base64 dentro de Redis, reenviados en cada reintento) ni por un
volumen compartido (acopla api y worker al mismo host): viajan por la base,
en una tabla aparte para no arrastrar el blob en cada lectura de documents
(``list_documents``/``get`` nunca la tocan).

PK = FK a documents: relación 1:1, se inserta en la MISMA transacción que
el alta del documento (register_document) y muere con él (CASCADE).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_contents",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", postgresql.BYTEA(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_contents")
