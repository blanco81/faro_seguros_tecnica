"""Tests del dominio: transiciones de estado del documento (decisión D04)."""

import pytest

from app.domain.document import (
    ALLOWED_TRANSITIONS,
    DocumentStatus,
    InvalidTransitionError,
    ensure_transition,
)


def test_happy_path_transitions():
    ensure_transition(DocumentStatus.PENDING, DocumentStatus.PROCESSING)
    ensure_transition(DocumentStatus.PROCESSING, DocumentStatus.READY)


def test_failure_is_reachable_from_processing():
    assert DocumentStatus.FAILED in ALLOWED_TRANSITIONS[DocumentStatus.PROCESSING]


def test_retry_and_reprocess_reenter_processing():
    ensure_transition(DocumentStatus.FAILED, DocumentStatus.PROCESSING)
    ensure_transition(DocumentStatus.READY, DocumentStatus.PROCESSING)


def test_illegal_transition_raises():
    with pytest.raises(InvalidTransitionError):
        ensure_transition(DocumentStatus.READY, DocumentStatus.PENDING)


def test_illegal_skip_processing_raises():
    with pytest.raises(InvalidTransitionError):
        ensure_transition(DocumentStatus.PENDING, DocumentStatus.READY)
