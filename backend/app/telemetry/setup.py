"""Instrumentación OpenTelemetry del stack (Fase 5, pasos 1-3).

Un solo módulo monta TODA la telemetría de un proceso, porque el objetivo
de la Fase 5 es seguir una petición de extremo a extremo:

    POST /api/documents          POST /api/query
      │ (span HTTP, faro-api)     │ (span HTTP, faro-api)
      │ .delay() ── inyecta ──┐   │ bucle agéntico → LLM (span httpx)
      ▼                       │   │
    Redis (cabeceras)         │   │
      │                       ▼   │
      │  run/tasks.ingest_document │
      │  (span Celery, faro-worker)│
      │  embeddings → httpx ───────┘

Las tres piezas (D23):

1. ``setup_telemetry`` — el TracerProvider del proceso: OTLP/HTTP hacia
   el collector configurado, en lotes (``BatchSpanProcessor``).
   Idempotente y desactivable: con ``OTEL_ENABLED=false`` no toca nada
   (así hermetiza la suite la variable del conftest).

2. ``instrument_api`` / ``instrument_worker`` — qué se instrumenta en
   cada proceso (la API también es productor de tareas Celery):

   ==============  ==========================  ==========================
   Proceso         Instrumentación             Cubre
   ==============  ==========================  ==========================
   faro-api        FastAPI + Celery + httpx    HTTP servidor, publicación
                                                 de tareas, llamadas LLM
   faro-worker     Celery + httpx              ejecución de la tarea,
                                                 llamadas embeddings
   ==============  ==========================  ==========================

3. Propagación HTTP → Celery: la instrumentación de Celery inyecta el
   contexto en las cabeceras del mensaje al publicar (proceso API) y lo
   extrae al ejecutar (worker) — ``run/tasks.ingest_document`` queda como
   hijo del ``apply_async/tasks.ingest_document`` que lo despachó (los
   nombres los fija la librería: prefijo ``run``/``apply_async`` + nombre
   de tarea).

Punto fino verificado contra un receptor real: el exporter OTLP/HTTP del
SDK NO añade ``/v1/traces`` a un endpoint sin ruta (exportaría a ``/`` y
el collector lo rechazaría); ``normalizar_endpoint_otlp`` lo corrige.

Lo que NO hace (todavía): spans de dominio RAG (scores, iteraciones del
agente) ni métricas precision@k/recall@k — siguientes pasos de la Fase 5.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from app.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover — solo para tipado
    from fastapi import FastAPI

__all__ = [
    "SERVICE_API",
    "SERVICE_WORKER",
    "instrument_api",
    "instrument_worker",
    "normalizar_endpoint_otlp",
    "setup_telemetry",
]

logger = logging.getLogger(__name__)

#: Nombres de servicio en Jaeger/collector: mismos servicios que en el
#: compose, prefijados — distinguen API de worker en el mismo despliegue.
SERVICE_API = "faro-api"
SERVICE_WORKER = "faro-worker"

#: Ruta del protocolo OTLP/HTTP para trazas.
_V1_TRACES = "/v1/traces"

#: Estado del proceso: ``set_tracer_provider`` es una sola vez por
#: proceso y el setup no debe reconstruir providers en cada request.
_provider: TracerProvider | None = None
_inicializado = False

#: Instrumentores ya aplicados en el proceso (idempotencia): main.py crea
#: la app a nivel de módulo Y create_app() puede llamarse otra vez (tests,
#: scripts) — los instrumentores de librería avisan si se aplican dos veces
#: y no hay que reaplicarlos: el parche es de clase, ya cubre a todos.
_instrumentado: set[str] = set()


# --- paso 1: el TracerProvider del proceso --------------------------------


def normalizar_endpoint_otlp(endpoint: str) -> str:
    """Añade ``/v1/traces`` si falta (el SDK NO lo hace por sí solo).

    >>> normalizar_endpoint_otlp("http://otel-collector:4318")
    'http://otel-collector:4318/v1/traces'
    >>> normalizar_endpoint_otlp("http://localhost:4318/v1/traces")
    'http://localhost:4318/v1/traces'
    """
    limpio = endpoint.rstrip("/")
    if limpio.endswith(_V1_TRACES):
        return limpio
    return f"{limpio}{_V1_TRACES}"


def _build_provider(
    service_name: str, exporter: SpanExporter
) -> TracerProvider:
    """Construye el provider SIN tocar el global (testeable aislado)."""
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def setup_telemetry(
    service_name: str,
    *,
    settings: Settings | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Monta (una vez por proceso) el TracerProvider global con OTLP.

    Args:
        service_name: ``faro-api`` o ``faro-worker`` (Resource de OTel:
            así agrupa Jaeger los servicios).
        settings: configuración inyectable (tests); por defecto, la del
            proceso.
        exporter: exporter inyectable (tests con InMemorySpanExporter);
            por defecto, OTLP/HTTP al endpoint configurado.

    Returns:
        El provider montado, o ``None`` si ``OTEL_ENABLED=false`` (la
        llamada siguiente es un no-op y devuelve lo mismo).
    """
    global _provider, _inicializado
    if _inicializado:
        return _provider
    _inicializado = True

    settings = settings or get_settings()
    if not settings.otel_enabled:
        logger.debug(
            "Telemetría desactivada (OTEL_ENABLED=false): sin trazas de %s.",
            service_name,
        )
        return None

    if exporter is None:
        exporter = OTLPSpanExporter(
            endpoint=normalizar_endpoint_otlp(settings.otel_exporter_otlp_endpoint),
        )
    provider = _build_provider(service_name, exporter)
    trace.set_tracer_provider(provider)  # idempotente en el SDK: avisa y
    # descarta segundos intentos; el guard de arriba evita llegar aquí.
    _provider = provider
    logger.info(
        "Trazas activas: %s → %s",
        service_name,
        settings.otel_exporter_otlp_endpoint,
    )
    return provider


