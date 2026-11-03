"""Tests de la política de recuperación y rechazo (decisión D13)."""

import pytest

from app.domain.retrieval import (
    REJECTION_MESSAGE,
    RetrievedChunk,
    decide_retrieval,
)


def _chunk(score: float, page: int = 1, document: str = "poliza.pdf") -> RetrievedChunk:
    return RetrievedChunk(
        document=document, page=page, content=f"contenido con score {score}", score=score
    )


# --- rechazos -------------------------------------------------------------


def test_sin_resultados_rechaza_de_forma_explicita():
    decision = decide_retrieval([], threshold=0.45)
    assert decision.answered is False
    assert decision.chunks == ()
    assert decision.best_score is None
    assert decision.reason == "no_results"
    assert decision.message == REJECTION_MESSAGE


def test_mejor_por_debajo_del_umbral_rechaza_sin_contexto_debil():
    decision = decide_retrieval([_chunk(0.30), _chunk(0.20)], threshold=0.45)
    assert decision.answered is False
    # Nunca responde "con lo que haya": no entrega chunks.
    assert decision.chunks == ()
    assert decision.best_score == 0.30
    assert decision.reason == "below_threshold"
    assert decision.message == REJECTION_MESSAGE


def test_mensaje_de_rechazo_es_util_y_sin_placeholders():
    assert len(REJECTION_MESSAGE) > 20
    assert "{" not in REJECTION_MESSAGE and "%" not in REJECTION_MESSAGE


# --- respuestas ------------------------------------------------------------


def test_mejor_exactamente_en_el_umbral_responde():
    # El umbral es inclusivo: alcanzarlo basta.
    decision = decide_retrieval([_chunk(0.45)], threshold=0.45)
    assert decision.answered is True
    assert decision.reason == "ok"
    assert decision.message is None


def test_responde_con_los_mejores_ordenados_por_similitud():
    candidates = [_chunk(0.50, page=3), _chunk(0.80, page=1), _chunk(0.65, page=2)]
    decision = decide_retrieval(candidates, threshold=0.45)

    assert decision.answered is True
    assert [chunk.score for chunk in decision.chunks] == [0.80, 0.65, 0.50]
    assert decision.best_score == 0.80


def test_top_k_limita_el_contexto_de_la_respuesta():
    candidates = [_chunk(0.50 + i / 100) for i in range(10)]
    decision = decide_retrieval(candidates, threshold=0.45, top_k=3)

    assert len(decision.chunks) == 3
    assert decision.chunks[0].score == max(chunk.score for chunk in candidates)


def test_empates_mantienen_el_orden_de_entrada():
    candidates = [_chunk(0.60, page=i) for i in range(1, 4)]
    decision = decide_retrieval(candidates, threshold=0.45)
    assert [chunk.page for chunk in decision.chunks] == [1, 2, 3]


# --- validaciones -----------------------------------------------------------


def test_umbral_fuera_de_rango_rechazado():
    with pytest.raises(ValueError):
        decide_retrieval([_chunk(0.9)], threshold=1.5)
    with pytest.raises(ValueError):
        decide_retrieval([_chunk(0.9)], threshold=-0.1)


def test_top_k_no_positivo_rechazado():
    with pytest.raises(ValueError):
        decide_retrieval([_chunk(0.9)], threshold=0.45, top_k=0)
