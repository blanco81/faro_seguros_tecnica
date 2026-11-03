"""Tests del chunker: invariantes de la decisión D11.

El caso de uso real son pólizas en español; los textos largos simulan
páginas densas de cláusulas.
"""

from itertools import pairwise

import pytest

from app.domain.chunker import (
    PageText,
    chunk_document,
    validate_chunk_config,
)

#: 200 palabras de ~10 caracteres: fuerza varios chunks por página.
PAGINA_DENSA = " ".join(f"clausula{i}" for i in range(1, 201))


# --- configuración -----------------------------------------------------


def test_config_valida_pasa():
    validate_chunk_config(800, 120)


def test_config_rechaza_tamano_no_positivo():
    with pytest.raises(ValueError):
        validate_chunk_config(0, 0)


def test_config_rechaza_solapamiento_negativo():
    with pytest.raises(ValueError):
        validate_chunk_config(800, -1)


def test_config_rechaza_solapamiento_mayor_o_igual_que_tamano():
    with pytest.raises(ValueError):
        validate_chunk_config(100, 100)
    with pytest.raises(ValueError):
        validate_chunk_config(100, 150)


# --- invariante 1: un chunk nunca cruza páginas ------------------------


def test_cada_chunk_pertenece_a_una_sola_pagina():
    paginas = [
        PageText(1, "cobertura basica del seguro agricola"),
        PageText(2, "exclusiones por sequia y heladas"),
    ]
    chunks = chunk_document(paginas, 20, 0)

    for chunk in chunks:
        origen = paginas[chunk.page_number - 1].text
        # Cada palabra del chunk viene de SU página.
        assert set(chunk.content.split()) <= set(origen.split())


def test_pagina_invalida_rechazada():
    with pytest.raises(ValueError):
        chunk_document([PageText(0, "texto")], 800, 120)


# --- invariante 2: tamaño máximo, palabras enteras ----------------------


def test_chunks_no_exceden_el_tamano():
    chunks = chunk_document([PageText(1, PAGINA_DENSA)], 100, 20)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.content) <= 100


def test_palabra_mas_larga_que_el_chunk_se_emite_sola():
    texto = "corta " + "x" * 300 + " final"
    chunks = chunk_document([PageText(1, texto)], 100, 20)

    assert len(chunks) >= 2  # no se cuelga ni pierde texto
    for chunk in chunks:
        # Solo se permite exceder el tamaño si el chunk es una palabra única.
        if len(chunk.content) > 100:
            assert len(chunk.content.split()) == 1


def test_espacios_y_saltos_se_normalizan():
    chunks = chunk_document([PageText(1, "linea uno\nlinea   dos\ttres")], 800, 0)
    assert len(chunks) == 1
    assert chunks[0].content == "linea uno linea dos tres"


# --- invariante 3: solapamiento -----------------------------------------


def test_chunks_consecutivos_comparten_solapamiento():
    chunks = chunk_document([PageText(1, PAGINA_DENSA)], 200, 40)
    assert len(chunks) >= 2

    for anterior, siguiente in pairwise(chunks):
        # Las últimas 3 palabras del chunk anterior (~32 caracteres)
        # caben en el presupuesto de 40 y deben abrir el siguiente.
        cola = " ".join(anterior.content.split()[-3:])
        assert siguiente.content.startswith(cola)


def test_solapamiento_cero_no_repite_palabras():
    chunks = chunk_document([PageText(1, PAGINA_DENSA)], 200, 0)
    for anterior, siguiente in pairwise(chunks):
        # Sin solapamiento, el chunk siguiente empieza con una palabra
        # nueva: nunca con la última del anterior.
        assert siguiente.content.split()[0] != anterior.content.split()[-1]


# --- invariante 4: vacíos y ordinales ------------------------------------


def test_paginas_en_blanco_no_producen_chunks():
    paginas = [PageText(1, "   \n\t  "), PageText(2, "texto real")]
    chunks = chunk_document(paginas, 100, 20)
    assert [chunk.page_number for chunk in chunks] == [2]


def test_documento_sin_paginas_produce_lista_vacia():
    assert chunk_document([], 800, 120) == []


def test_ordinales_globales_secuenciales():
    paginas = [PageText(i, PAGINA_DENSA) for i in range(1, 4)]
    chunks = chunk_document(paginas, 150, 30)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    # Las páginas aportan chunks en orden.
    assert [chunk.page_number for chunk in chunks] == sorted(
        chunk.page_number for chunk in chunks
    )


def test_texto_corto_queda_en_un_solo_chunk():
    texto = "Cláusula 1: el asegurador cubre daños por granizo en cultivos."
    chunks = chunk_document([PageText(3, texto)], 800, 120)
    assert len(chunks) == 1
    assert chunks[0].page_number == 3
    assert chunks[0].content == texto
