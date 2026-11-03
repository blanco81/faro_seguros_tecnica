"""Tests de citas verificables: contrato de literalidad D12."""

import pytest

from app.domain.citations import (
    DEFAULT_MAX_QUOTE_CHARS,
    build_citation,
    quote_is_verbatim,
)

CHUNK_CORTO = "La póliza cubre daños por granizo en cultivos de trigo."
CHUNK_LARGO = " ".join(f"termino{i}" for i in range(1, 80))  # > 220 caracteres


# --- contrato: el fragmento es literal ----------------------------------


def test_cita_contiene_fragmento_literal():
    cita = build_citation("poliza-trigo.pdf", 4, CHUNK_CORTO)
    assert cita.document == "poliza-trigo.pdf"
    assert cita.page == 4
    assert cita.quote in CHUNK_CORTO
    assert quote_is_verbatim(cita, CHUNK_CORTO)


def test_chunk_corto_se_cita_entero():
    cita = build_citation("poliza.pdf", 1, CHUNK_CORTO)
    assert cita.quote == CHUNK_CORTO


def test_espacios_extremos_del_chunk_se_limpian():
    cita = build_citation("poliza.pdf", 1, f"  {CHUNK_CORTO}\n")
    assert cita.quote == CHUNK_CORTO


def test_chunk_largo_se_recorta_y_sigue_siendo_literal():
    cita = build_citation("poliza.pdf", 2, CHUNK_LARGO)

    assert len(cita.quote) <= DEFAULT_MAX_QUOTE_CHARS
    # Contrato D12: aun recortado, es un substring exacto.
    assert cita.quote in CHUNK_LARGO
    assert quote_is_verbatim(cita, CHUNK_LARGO)
    # El recorte cae en palabra completa: lo que sigue es espacio o nada.
    resto = CHUNK_LARGO[len(cita.quote) : len(cita.quote) + 1]
    assert resto in ("", " ")


def test_limite_de_recorte_configurable():
    cita = build_citation("poliza.pdf", 1, CHUNK_LARGO, max_quote_chars=30)
    assert len(cita.quote) <= 30
    assert cita.quote in CHUNK_LARGO


# --- validaciones ---------------------------------------------------------


def test_documento_vacio_rechazado():
    with pytest.raises(ValueError):
        build_citation("", 1, CHUNK_CORTO)


def test_pagina_invalida_rechazada():
    with pytest.raises(ValueError):
        build_citation("poliza.pdf", 0, CHUNK_CORTO)


def test_limite_no_positivo_rechazado():
    with pytest.raises(ValueError):
        build_citation("poliza.pdf", 1, CHUNK_CORTO, max_quote_chars=0)


# --- integración con el chunker: de PDF a cita verificable ---------------


def test_cita_de_un_chunk_real_del_chunker_es_verificable():
    from app.domain.chunker import PageText, chunk_document

    pagina = PageText(1, " ".join(f"clausula{i}" for i in range(1, 100)))
    chunk = chunk_document([pagina], 120, 30)[1]  # un chunk del medio

    cita = build_citation("poliza-multirriesgo.pdf", chunk.page_number, chunk.content)
    assert quote_is_verbatim(cita, chunk.content)
    assert cita.page == 1
