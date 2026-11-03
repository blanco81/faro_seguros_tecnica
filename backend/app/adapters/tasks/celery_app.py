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
