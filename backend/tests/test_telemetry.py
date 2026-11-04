"""Tests de la instrumentación OTel (Fase 5, pasos 1-3, D23).

Regla de la suite (D01): sin red ni collector. Por eso:

- La ruta DESACTIVADA (OTEL_ENABLED=false) se prueba contra Settings
  reales — es la que corre en CI.
- La ruta ACTIVA se prueba con providers EXPLÍCITOS + InMemory exporter:
  nunca se toca el TracerProvider global del proceso (set_tracer_provider
  es una sola vez y contaminaría al resto de la suite).
- La exportación OTLP real (endpoint, /v1/traces, collector) la verifica
  el smoke contra el stack levantado (sección 17).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.adapters.http.query import get_query_service, query_router
from app.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.services.query import (
    SPAN_CONSULTA,
    SPAN_ITERACION,
    AgenticQueryService,
)
from app.telemetry import (
    SERVICE_API,
    instrument_api,
    normalizar_endpoint_otlp,
    setup_telemetry,
)
from app.telemetry.setup import _build_provider
from tests.fakes import FakeRetriever, ScriptedLLM, StubEmbedder


def _settings_desactivados() -> Settings:
    return Settings(otel_enabled=False, _env_file=None)


def _provider_memoria() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Provider aislado con export en memoria (sin globales, sin red)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# --- normalización del endpoint OTLP ---------------------------------------


def test_normalizar_anade_ruta_a_endpoint_base() -> None:
    """Sin ruta, el exporter OTLP/HTTP exportaría a '/' — hay que añadirla."""
    assert (
        normalizar_endpoint_otlp("http://otel-collector:4318")
        == "http://otel-collector:4318/v1/traces"
    )


def test_normalizar_respeta_endpoint_con_ruta() -> None:
    assert (
        normalizar_endpoint_otlp("http://localhost:4318/v1/traces")
        == "http://localhost:4318/v1/traces"
    )


def test_normalizar_limpia_barras_finales() -> None:
    assert (
        normalizar_endpoint_otlp("http://collector:4318/")
        == "http://collector:4318/v1/traces"
    )


# --- ruta desactivada: el no-op que corre en la suite y el CI --------------


def test_setup_desactivado_no_monta_nada() -> None:
    """OTEL_ENABLED=false → None: sin provider, sin exporter, sin red."""
    assert setup_telemetry(SERVICE_API, settings=_settings_desactivados()) is None


def test_setup_desactivado_es_idempotente() -> None:
    """La segunda llamada devuelve lo mismo aunque cambien los settings:
    el guard de proceso evita remontar providers por request."""
    setup_telemetry(SERVICE_API, settings=_settings_desactivados())
    assert setup_telemetry(SERVICE_API, settings=_settings_desactivados()) is None


def test_instrument_api_desactivada_no_toca_la_app() -> None:
    """create_app() con OTEL_ENABLED=false: ni middleware ni spans — la
    app sirve igual que sin telemetría."""
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    middleware_antes = list(app.user_middleware)
    instrument_api(app, settings=_settings_desactivados())
    assert app.user_middleware == middleware_antes

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# --- ruta activa: instrumentación real, provider explícito -----------------


def test_span_http_con_la_ruta_y_servicio() -> None:
    """Un GET instrumentado produce un span HTTP con la ruta plantilla."""
    provider, exporter = _provider_memoria()
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    instrument_api(app, tracer_provider=provider)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    spans = exporter.get_finished_spans()
    assert spans, "la request no generó spans"
    span = spans[-1]
    assert span.name == "GET /health"
    assert span.attributes is not None
    assert span.attributes.get("http.route") == "/health"


def test_resource_del_setup_lleva_service_name() -> None:
    """El provider de setup marca el service.name: es lo que agrupa
    faro-api y faro-worker como servicios distintos en Jaeger."""
    exporter = InMemorySpanExporter()
    provider = _build_provider(SERVICE_API, exporter)
    with provider.get_tracer("test").start_as_current_span("s") as span:
        pass
    assert span.resource.attributes["service.name"] == "faro-api"


def test_worker_se_instrumenta_en_cada_hijo_prefork(monkeypatch) -> None:
    """D24: la instrumentación del worker va en worker_process_init (cada
    hijo del pool prefork), NO en worker_init (padre). El padre no ejecuta
    tareas con prefork y su BatchSpanProcessor no sobrevive al fork -> los
    spans del hijo se perderían. Pin: worker_process_init llama a
    instrument_worker; worker_init no.
    """
    from celery.signals import worker_init, worker_process_init

    import app.adapters.tasks.celery_app  # noqa: F401 — registra el handler
    import app.telemetry as modulo_telem

    llamadas: list[str] = []

    def _falsa() -> None:
        llamadas.append("worker")

    monkeypatch.setattr(modulo_telem, "instrument_worker", _falsa)

    worker_process_init.send(sender=None)
    assert llamadas == ["worker"], (
        "worker_process_init debe disparar instrument_worker (un hijo por instrumentar)"
    )

    llamadas.clear()
    worker_init.send(sender=None)
    assert not llamadas, (
        "worker_init NO debe instrumentar: el padre no ejecuta tareas con prefork"
    )


def test_collector_reenvia_a_jaeger() -> None:
    """D25: el collector exporta trazas a Jaeger (siempre-on), además del
    debug. Pin sobre el YAML del collector — sin red ni contenedor."""
    from pathlib import Path

    cfg = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "otel-collector-config.yaml"
    ).read_text()
    assert "otlp/jaeger:" in cfg, "falta el exporter otlp/jaeger en el collector"
    pipeline = cfg[cfg.index("pipelines:") :]
    assert "otlp/jaeger" in pipeline, "el pipeline de trazas no exporta a Jaeger"


def test_jaeger_arranca_con_el_resto() -> None:
    """D25: Jaeger deja de ser opt-in — sin 'profiles:' en el compose, arranca
    con 'make up' como db/redis/api/worker/frontend/otel-collector."""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "profiles:" not in compose, "Jaeger sigue siendo opt-in (quitar profiles)"


# --- Fase 5 (D27): spans de dominio RAG ---------------------------------------
#
# El servicio emite rag.consulta (raíz: desenlace, umbral, iteraciones) y
# rag.iteracion (hijo por vuelta: scores vs umbral, decisión). Se prueban
# con el MISMO patrón del resto del archivo: provider aislado en memoria
# que jamás toca el global del proceso.

PREGUNTA_RAG = "¿Cuál es la franquicia aplicable a los daños por agua?"

CHUNK_RAG = RetrievedChunk(
    document="poliza-hogar.pdf",
    page=2,
    content=(
        "Cláusula 7.4 — Franquicia. Para la cobertura de daños por agua "
        "se aplica una franquicia de 300 euros por siniestro."
    ),
    score=0.87,
)
CITA_RAG = "se aplica una franquicia de 300 euros por siniestro"


def _json_rag(**datos):
    return json.dumps(datos)


def _servicio_rag(llm, chunks, *, umbral=0.45, tracer=None):
    """El servicio real con dobles de test y tracer inyectado."""
    return AgenticQueryService(
        llm=llm,
        embedder=StubEmbedder(),
        retriever=FakeRetriever(chunks),
        rejection_threshold=umbral,
        tracer=tracer,
    )


def _spans(exporter, nombre):
    return [s for s in exporter.get_finished_spans() if s.name == nombre]


def test_respuesta_deja_consulta_e_iteracion_con_score_sobre_umbral() -> None:
    """D27: la traza cuenta la historia — raíz rag.consulta con desenlace
    respondida y un hijo rag.iteracion con el score que cruzó el umbral."""
    provider, exporter = _provider_memoria()
    llm = ScriptedLLM(
        _json_rag(subquestions=[PREGUNTA_RAG]),
        _json_rag(answer="La franquicia es de 300 euros.", quote=CITA_RAG),
    )
    servicio = _servicio_rag(llm, [CHUNK_RAG], tracer=provider.get_tracer("t"))

    resultado = servicio.answer(PREGUNTA_RAG)

    assert resultado.refused is False
    (raiz,) = _spans(exporter, SPAN_CONSULTA)
    assert raiz.attributes["rag.pregunta"] == PREGUNTA_RAG
    assert raiz.attributes["rag.umbral"] == 0.45
    assert raiz.attributes["rag.desenlace"] == "respondida"
    assert raiz.attributes["rag.iteraciones"] == 1
    assert raiz.attributes["rag.citas"] == 1
    assert "rag.motivo" not in raiz.attributes  # responder no es fallar

    (iteracion,) = _spans(exporter, SPAN_ITERACION)
    assert iteracion.attributes["rag.decision"] == "ok"
    assert iteracion.attributes["rag.cita_verificada"] is True
    assert iteracion.attributes["rag.mejor_score"] == pytest.approx(0.87)
    assert iteracion.attributes["rag.superumbral"] is True
    assert "0.8700" in iteracion.attributes["rag.scores"]
    assert iteracion.attributes["rag.queries"] == 1
    assert iteracion.attributes["rag.resultados"] == 1
    # waterfall de Jaeger: la iteración cuelga de la consulta
    assert iteracion.parent.span_id == raiz.context.span_id


def test_rechazo_registra_scores_bajo_umbral_por_iteracion() -> None:
    """El pin del paso: scores vs umbral por iteración. Tres vueltas con
    mejor_score 0.30 < umbral 0.45 y la raíz con el rechazo completo."""
    chunk_flojo = RetrievedChunk(
        document=CHUNK_RAG.document,
        page=CHUNK_RAG.page,
        content=CHUNK_RAG.content,
        score=0.30,  # < umbral 0.45 (D13): evidencia insuficiente
    )
    provider, exporter = _provider_memoria()
    llm = ScriptedLLM(
        _json_rag(subquestions=[PREGUNTA_RAG]),
        _json_rag(query="reformulación 1"),
        _json_rag(query="reformulación 2"),
    )
    servicio = _servicio_rag(llm, [chunk_flojo], tracer=provider.get_tracer("t"))

    resultado = servicio.answer(PREGUNTA_RAG)

    assert resultado.refused is True
    assert resultado.reason == "below_threshold"
    (raiz,) = _spans(exporter, SPAN_CONSULTA)
    assert raiz.attributes["rag.desenlace"] == "rechazada"
    assert raiz.attributes["rag.motivo"] == "below_threshold"
    assert raiz.attributes["rag.iteraciones"] == 3
    assert raiz.attributes["rag.citas"] == 0

    iteraciones = sorted(
        _spans(exporter, SPAN_ITERACION),
        key=lambda s: s.attributes["rag.iteracion"],
    )
    assert len(iteraciones) == 3
    for numero, span in enumerate(iteraciones, start=1):
        assert span.attributes["rag.iteracion"] == numero
        assert span.attributes["rag.decision"] == "below_threshold"
        assert span.attributes["rag.mejor_score"] == pytest.approx(0.30)
        assert span.attributes["rag.superumbral"] is False
        # el pin literal: el score registrado queda BAJO el umbral registrado
        assert span.attributes["rag.mejor_score"] < span.attributes["rag.umbral"]
        assert "0.3000" in span.attributes["rag.scores"]
        # nunca llegó a generar: sin atributo de cita
        assert "rag.cita_verificada" not in span.attributes


def test_el_refinamiento_deja_huella_por_iteracion() -> None:
    """citation_mismatch → refinar → ok: cada vuelta anota SU decisión."""
    provider, exporter = _provider_memoria()
    llm = ScriptedLLM(
        _json_rag(subquestions=[PREGUNTA_RAG]),
        _json_rag(answer="Unos trescientos.", quote="cita inventada"),
        _json_rag(query="reformulada"),
        _json_rag(answer="La franquicia es de 300 euros.", quote=CITA_RAG),
    )
    servicio = _servicio_rag(llm, [CHUNK_RAG], tracer=provider.get_tracer("t"))

    resultado = servicio.answer(PREGUNTA_RAG)

    assert resultado.refused is False
    assert resultado.iterations == 2
    (raiz,) = _spans(exporter, SPAN_CONSULTA)
    assert raiz.attributes["rag.desenlace"] == "respondida"
    assert raiz.attributes["rag.iteraciones"] == 2

    iteraciones = sorted(
        _spans(exporter, SPAN_ITERACION),
        key=lambda s: s.attributes["rag.iteracion"],
    )
    decisiones = [s.attributes["rag.decision"] for s in iteraciones]
    assert decisiones == ["citation_mismatch", "ok"]
    assert iteraciones[0].attributes["rag.cita_verificada"] is False
    assert iteraciones[1].attributes["rag.cita_verificada"] is True


def test_aclaracion_cierra_la_consulta_sin_iteraciones() -> None:
    """El planificador pide aclaración antes de recuperar: la traza es la
    raíz con desenlace aclaracion y CERO spans de iteración."""
    provider, exporter = _provider_memoria()
    llm = ScriptedLLM(
        _json_rag(clarification="¿La póliza de hogar o la de auto?")
    )
    servicio = _servicio_rag(llm, [CHUNK_RAG], tracer=provider.get_tracer("t"))

    resultado = servicio.answer("¿Qué cubre la póliza?")

    assert resultado.clarification == "¿La póliza de hogar o la de auto?"
    (raiz,) = _spans(exporter, SPAN_CONSULTA)
    assert raiz.attributes["rag.desenlace"] == "aclaracion"
    assert raiz.attributes["rag.iteraciones"] == 0
    assert raiz.attributes["rag.citas"] == 0
    assert _spans(exporter, SPAN_ITERACION) == []


def test_el_span_de_dominio_cuelga_del_span_http() -> None:
    """Wiring de producción: POST /api/query genera el span HTTP del
    servidor y, debajo, rag.consulta → rag.iteracion — la cascada que se
    ve en Jaeger para el servicio faro-api."""
    provider, exporter = _provider_memoria()
    app = FastAPI()
    app.include_router(query_router)
    instrument_api(app, tracer_provider=provider)
    llm = ScriptedLLM(
        _json_rag(subquestions=[PREGUNTA_RAG]),
        _json_rag(answer="La franquicia es de 300 euros.", quote=CITA_RAG),
    )
    app.dependency_overrides[get_query_service] = lambda: _servicio_rag(
        llm, [CHUNK_RAG], tracer=provider.get_tracer("t")
    )

    with TestClient(app) as client:
        respuesta = client.post("/api/query", json={"question": PREGUNTA_RAG})

    assert respuesta.status_code == 200
    assert respuesta.json()["refused"] is False
    spans = exporter.get_finished_spans()
    http = next(s for s in spans if s.name == "POST /api/query")
    (raiz,) = _spans(exporter, SPAN_CONSULTA)
    assert raiz.parent.span_id == http.context.span_id
    assert any(s.name == SPAN_ITERACION for s in spans)
