"""Tests de la tarea Celery de ingesta (Fase 2 paso 3): política D16.

Sin broker ni base (D01): la política de reintentos es una FUNCIÓN PURA
(``decide_retry``) y el despacho de la tarea (``_dispatch``) recibe sus
piezas cableables por parámetro, así que se prueba con dobles — un Task
falso que registra ``retry()`` y lanza ``Retry`` como el real, y los
mismos fakes de la suite. El cableado completo (Redis + worker +
PostgreSQL) lo prueba el smoke de la Fase 2 paso 5, como el adapter
SQLAlchemy (ver su docstring).

Lo que estos tests fijan:

- D16: fatal no se reintenta; transitoria reintenta con backoff
  exponencial (10/20/40 s); agotado el presupuesto, la TAREA convierte
  la transitoria en ``failed`` con razón (run_ingestion no lo hace).
- El desenlace de la tarea es un dict uniforme (ready/failed con razón
  e intento) — es lo que leerán el smoke y el polling de la Fase 4.
- La tarea queda registrada en la app Celery con el nombre y el techo
  de reintentos de la política (paridad decorador ↔ política).
- D15 (registro): register_document guarda el contenido junto al alta
  y re-subir no lo reescribe (relación 1:1).
"""

import uuid
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from app.adapters.extraction.extractors import build_default_extractor
from app.adapters.llm.fake_embeddings import FakeEmbeddingProvider
from app.adapters.tasks.celery_app import celery_app
from app.adapters.tasks.ingestion_task import (
    MAX_RETRIES,
    RETRY_BACKOFF_BASE_SECONDS,
    RetryDecision,
    RetryPolicy,
    _dispatch,
    decide_retry,
    ingest_document,
    mark_permanent_failure,
)
from app.domain.document import DocumentStatus
from app.services.ingestion import (
    FatalIngestionError,
    register_document,
    run_ingestion,
)
from tests.fakes import FakeDocumentRepository

TEXTO_POLIZA = (
    "La póliza cubre daños por inundación en el hogar asegurado según "
    "los límites y condiciones descritos en la cláusula 4.1 "
) * 6

CHUNK = {"chunk_size": 120, "chunk_overlap": 30}


class FakeCeleryTask:
    """Doble del Task de Celery (``bind=True``).

    Como el real: ``retry()`` no retorna — registra la orden y lanza
    ``Retry`` para que el worker la re-programe.
    """

    def __init__(self, retries: int = 0) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retry_calls: list[SimpleNamespace] = []

    def retry(self, *, exc=None, countdown=None, **kwargs):
        self.retry_calls.append(
            SimpleNamespace(exc=exc, countdown=countdown, kwargs=kwargs)
        )
        raise Retry("reintentando", exc=exc, when=countdown)


class TestRetryPolicy:
    def test_backoff_exponencial_10_20_40(self):
        policy = RetryPolicy()

        esperados = [
            RETRY_BACKOFF_BASE_SECONDS * (2**n) for n in range(MAX_RETRIES)
        ]

        assert [policy.countdown(n) for n in range(MAX_RETRIES)] == esperados
        assert esperados == [10.0, 20.0, 40.0]


class TestDecideRetry:
    def test_fatal_no_se_reintenta(self):
        plan = decide_retry(
            FatalIngestionError("PDF cifrado"),
            retries_so_far=0,
            policy=RetryPolicy(),
        )

        assert plan.decision is RetryDecision.GIVE_UP
        assert plan.countdown_seconds == 0.0

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("red caída"),
            TimeoutError("proveedor lento"),
            RuntimeError("inesperado"),
        ],
    )
    def test_transitoria_reintenta_con_backoff(self, exc):
        plan = decide_retry(exc, retries_so_far=1, policy=RetryPolicy())

        assert plan.decision is RetryDecision.RETRY
        assert plan.countdown_seconds == 20.0  # 10 * 2**1

    def test_presupuesto_agotado_falla_permanentemente(self):
        plan = decide_retry(
            ConnectionError("red caída"),
            retries_so_far=MAX_RETRIES,
            policy=RetryPolicy(),
        )

        assert plan.decision is RetryDecision.FAIL_PERMANENTLY

    def test_fatal_gana_aunque_el_presupuesto_este_agotado(self):
        # Un PDF cifrado no se cura con reintentos: ni con presupuesto
        # restante ni sin él. La clasificación por tipo va primero.
        plan = decide_retry(
            FatalIngestionError("sin texto extraíble"),
            retries_so_far=MAX_RETRIES + 5,
            policy=RetryPolicy(),
        )

        assert plan.decision is RetryDecision.GIVE_UP