# --- pasos 2 y 3: qué instrumenta cada proceso -----------------------------


def _instrumentar_fastapi(app: FastAPI, provider: TracerProvider) -> None:
    """Spans HTTP del servidor (``GET /api/documents``...)."""
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


def _instrumentar_celery(provider: TracerProvider) -> None:
    """Productor (inyecta contexto al publicar) y consumidor (extrae)."""
    if "celery" in _instrumentado:
        return
    CeleryInstrumentor().instrument(tracer_provider=provider)
    _instrumentado.add("celery")


def _instrumentar_httpx(provider: TracerProvider) -> None:
    """Llamadas al proveedor: el SDK de OpenAI corre sobre httpx y queda
    cubierto por el parche de clase (cubre clientes ya creados y futuros)."""
    if "httpx" in _instrumentado:
        return
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    _instrumentado.add("httpx")


def instrument_api(
    app: FastAPI,
    *,
    tracer_provider: TracerProvider | None = None,
    settings: Settings | None = None,
) -> None:
    """Instrumenta el proceso API: HTTP + productor Celery + LLM (httpx).

    Con ``tracer_provider`` explícito no toca el global (tests); sin él,
    monta el provider del proceso vía ``setup_telemetry`` — que con
    ``OTEL_ENABLED=false`` devuelve ``None`` y aquí no se instrumenta nada.
    """
    provider = tracer_provider or setup_telemetry(SERVICE_API, settings=settings)
    if provider is None:
        return
    _instrumentar_fastapi(app, provider)
    _instrumentar_celery(provider)
    _instrumentar_httpx(provider)


def instrument_worker(
    *,
    tracer_provider: TracerProvider | None = None,
    settings: Settings | None = None,
) -> None:
    """Instrumenta el worker: ejecución de tareas + embeddings (httpx).

    Se engancha a la señal ``worker_process_init`` de Celery (ver
    ``celery_app.py``): se emite en cada hijo del pool prefork al arrancar
    — ahí se ejecutan las tareas y cada hijo monta un provider fresco
    (el del padre no sobrevive al fork: su hilo exportador no se copia).
    """
    provider = tracer_provider or setup_telemetry(SERVICE_WORKER, settings=settings)
    if provider is None:
        return
    _instrumentar_celery(provider)
    _instrumentar_httpx(provider)
