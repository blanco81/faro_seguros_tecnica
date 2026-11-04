"""Métricas de recuperación del set de evaluación (D28, cierra la deuda D20).

D20 calibró el umbral 0.45 con los dos casos polo del smoke; estas métricas
son la versión formal que quedó prometida: un set etiquetado y tres medidas
sobre la lista rankeada que devuelve ``RetrievalPort``.

Granularidad de la etiqueta = PÁGINA (la de la cita navegable, D12/D21):
un chunk recuperado es relevante si su página contiene la cláusula que
responde la pregunta. La página —no el chunk— es lo estable frente a
cambios de ``chunk_size``/``chunk_overlap`` (D11) y es lo que el analista
verifica con un clic en el frontend.

- ``precision_at_k``: de los k chunks recuperados, qué fracción cae en
  páginas relevantes. El denominador es SIEMPRE k (el clásico de IR):
  los puestos no recuperados no pueden ser relevantes.
- ``recall_at_k``: de las páginas relevantes, qué fracción aparece entre
  las páginas de los top-k. Con el conjunto relevante vacío no se define
  (es un caso fuera de dominio: eso lo mide el umbral, no P/R).
- ``evidence_hit_at_k``: ¿está TODA la evidencia de la respuesta (los
  snippets etiquetados, verbatim y normalizando espacios igual que la
  cita D12) dentro de los top-k? Es la métrica operativa: si falla, el
  bucle agéntico no tiene con qué responder, tenga la página o no.
"""

from collections.abc import Sequence

from app.domain.retrieval import RetrievedChunk

__all__ = [
    "evidence_hit_at_k",
    "normaliza_texto",
    "precision_at_k",
    "recall_at_k",
]


def normaliza_texto(texto: str) -> str:
    """Colapsa todo el whitespace a espacios simples (para comparar verbatim).

    La MISMA normalización del verificador de citas del servicio de consulta
    (D12): lo que vale para verificar una cita del LLM vale para verificar
    una etiqueta del set — una sola definición de "literal" en el sistema.
    """
    return " ".join(texto.split())


def precision_at_k(
    chunks: Sequence[RetrievedChunk], relevant_pages: frozenset[int], k: int
) -> float:
    """Fracción de los top-k chunks cuya página es relevante.

    Args:
        chunks: lista rankeada (mejor primero; el orden lo da el retriever).
        relevant_pages: páginas que contienen la respuesta (etiqueta del set).
        k: corte. El denominador es k aunque el sistema devuelva menos
            chunks: los puestos vacíos cuentan como no relevantes.

    Raises:
        ValueError: si ``k`` no es positivo o ``relevant_pages`` está vacío
            (un caso sin páginas relevantes está fuera de dominio: se mide
            por el umbral D13, no por precisión).
    """
    _valida_k(k)
    if not relevant_pages:
        raise ValueError(
            "precision_at_k sin páginas relevantes: es un caso fuera de "
            "dominio — se mide por el umbral (D13), no por precisión"
        )
    top = list(chunks)[:k]
    aciertos = sum(1 for chunk in top if chunk.page in relevant_pages)
    return aciertos / k


def recall_at_k(
    chunks: Sequence[RetrievedChunk], relevant_pages: frozenset[int], k: int
) -> float:
    """Fracción de las páginas relevantes presentes en los top-k.

    La unidad es la PÁGINA: dos chunks de la misma página relevante cuentan
    una vez — la respuesta está alcanzada cuando su página aparece, no hace
    falta recuperarla dos veces.

    Raises:
        ValueError: igual que ``precision_at_k`` (k no positivo, o conjunto
            relevante vacío).
    """
    _valida_k(k)
    if not relevant_pages:
        raise ValueError(
            "recall_at_k sin páginas relevantes: es un caso fuera de dominio "
            "— se mide por el umbral (D13), no por recall"
        )
    paginas = {chunk.page for chunk in list(chunks)[:k]}
    return len(paginas & relevant_pages) / len(relevant_pages)


def evidence_hit_at_k(
    chunks: Sequence[RetrievedChunk], snippets: Sequence[str], k: int
) -> bool:
    """¿Los top-k contienen TODA la evidencia de la respuesta, verbatim?

    Cada snippet debe aparecer en algún chunk de los top-k (pueden ser
    chunks distintos: una respuesta que necesita dos cláusulas está en dos
    chunks), con la normalización de espacios de la cita (D12). Un caso con
    varios snippets los exige TODOS: la respuesta no está completa con una
    pieza suelta.

    Raises:
        ValueError: si ``k`` no es positivo, no hay snippets (etiqueta sin
            evidencia) o alguno queda vacío tras normalizar.
    """
    _valida_k(k)
    if not snippets:
        raise ValueError("evidence_hit_at_k sin snippets: etiqueta sin evidencia")
    normalizados = [normaliza_texto(snippet) for snippet in snippets]
    if any(not snippet for snippet in normalizados):
        raise ValueError("evidence_hit_at_k con snippet vacío (etiqueta inválida)")
    contenidos = [normaliza_texto(chunk.content) for chunk in list(chunks)[:k]]
    return all(
        any(snippet in contenido for contenido in contenidos)
        for snippet in normalizados
    )


def _valida_k(k: int) -> None:
    if k <= 0:
        raise ValueError(f"k debe ser positivo (recibido: {k})")
