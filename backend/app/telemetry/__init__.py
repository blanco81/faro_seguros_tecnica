"""Telemetría del stack (Fase 5): instrumentación OpenTelemetry.

API pública: ``setup_telemetry``, ``instrument_api``, ``instrument_worker``
(los detalles y el porqué de cada pieza, en ``setup.py``).
"""

from app.telemetry.setup import (
    SERVICE_API,
    SERVICE_WORKER,
    instrument_api,
    instrument_worker,
    normalizar_endpoint_otlp,
    setup_telemetry,
)

__all__ = [
    "SERVICE_API",
    "SERVICE_WORKER",
    "instrument_api",
    "instrument_worker",
    "normalizar_endpoint_otlp",
    "setup_telemetry",
]
