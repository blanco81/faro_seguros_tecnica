"""Ciclo de vida del documento (núcleo de dominio).

Estados exigidos por el enunciado: pendiente, procesando, listo, fallido.
Las transiciones se validan aquí, no en la base ni en la API.
"""

from enum import Enum


class DocumentStatus(str, Enum):
    PENDING = "pending"        # pendiente
    PROCESSING = "processing"  # procesando
    READY = "ready"            # listo
    FAILED = "failed"          # fallido


#: Transiciones permitidas entre estados.
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset(
        {DocumentStatus.PROCESSING, DocumentStatus.FAILED}
    ),
    DocumentStatus.PROCESSING: frozenset(
        {DocumentStatus.READY, DocumentStatus.FAILED}
    ),
    DocumentStatus.READY: frozenset({DocumentStatus.PROCESSING}),  # reproceso
    DocumentStatus.FAILED: frozenset({DocumentStatus.PROCESSING}),  # reintento
}


class InvalidTransitionError(ValueError):
    """Se intentó una transición de estado no permitida."""


def ensure_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    """Valida la transición ``current -> target``.

    Raises:
        InvalidTransitionError: si la transición no está permitida.
    """
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"Transición ilegal: {current.value} -> {target.value}"
        )
