"""Tests del agente de consulta (Fase 3, D19).

El bucle completo con dobles: ``ScriptedLLM`` programa el desenlace de
cada paso (planificar / responder / refinar), ``FakeRetriever`` devuelve
chunks fijos y ``StubEmbedder`` da vectores cualquiera. Sin red, sin
base, sin claves (D01).

Los casos cubren las tres salidas del bucle (responder / rehusar /
pedir aclaración), los dos motivos de refinamiento (cita sin verificar,
evidencia bajo el umbral) y el techo de iteraciones.
"""

import json
import uuid

import pytest

from app.adapters.llm.fake_llm import FakeLLMProvider
from app.domain.retrieval import REJECTION_MESSAGE, RetrievedChunk
from app.services.query import (
    MAX_QUERY_ITERATIONS,
    UNVERIFIED_MESSAGE,
    AgenticQueryService,
)
from tests.fakes import FakeRetriever, ScriptedLLM, StubEmbedder

PREGUNTA = "¿Cuál es la franquicia aplicable a los daños por agua?"

CHUNK_AGUA = RetrievedChunk(
    document="poliza-hogar.pdf",
    page=2,
    content=(
        "Cláusula 7.4 — Franquicia. Para la cobertura de daños por agua "
        "se aplica una franquicia de 300 euros por siniestro."
    ),
    score=0.87,
)
CITA_VERBATIM = "se aplica una franquicia de 300 euros por siniestro"


def _json(**datos):
    return json.dumps(datos)


def _servicio(llm, chunks, *, umbral=0.45):
    return AgenticQueryService(
        llm=llm,
        embedder=StubEmbedder(),
        retriever=FakeRetriever(chunks),
        rejection_threshold=umbral,
    )


def _agente(llm, retriever, *, umbral=0.45):
    return AgenticQueryService(
        llm=llm,
        embedder=StubEmbedder(),
        retriever=retriever,
        rejection_threshold=umbral,
    )


# --- responder ------------------------------------------------------------


def test_responde_en_una_iteracion_con_cita_verificada():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(
            answer="La franquicia es de 300 euros por siniestro.",
            quote=CITA_VERBATIM,
        ),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is False
    assert resultado.answer == "La franquicia es de 300 euros por siniestro."
    assert resultado.iterations == 1
    assert resultado.trace == ()
    (cita,) = resultado.citations
    assert cita.document == "poliza-hogar.pdf"
    assert cita.page == 2
    assert cita.quote == CITA_VERBATIM


def test_cita_parafraseada_refina_y_verifica_en_la_segunda_iteracion():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(
            answer="Unos trescientos euros.",
            quote="una franquicia de unos trescientos euros",  # paráfrasis: NO está en el chunk
        ),
        _json(query="franquicia de la cláusula 7.4 de daños por agua"),
        _json(
            answer="La franquicia es de 300 euros.",
            quote=CITA_VERBATIM,
        ),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is False
    assert resultado.iterations == 2
    assert list(resultado.trace) == [{"iteracion": 1, "motivo": "citation_mismatch"}]


def test_la_verificacion_toleran_diferencias_de_whitespace():
    """El LLM copia bien las palabras pero con saltos de línea distintos."""
    cita_con_saltos = "se aplica una franquicia\nde 300 euros   por siniestro"
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(answer="300 euros.", quote=cita_con_saltos),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is False
    assert resultado.citations[0].page == 2


def test_subpreguntas_deduplican_chunks_repetidos():
    llm = ScriptedLLM(
        _json(subquestions=["¿franquicia de daños por agua?", "¿qué dice la cláusula 7.4?"]),
        _json(answer="300 euros.", quote=CITA_VERBATIM),
    )
    retriever = FakeRetriever([CHUNK_AGUA])
    resultado = _agente(llm, retriever).answer(PREGUNTA)

    assert resultado.refused is False
    assert len(retriever.llamadas) == 2  # una búsqueda por sub-pregunta
    assert len(resultado.citations) == 1  # el chunk duplicado se fusionó


def test_el_document_id_se_reenvia_al_retriever():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(answer="300 euros.", quote=CITA_VERBATIM),
    )
    retriever = FakeRetriever([CHUNK_AGUA])
    objetivo = uuid.uuid4()
    _agente(llm, retriever).answer(PREGUNTA, document_id=objetivo)
    assert retriever.llamadas[0]["document_id"] == objetivo


# --- rehusar ----------------------------------------------------------------


