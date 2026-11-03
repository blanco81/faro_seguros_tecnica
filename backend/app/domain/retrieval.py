"""Política de recuperación: responder o rechazarse (decisión D13).

Regla de todo-o-nada sobre el mejor resultado: se responde solo si el
chunk con MAYOR similitud alcanza el umbral. Si lo alcanza, la respuesta
se apoya en los ``top_k`` mejores chunks; si no, el sistema se rechaza
de forma explícita y NO entrega contextos débiles — nunca responde
"con lo que haya".

La similitud llega ya convertida por el adapter de base de datos:
pgvector devuelve distancia coseno y aquí entra ``1 - distancia``, es
decir, similitud coseno en [-1, 1].
"""

from dataclasses import dataclass

__all__ = [
    "REJECTION_MESSAGE",
    "RetrievalDecision",
    "RetrievedChunk",
    "decide_retrieval",
]

#: Mensaje de rechazo que la API devuelve tal cual (Fase 3).
#: Es deliberadamente explícito: dice QUÉ no se puede hacer y POR QUÉ.
REJECTION_MESSAGE = (
    "No tengo información suficiente en los documentos cargados para "
    "responder esa consulta con respaldo verificable. Pruebe reformular "
    "la pregunta o suba el documento que la cubra."
)


@dataclass(frozen=True)
class RetrievedChunk:
    """Chunk recuperado de la base vectorial, con su similitud."""

    document: str
    page: int
    content: str
    score: float  # similitud coseno [-1, 1] (1 - distancia de pgvector)


@dataclass(frozen=True)
class RetrievalDecision:
    """Resultado de la política: responder (o no) y con qué contexto."""

    answered: bool
    chunks: tuple[RetrievedChunk, ...]  # vacío cuando se rechaza
    best_score: float | None  # None solo si no hubo resultados
    reason: str  # "ok" | "no_results" | "below_threshold"
    message: str | None  # REJECTION_MESSAGE al rechazar; None al responder


def decide_retrieval(
    chunks: list[RetrievedChunk],
    threshold: float,
    top_k: int = 5,
) -> RetrievalDecision:
    """Decide si el sistema puede responder y con qué contexto.

    Args:
        chunks: candidatos recuperados (cualquier orden).
        threshold: similitud mínima (inclusive) del mejor chunk para
            poder responder. En [0, 1].
        top_k: cantidad de chunks que acompañan la respuesta.

    Raises:
        ValueError: si el umbral no está en [0, 1] o ``top_k`` no es
            positivo.
    """
    if not 0 <= threshold <= 1:
        raise ValueError(f"threshold debe estar en [0, 1] (recibido: {threshold})")
    if top_k <= 0:
        raise ValueError(f"top_k debe ser positivo (recibido: {top_k})")

    if not chunks:
        return RetrievalDecision(
            answered=False,
            chunks=(),
            best_score=None,
            reason="no_results",
            message=REJECTION_MESSAGE,
        )

    ranked = sorted(chunks, key=lambda chunk: chunk.score, reverse=True)
    best_score = ranked[0].score

    if best_score < threshold:
        return RetrievalDecision(
            answered=False,
            chunks=(),
            best_score=best_score,
            reason="below_threshold",
            message=REJECTION_MESSAGE,
        )

    return RetrievalDecision(
        answered=True,
        chunks=tuple(ranked[:top_k]),
        best_score=best_score,
        reason="ok",
        message=None,
    )