class TestMarkPermanentFailure:
    def test_marca_failed_con_la_razon(self):
        repo = FakeDocumentRepository()
        documento, _ = register_document(
            repo, filename="p.txt", mime_type="text/plain", data=b"hola"
        )
        repo.set_status(documento.id, DocumentStatus.PROCESSING)

        marcado = mark_permanent_failure(
            repo, documento.id, "Reintentos agotados: red caída"
        )

        assert marcado is not None
        assert marcado.status is DocumentStatus.FAILED
        assert "Reintentos agotados" in marcado.failure_reason

    def test_documento_inexistente_no_explota(self):
        # Borrado concurrente: no hay fila que marcar y NO es un error
        # de la tarea (no debe disparar otra ronda de reintentos).
        resultado = mark_permanent_failure(
            FakeDocumentRepository(), uuid.uuid4(), "razón"
        )

        assert resultado is None


class TestDispatch:
    @pytest.fixture
    def repo(self) -> FakeDocumentRepository:
        return FakeDocumentRepository()

    @pytest.fixture
    def pipeline_ok(self, repo):
        """``process`` real con fakes: pipeline completo → DocumentInfo ready."""
        extractor = build_default_extractor()
        embedder = FakeEmbeddingProvider()
        data = TEXTO_POLIZA.encode("utf-8")
        documento, _ = register_document(
            repo, filename="poliza.txt", mime_type="text/plain", data=data
        )

        def process(document_id):
            return run_ingestion(
                repo, extractor, embedder, document_id, data, **CHUNK
            )

        return documento, process

    def test_exito_devuelve_resumen_ready(self, repo, pipeline_ok):
        documento, process = pipeline_ok

        resultado = _dispatch(FakeCeleryTask(), documento.id, process=process)

        assert resultado == {
            "document_id": str(documento.id),
            "status": "ready",
            "page_count": resultado["page_count"],
            "chunk_count": resultado["chunk_count"],
            "failure_reason": None,
            "attempt": 1,
        }
        assert resultado["chunk_count"] > 0

    def test_fatal_no_reintenta_y_devuelve_failed(self, pipeline_ok):
        documento, _ = pipeline_ok

        def process(_):
            raise FatalIngestionError("El documento no contiene texto extraíble")

        tarea = FakeCeleryTask()
        resultado = _dispatch(tarea, documento.id, process=process)

        assert tarea.retry_calls == []
        assert resultado["status"] == "failed"
        assert resultado["failure_reason"] == (
            "El documento no contiene texto extraíble"
        )
        assert resultado["attempt"] == 1

    def test_transitoria_reintenta_con_countdown(self, pipeline_ok):
        documento, _ = pipeline_ok

        def process(_):
            raise ConnectionError("proveedor 502")

        tarea = FakeCeleryTask()  # primer intento: request.retries == 0
        with pytest.raises(Retry):
            _dispatch(tarea, documento.id, process=process)

        (llamada,) = tarea.retry_calls
        assert llamada.countdown == 10.0  # 10 * 2**0
        assert isinstance(llamada.exc, ConnectionError)

    def test_transitoria_en_reintento_espera_mas(self, pipeline_ok):
        documento, _ = pipeline_ok

        def process(_):
            raise TimeoutError("proveedor lento")

        tarea = FakeCeleryTask(retries=2)  # tercer intento
        with pytest.raises(Retry):
            _dispatch(tarea, documento.id, process=process)

        assert tarea.retry_calls[0].countdown == 40.0  # 10 * 2**2

    def test_agotados_marca_failed_y_no_reintenta(self, pipeline_ok):
        documento, _ = pipeline_ok
        marcas: list[tuple[uuid.UUID, str]] = []

        def process(_):
            raise ConnectionError("red caída (persistente)")

        def mark_failed(document_id, reason):
            marcas.append((document_id, reason))

        tarea = FakeCeleryTask(retries=MAX_RETRIES)
        resultado = _dispatch(
            tarea,
            documento.id,
            process=process,
            mark_failed=mark_failed,
        )

        assert tarea.retry_calls == []  # ya no queda presupuesto
        assert resultado["status"] == "failed"
        assert resultado["attempt"] == MAX_RETRIES + 1
        (documento_id, razon), = marcas
        assert documento_id == documento.id
        assert "Reintentos agotados" in razon
        assert "red caída (persistente)" in razon

    def test_el_intento_se_cuenta_desde_request_retries(self, pipeline_ok):
        documento, _ = pipeline_ok

        def process(_):
            raise FatalIngestionError("PDF cifrado")

        resultado = _dispatch(
            FakeCeleryTask(retries=2), documento.id, process=process
        )

        assert resultado["attempt"] == 3


class TestRegistroDeLaTarea:
    def test_registrada_con_nombre_y_techo_de_la_politica(self):
        # ingest_document se importa arriba → queda en el registry de la
        # app. El techo del decorador debe seguir siendo el de la política
        # (decide_retry corta ANTES; el decorador es la red de seguridad).
        tarea = celery_app.tasks["tasks.ingest_document"]

        assert tarea.name == ingest_document.name
        assert tarea.max_retries == RetryPolicy().max_retries
