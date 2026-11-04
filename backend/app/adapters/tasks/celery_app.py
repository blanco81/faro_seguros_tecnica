"""Aplicación Celery del worker.

Tareas registradas (Fase 2 paso 3):

- ``tasks.ping`` — sonda de vida para el healthcheck de compose.
- ``tasks.ingest_document`` — pipeline de ingesta (importada vía
  ``include`` cuando arranca el worker; así este módulo no depende de
  los adapters de la tarea y no hay import circular).

Configuración de entrega (D03/D16): ``task_acks_late`` + prefetch 1
  — la tarea solo se saca de la cola al TERMINAR, de modo que un
  reinicio del worker a mitad reentrega el mensaje (los reintentos
  son seguros: run_ingestion es re-ejecutable).
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "faro_seguros",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.adapters.tasks.ingestion_task"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Idempotencia y reintentos: la tarea solo se saca de la cola al terminar
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)


@celery_app.task(name="tasks.ping")
def ping() -> str:
    """Sonda de vida para el healthcheck del worker."""
    return "pong"


# --- Fase 5 (D23/D24): trazas del worker ----------------------------------
# worker_process_init se emite en CADA HIJO del pool prefork al arrancar
# (no en el padre, ni al importar el módulo desde la API o la suite). Es la
# señal correcta para instrumentar porque:
#   - el padre NO ejecuta tareas con prefork: instrumentarlo no produce spans.
#   - el BatchSpanProcessor arranca un hilo exportador; fork() NO copia
#     hilos → un hijo que hereda el provider del padre acumula spans en una
#     cola cuyo hilo no existe y NUNCA los exporta (silencioso). Cada hijo
#     debe montar su propio provider+thread.
# El contexto de la traza llega en las cabeceras del mensaje (traceparent
# inyectado por el productor instrumentado en la API) y la instrumentación
# de Celery lo extrae: run/tasks.ingest_document continúa la traza del
# POST /api/documents que la despachó.
from celery.signals import worker_process_init


@worker_process_init.connect(weak=False)
def _instrumentar_worker(**_kwargs: object) -> None:
    from app.telemetry import instrument_worker

    instrument_worker()
