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

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.config import Settings
from app.telemetry import (
    SERVICE_API,
    instrument_api,
    normalizar_endpoint_otlp,
    setup_telemetry,
)
from app.telemetry.setup import _build_provider


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
