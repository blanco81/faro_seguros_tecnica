"""Tests del endpoint POST /api/query (Fase 3, D19).

Contrato HTTP con el servicio sustituido por un doble (dependency
overriding): la ruta no toca base ni proveedor — solo serializa.

Lo que se valida aquí es el BORDE: códigos de estado correctos para cada
desenlace (200 responder/rehusar/aclarar, 422 contrato, 503 proveedor) y
que nada se pierda en la serialización (la cita llega completa).
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.adapters.http.query import get_query_service
from app.domain.citations import Citation
from app.domain.retrieval import REJECTION_MESSAGE
from app.main import app
from app.services.query import QueryAnswer

client = TestClient(app)


class _ServicioFijo:
    """Doble del agente: devuelve un resultado prefijado y registra la consulta."""

    def __init__(self, resultado: QueryAnswer) -> None:
        self.resultado = resultado
        self.consultado: tuple[str, uuid.UUID | None] | None = None

    def answer(self, question: str, *, document_id=None) -> QueryAnswer:
        self.consultado = (question, document_id)
        return self.resultado


class _ServicioCaido:
    """Doble que simula un proveedor caído (excepción de transporte)."""

    def answer(self, question: str, *, document_id=None) -> QueryAnswer:
        raise RuntimeError("Connection error.")


@pytest.fixture(autouse=True)
def _limpiar_overrides():
    yield
    app.dependency_overrides.clear()


def _override(servicio) -> None:
    app.dependency_overrides[get_query_service] = lambda: servicio


def _respuesta_ok() -> QueryAnswer:
    return QueryAnswer(
        answer="La franquicia es de 300 euros por siniestro.",
        citations=(
            Citation(
                document="poliza-hogar.pdf",
                page=2,
                quote="se aplica una franquicia de 300 euros por siniestro",
            ),
        ),
        refused=False,
        reason=None,
        message=None,
        clarification=None,
        iterations=1,
        trace=(),
    )


def test_respuesta_con_cita_verificada_es_200():
    _override(_ServicioFijo(_respuesta_ok()))
    respuesta = client.post("/api/query", json={"question": "¿Cuál es la franquicia?"})

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["answer"] == "La franquicia es de 300 euros por siniestro."
    assert cuerpo["refused"] is False
    assert cuerpo["iterations"] == 1
    (cita,) = cuerpo["citations"]
    assert cita == {
        "document": "poliza-hogar.pdf",
        "page": 2,
        "quote": "se aplica una franquicia de 300 euros por siniestro",
    }


def test_el_rechazo_es_200_con_razon_y_mensaje():
    resultado = QueryAnswer(
        answer=None,
        citations=(),
        refused=True,
        reason="below_threshold",
        message=REJECTION_MESSAGE,
        clarification=None,
        iterations=3,
        trace=(),
    )
    _override(_ServicioFijo(resultado))
    respuesta = client.post("/api/query", json={"question": "¿algo sin cobertura?"})

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["refused"] is True
    assert cuerpo["answer"] is None
    assert cuerpo["reason"] == "below_threshold"
    assert cuerpo["message"] == REJECTION_MESSAGE


def test_la_aclaracion_es_200_sin_respuesta_ni_rechazo():
    resultado = QueryAnswer(
        answer=None,
        citations=(),
        refused=False,
        reason=None,
        message=None,
        clarification="¿Hogar o auto?",
        iterations=0,
        trace=(),
    )
    _override(_ServicioFijo(resultado))
    respuesta = client.post("/api/query", json={"question": "¿Qué cubre la póliza?"})

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["clarification"] == "¿Hogar o auto?"
    assert cuerpo["refused"] is False
    assert cuerpo["answer"] is None


def test_pregunta_corta_es_422():
    # Se sobrescribe igual: FastAPI construye dependencias ANTES de
    # validar el cuerpo, y el contrato que se prueba aquí es el 422.
    _override(_ServicioFijo(_respuesta_ok()))
    respuesta = client.post("/api/query", json={"question": "ho"})
    assert respuesta.status_code == 422


def test_pregunta_ausente_es_422():
    _override(_ServicioFijo(_respuesta_ok()))
    respuesta = client.post("/api/query", json={})
    assert respuesta.status_code == 422


def test_proveedor_caido_es_503_y_no_un_rechazo():
    """Transporte roto ≠ rechazo del dominio: nunca se confunden (D19)."""
    _override(_ServicioCaido())
    respuesta = client.post("/api/query", json={"question": "¿Cuál es la franquicia?"})

    assert respuesta.status_code == 503
    assert "no está disponible" in respuesta.json()["detail"]


def test_el_document_id_viaja_hasta_el_servicio():
    objetivo = uuid.uuid4()
    servicio = _ServicioFijo(_respuesta_ok())
    _override(servicio)
    respuesta = client.post(
        "/api/query",
        json={"question": "¿Cuál es la franquicia?", "document_id": str(objetivo)},
    )

    assert respuesta.status_code == 200
    assert servicio.consultado == ("¿Cuál es la franquicia?", objetivo)
