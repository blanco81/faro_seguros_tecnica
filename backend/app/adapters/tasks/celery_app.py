"""Aplicación Celery del worker.

Las tareas reales de ingesta llegan en la Fase 2; por ahora existe para que el
servicio del worker arranque y el healthcheck de compose sea válido.
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "faro_seguros",
    broker=settings.redis_url,
    backend=settings.redis_url,
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