def test_sin_resultados_rechaza_inmediatamente_sin_refinar():
    llm = ScriptedLLM(_json(subquestions=[PREGUNTA]))
    resultado = _servicio(llm, []).answer(PREGUNTA)

    assert resultado.refused is True
    assert resultado.reason == "no_results"
    assert resultado.message == REJECTION_MESSAGE
    assert resultado.iterations == 1
    assert len(llm.prompts) == 1  # solo el planificador: no hay nada que refinar


def test_bajo_umbral_agota_las_iteraciones_y_rechaza():
    chunk_flojo = RetrievedChunk(
        document=CHUNK_AGUA.document,
        page=CHUNK_AGUA.page,
        content=CHUNK_AGUA.content,
        score=0.30,  # < umbral 0.45: evidencia insuficiente (D13)
    )
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(query="reformulación 1"),
        _json(query="reformulación 2"),
    )
    resultado = _servicio(llm, [chunk_flojo]).answer(PREGUNTA)

    assert resultado.refused is True
    assert resultado.reason == "below_threshold"
    assert resultado.message == REJECTION_MESSAGE
    assert resultado.iterations == MAX_QUERY_ITERATIONS
    assert [paso["motivo"] for paso in resultado.trace] == ["below_threshold"] * 3


def test_techo_por_cita_no_verificada_rechaza_con_motivo_especifico():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(answer="a", quote="cita inventada 1"),
        _json(query="r1"),
        _json(answer="a", quote="cita inventada 2"),
        _json(query="r2"),
        _json(answer="a", quote="cita inventada 3"),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is True
    assert resultado.reason == "unverified_citation"
    assert resultado.message == UNVERIFIED_MESSAGE
    assert resultado.iterations == 3
    assert len(resultado.trace) == 3


def test_pregunta_vacia_rechaza_sin_llamar_al_proveedor():
    llm = ScriptedLLM()
    resultado = _servicio(llm, [CHUNK_AGUA]).answer("   ")

    assert resultado.refused is True
    assert resultado.reason == "empty_question"
    assert resultado.iterations == 0
    assert llm.prompts == []


# --- pedir aclaración --------------------------------------------------------


def test_el_plan_pide_aclaracion_sin_buscar_nada():
    llm = ScriptedLLM(
        _json(clarification="¿Te refieres a la póliza de hogar o a la de auto?")
    )
    retriever = FakeRetriever([CHUNK_AGUA])
    resultado = _agente(llm, retriever).answer("¿Qué cubre la póliza?")

    assert resultado.clarification == "¿Te refieres a la póliza de hogar o a la de auto?"
    assert resultado.refused is False
    assert resultado.iterations == 0
    assert retriever.llamadas == []  # ambigüedad: ni se recuperó


# --- degradaciones con gracia -------------------------------------------------


def test_json_irrecuperable_del_modelo_refina_y_recupera():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        "lo siento, no puedo responder en JSON",  # sin objeto parseable
        _json(query="reformulada"),
        _json(answer="La franquicia es de 300 euros.", quote=CITA_VERBATIM),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is False
    assert resultado.iterations == 2
    assert resultado.trace[0]["motivo"] == "empty_or_unparseable"


def test_respuesta_vacia_del_modelo_refina():
    llm = ScriptedLLM(
        _json(subquestions=[PREGUNTA]),
        _json(answer="", quote=""),  # el modelo dice: el contexto no responde
        _json(query="reformulada"),
        _json(answer="La franquicia es de 300 euros.", quote=CITA_VERBATIM),
    )
    resultado = _servicio(llm, [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.iterations == 2
    assert resultado.trace[0]["motivo"] == "empty_or_unparseable"


# --- contrato de construcción y modo simulado ---------------------------------


def test_umbral_fuera_de_rango_no_se_construye():
    with pytest.raises(ValueError, match="rejection_threshold"):
        AgenticQueryService(
            llm=ScriptedLLM(),
            embedder=StubEmbedder(),
            retriever=FakeRetriever([]),
            rejection_threshold=1.5,
        )


def test_el_fake_del_stack_completa_el_bucle_de_punta_a_punta():
    """El proveedor simulado del enunciado (D14) también pasa por el bucle.

    Valida que FakeLLMProvider extrae la cita del contexto que el propio
    servicio le envía: el modo fake funciona de verdad, no solo importa.
    """
    resultado = _servicio(FakeLLMProvider(), [CHUNK_AGUA]).answer(PREGUNTA)

    assert resultado.refused is False
    assert resultado.citations, "el fake debe citar el fragmento del contexto"
    assert resultado.citations[0].document == "poliza-hogar.pdf"
    assert resultado.citations[0].page == 2
