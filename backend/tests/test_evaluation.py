"""Tests de la evaluación de recuperación: métricas, set y runner (D28/D29).

Tres niveles:

1. **Métricas puras**: aritmética exacta sobre ``RetrievedChunk``
   sintéticos (P@k con denominador k, R@k por páginas, hit@k verbatim).
2. **El set y su ancla con los PDF reales**: la suite extrae y trocea las
   TRES pólizas commiteadas con el MISMO pipeline de producción
   (extracción local + chunker D11, sin red) y verifica el pin triple
   PDF ↔ sha256 ↔ set: si alguien sustituye un PDF o rompe una etiqueta,
   esto lo grita en CI sin necesidad del stack.
3. **El runner con dobles** (D01): los dos regímenes de búsqueda (acotada
   a LA póliza del caso / sobre toda la base), el reporte, el barrido de
   umbral y la puerta de salida (etiquetas verificadas + hit@5 completo +
   0 fuera de dominio aceptados).
"""

import ast
import hashlib
import uuid
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import pytest

from app.adapters.extraction.extractors import build_default_extractor
from app.domain.chunker import chunk_document
from app.domain.retrieval import RetrievedChunk
from app.evaluation.dataset import (
    DOCUMENTOS_EVAL,
    EVAL_SET,
    EvalCase,
    validate_eval_set,
)
from app.evaluation.metrics import (
    evidence_hit_at_k,
    normaliza_texto,
    precision_at_k,
    recall_at_k,
)
from app.evaluation.runner import ChunkDocumento, DocumentoEvaluado, run_evaluation
from tests.fakes import StubEmbedder

# Los PDF commiteados: los mismos archivos que sube el smoke (sección 10
# y 18) y contra los que está etiquetado el set. El chunking usa los
# defaults de Settings (800/120): si cambian, este pin y las etiquetas se
# revisan.
DATA = Path(__file__).resolve().parents[2] / "scripts" / "data"

UMBRAL = 0.45
PAGINAS_POR_DOCUMENTO: Mapping[str, int] = {
    doc.clave: doc.paginas for doc in DOCUMENTOS_EVAL
}


def _chunk(pagina: int, contenido: str, score: float, documento: str) -> RetrievedChunk:
    return RetrievedChunk(
        document=documento, page=pagina, content=contenido, score=score
    )


@lru_cache(maxsize=1)
def _documentos_evaluados() -> dict[str, DocumentoEvaluado]:
    """Las tres pólizas reales (extracción + chunking de producción) con
    un id distinto por clave — el wiring del runner tiene que acotar cada
    caso a SU póliza, y para verlo hacen falta ids distintos."""
    resueltos: dict[str, DocumentoEvaluado] = {}
    for doc in DOCUMENTOS_EVAL:
        data = (DATA / doc.filename).read_bytes()
        paginas = build_default_extractor().extract(data, "application/pdf")
        chunks = tuple(
            (chunk.page_number, chunk.content)
            for chunk in chunk_document(paginas, chunk_size=800, chunk_overlap=120)
        )
        resueltos[doc.clave] = DocumentoEvaluado(
            clave=doc.clave,
            document_id=uuid.uuid5(uuid.NAMESPACE_URL, f"faro-eval-{doc.clave}"),
            filename=doc.filename,
            chunks=tuple(
                ChunkDocumento(pagina=p, contenido=c) for p, c in chunks
            ),
        )
    return resueltos


def _caso(id_: str) -> EvalCase:
    caso = next(c for c in EVAL_SET if c.id == id_)
    assert caso is not None, f"caso inexistente en el set: {id_}"
    return caso


# --- métricas puras ----------------------------------------------------------


def test_precision_at_k_cuenta_paginas_relevantes_en_los_top_k():
    chunks = [
        _chunk(1, "a", 0.8, "d"),
        _chunk(1, "b", 0.7, "d"),
        _chunk(2, "c", 0.6, "d"),
        _chunk(3, "d", 0.5, "d"),
        _chunk(2, "e", 0.4, "d"),
    ]
    relevantes = frozenset({1})
    assert precision_at_k(chunks, relevantes, 5) == pytest.approx(2 / 5)
    assert precision_at_k(chunks, relevantes, 3) == pytest.approx(2 / 3)
    assert precision_at_k(chunks, relevantes, 1) == pytest.approx(1.0)


def test_precision_at_k_divide_por_k_aunque_devuelva_menos_chunks():
    # El clásico de IR: los puestos no recuperados no pueden ser relevantes.
    chunks = [_chunk(1, "a", 0.8, "d"), _chunk(1, "b", 0.7, "d")]
    assert precision_at_k(chunks, frozenset({1}), 5) == pytest.approx(2 / 5)


def test_recall_at_k_sobre_las_paginas_de_los_top_k():
    # Multi-página (el caso danos_agua_limite_y_franquicia): la respuesta
    # necesita una cláusula de la pág. 1 y otra de la pág. 2.
    chunks = [
        _chunk(1, "a", 0.8, "d"),
        _chunk(1, "b", 0.7, "d"),
        _chunk(1, "c", 0.6, "d"),
        _chunk(2, "d", 0.5, "d"),
        _chunk(3, "e", 0.4, "d"),
    ]
    relevantes = frozenset({1, 2})
    assert recall_at_k(chunks, relevantes, 5) == pytest.approx(1.0)
    # Con k=3 solo aparece la página 1: la mitad de la respuesta.
    assert recall_at_k(chunks, relevantes, 3) == pytest.approx(0.5)


def test_metricas_rechazan_argumentos_invalidos():
    chunks = [_chunk(1, "a", 0.8, "d")]
    with pytest.raises(ValueError, match="positivo"):
        precision_at_k(chunks, frozenset({1}), 0)
    # Sin páginas relevantes no hay P/R: es un caso fuera de dominio,
    # que se mide por el umbral (D13), no por precisión/recall.
    with pytest.raises(ValueError, match="fuera de dominio"):
        precision_at_k(chunks, frozenset(), 5)
    with pytest.raises(ValueError, match="fuera de dominio"):
        recall_at_k(chunks, frozenset(), 5)
    with pytest.raises(ValueError, match="sin snippets"):
        evidence_hit_at_k(chunks, (), 5)
    with pytest.raises(ValueError, match="snippet vacío"):
        evidence_hit_at_k(chunks, ("   ",), 5)


def test_hit_at_k_exige_todas_las_piezas_verbatim():
    limite = _chunk(1, "máximo de 30.000 euros por siniestro", 0.9, "hogar")
    franquicia = _chunk(2, "franquicia de 300 euros por siniestro", 0.8, "hogar")
    ruido = _chunk(3, "texto sin relación", 0.7, "auto")
    piezas = ("máximo de 30.000 euros por siniestro", "franquicia de 300 euros")
    assert evidence_hit_at_k([limite, franquicia, ruido], piezas, 5)
    # Una respuesta que necesita dos cláusulas no está completa con una.
    assert not evidence_hit_at_k([limite, ruido], piezas, 5)


def test_hit_at_k_normaliza_el_whitespace_como_la_cita():
    # D12: la misma tolerancia del verificador de citas — colapsar
    # espacios sin abrir la puerta a paráfrasis.
    chunk = _chunk(
        2, "…se aplica una\nfranquicia de\t300 euros por siniestro, que será…",
        0.7, "hogar",
    )
    assert evidence_hit_at_k([chunk], ("franquicia de 300 euros por siniestro",), 1)
    assert not evidence_hit_at_k([chunk], ("franquicia de quinientos euros",), 1)


# --- el set y su ancla con los PDF reales ----------------------------------------


@pytest.mark.parametrize("documento", DOCUMENTOS_EVAL)
def test_cada_pdf_del_repo_es_el_que_dice_el_set(documento):
    # El ancla sha256 de cada póliza: si se sustituye un PDF, el hash del
    # set debe actualizarse JUNTO con sus etiquetas — y esto lo grita.
    pdf = DATA / documento.filename
    assert pdf.exists(), f"falta el PDF del set: {pdf}"
    assert (
        hashlib.sha256(pdf.read_bytes()).hexdigest() == documento.sha256
    ), documento.filename


def test_todos_los_snippets_del_set_existen_verbatim_en_su_pdf_real():
    documentos = _documentos_evaluados()
    for caso in EVAL_SET:
        if caso.fuera_de_dominio:
            continue
        chunks = documentos[caso.documento].chunks
        for snippet in caso.snippets:
            paginas = {
                chunk.pagina
                for chunk in chunks
                if normaliza_texto(snippet) in normaliza_texto(chunk.contenido)
            }
            assert paginas, (
                f"{caso.id}: el snippet no está en {caso.documento}: {snippet!r}"
            )
            assert paginas & caso.paginas_relevantes, (
                f"{caso.id}: el snippet está en {sorted(paginas)} de "
                f"{caso.documento}, no en las etiquetadas como relevantes "
                f"{sorted(caso.paginas_relevantes)}"
            )


def test_el_set_valida_y_tiene_la_forma_esperada():
    validate_eval_set(EVAL_SET, paginas_por_documento=PAGINAS_POR_DOCUMENTO)
    en_dominio = [c for c in EVAL_SET if not c.fuera_de_dominio]
    fuera = [c for c in EVAL_SET if c.fuera_de_dominio]
    assert len(en_dominio) == 19
    assert len(fuera) == 2
    # Al menos un caso multi-página: recall@k con |relevantes| > 1, de verdad.
    assert any(len(c.paginas_relevantes) > 1 for c in en_dominio)
    # El entregable completo: una pregunta como mínimo por cada póliza.
    assert {c.documento for c in en_dominio} == set(PAGINAS_POR_DOCUMENTO)
    assert all(len(doc.sha256) == 64 for doc in DOCUMENTOS_EVAL)


def test_validate_eval_set_rechaza_la_etiqueta_mal_puesta():
    bien = EvalCase(
        id="a", pregunta="p", documento="hogar",
        paginas_relevantes=frozenset({1}), snippets=("x",),
    )
    fuera_ok = EvalCase(id="b", pregunta="p", fuera_de_dominio=True)
    with pytest.raises(ValueError, match="con etiquetas"):
        validate_eval_set(
            [bien, EvalCase(id="c", pregunta="p", fuera_de_dominio=True,
                            paginas_relevantes=frozenset({1}))],
            paginas_por_documento=PAGINAS_POR_DOCUMENTO,
        )
    with pytest.raises(ValueError, match="sin páginas relevantes"):
        validate_eval_set(
            [EvalCase(id="a", pregunta="p", documento="hogar"), fuera_ok],
            paginas_por_documento=PAGINAS_POR_DOCUMENTO,
        )
    with pytest.raises(ValueError, match="fuera de 'hogar'"):
        validate_eval_set(
            [EvalCase(id="a", pregunta="p", documento="hogar",
                      paginas_relevantes=frozenset({7}), snippets=("x",)),
             fuera_ok],
            paginas_por_documento=PAGINAS_POR_DOCUMENTO,
        )
    with pytest.raises(ValueError, match="documento desconocido"):
        validate_eval_set(
            [EvalCase(id="a", pregunta="p", documento="salud",
                      paginas_relevantes=frozenset({1}), snippets=("x",)),
             fuera_ok],
            paginas_por_documento=PAGINAS_POR_DOCUMENTO,
        )
    with pytest.raises(ValueError, match="anclado a un documento"):
        validate_eval_set(
            [bien, EvalCase(id="c", pregunta="p", fuera_de_dominio=True,
                            documento="hogar")],
            paginas_por_documento=PAGINAS_POR_DOCUMENTO,
        )


# --- el runner con dobles (D01) ----------------------------------------------


class RetrieverSecuencial:
    """RetrievalPort de test: la n-ésima llamada devuelve la n-ésima lista.

    ``run_evaluation`` busca una vez por caso en el orden del dataset; el
    ``FakeRetriever`` de fakes.py devuelve siempre lo mismo y aquí cada
    caso necesita su ranking.
    """

    def __init__(self, resultados: list[list[RetrievedChunk]]) -> None:
        self._pendientes = list(resultados)
        self.llamadas: list[dict] = []

    def search(self, query_embedding, *, top_k, document_id=None):
        self.llamadas.append({"top_k": top_k, "document_id": document_id})
        assert self._pendientes, "búsqueda inesperada: el doble se quedó sin listas"
        return self._pendientes.pop(0)[:top_k]


def _ranking_perfecto(
    documentos: Mapping[str, DocumentoEvaluado],
    dataset=None,
    *,
    score_relevante: float = 0.60,
    score_ruido: float = 0.40,
    score_fuera: float = 0.30,
) -> list[list[RetrievedChunk]]:
    """Un ranking por caso del set: páginas relevantes arriba (ordenado por
    score, como hace PgVectorRetriever).

    En dominio: los chunks de SU póliza. Fuera de dominio: los de TODAS
    las pólizas del set (la base entera que vería una búsqueda sin acote).
    """
    rankings: list[list[RetrievedChunk]] = []
    for caso in dataset or EVAL_SET:
        if caso.fuera_de_dominio:
            lista = [
                _chunk(chunk.pagina, chunk.contenido, score_fuera, doc.filename)
                for doc in documentos.values()
                for chunk in doc.chunks
            ]
        else:
            doc = documentos[caso.documento]
            lista = [
                _chunk(
                    chunk.pagina, chunk.contenido,
                    score_relevante if chunk.pagina in caso.paginas_relevantes
                    else score_ruido,
                    doc.filename,
                )
                for chunk in doc.chunks
            ]
        rankings.append(sorted(lista, key=lambda chunk: chunk.score, reverse=True))
    return rankings


def _correr(
    retriever: RetrieverSecuencial,
    dataset=None,
    *,
    umbral: float = UMBRAL,
) -> object:
    return run_evaluation(
        embedder=StubEmbedder(),
        retriever=retriever,
        dataset=dataset or EVAL_SET,
        documentos=_documentos_evaluados(),
        umbral=umbral,
    )


def test_el_set_completo_verde_con_recuperacion_perfecta():
    documentos = _documentos_evaluados()
    retriever = RetrieverSecuencial(_ranking_perfecto(documentos))
    reporte = _correr(retriever)

    assert reporte.ok
    assert reporte.etiquetas_verificadas
    assert reporte.hit5 == (19, 19)
    assert reporte.fuera_aceptados == (0, 2)
    assert reporte.documentos == tuple(doc.filename for doc in documentos.values())
    resumen = reporte.lineas_resumen()
    assert "EVAL resultado=OK" in resumen
    assert "EVAL hit5=19/19" in resumen
    assert "EVAL fuera_dominio_aceptados=0/2" in resumen

    # El wiring de los dos regímenes: en dominio acota por el id de SU
    # póliza (uno distinto por documento — las cláusulas gemelas de auto
    # y hogar no se pueden cruzar); fuera de dominio, sobre TODA la base.
    en_dominio = [c for c in EVAL_SET if not c.fuera_de_dominio]
    llamadas_en = retriever.llamadas[: len(en_dominio)]
    for caso, llamada in zip(en_dominio, llamadas_en, strict=True):
        id_esperado = documentos[caso.documento].document_id
        assert llamada["document_id"] == id_esperado, caso.id
        assert llamada["top_k"] == 5
    for llamada in retriever.llamadas[len(en_dominio):]:
        assert llamada["document_id"] is None


def test_el_reporte_muestra_metricas_medias_y_barrido_de_umbral():
    documentos = _documentos_evaluados()
    reporte = _correr(RetrieverSecuencial(_ranking_perfecto(documentos)))
    texto = str(reporte)
    assert "P@1" in texto and "P@5" in texto and "R@5" in texto and "hit@5" in texto
    assert "MEDIA" in texto
    assert "Barrido de umbral" in texto
    assert "[0.45]" in texto  # el umbral del stack, marcado en el barrido
    assert "en dominio aceptado" in texto
    assert "fuera de dominio aceptado" in texto
    # Las tres pólizas del entregable, a la vista.
    assert "Documentos (3)" in texto
    for doc in DOCUMENTOS_EVAL:
        assert doc.filename in texto


def test_resultado_de_caso_lleva_las_metricas_exactas():
    documentos = _documentos_evaluados()
    hogar = documentos["hogar"]
    # exclusiones vive en la pág. 2 del hogar: su chunk arriba, el resto detrás.
    ranking = sorted(
        [
            _chunk(chunk.pagina, chunk.contenido,
                   0.60 if chunk.pagina == 2 else 0.40, hogar.filename)
            for chunk in hogar.chunks
        ],
        key=lambda chunk: chunk.score,
        reverse=True,
    )
    bajo = [
        _chunk(chunk.pagina, chunk.contenido, 0.30, doc.filename)
        for doc in documentos.values()
        for chunk in doc.chunks
    ]
    dataset = [_caso("exclusiones"), _caso("receta_tortilla")]
    reporte = _correr(RetrieverSecuencial([ranking, bajo]), dataset=dataset)

    (resultado,) = reporte.en_dominio
    assert resultado.id == "exclusiones"
    assert resultado.documento == "hogar"
    assert resultado.mejor_score == pytest.approx(0.60)
    assert resultado.precision_1 == pytest.approx(1.0)  # el top-1 es de la pág. 2
    assert resultado.precision_5 == pytest.approx(2 / 5)  # 2 chunks de la pág. 2 / k=5
    assert resultado.recall_5 == pytest.approx(1.0)  # la pág. 2 aparece en los top-5
    assert resultado.hit_5 is True
    assert resultado.aceptado is True  # 0.60 >= 0.45 (D13)


def test_falla_si_un_caso_no_encuentra_su_evidencia_en_los_top_5():
    documentos = _documentos_evaluados()
    hogar = documentos["hogar"]
    # Ranking que deja fuera TODA la página 2 (donde vive la respuesta).
    sin_pagina_2 = [
        _chunk(chunk.pagina, chunk.contenido, 0.60, hogar.filename)
        for chunk in hogar.chunks
        if chunk.pagina != 2
    ]
    dataset = [_caso("exclusiones"), _caso("receta_tortilla")]
    reporte = _correr(
        RetrieverSecuencial([sin_pagina_2, sin_pagina_2]), dataset=dataset
    )

    assert not reporte.ok
    assert reporte.hit5 == (0, 1)
    assert "EVAL fallo=evidencia_incompleta" in reporte.lineas_resumen()


def test_falla_si_una_pregunta_fuera_de_dominio_supera_el_umbral():
    documentos = _documentos_evaluados()
    fuertes = [
        _chunk(chunk.pagina, chunk.contenido, 0.60, doc.filename)
        for doc in documentos.values()
        for chunk in doc.chunks
    ]
    dataset = [_caso("suma_asegurada"), _caso("receta_tortilla")]
    reporte = _correr(RetrieverSecuencial([fuertes, fuertes]), dataset=dataset)

    assert not reporte.ok
    assert reporte.fuera_aceptados == (1, 1)
    assert "EVAL fallo=fuera_de_dominio_aceptado" in reporte.lineas_resumen()


def test_el_umbral_es_inclusivo_como_la_politica_d13():
    documentos = _documentos_evaluados()
    # 0.45 EXACTO se acepta: >= inclusivo, la misma semántica de
    # decide_retrieval (el test_retrieval la pinea para la política).
    al_umbral = [
        _chunk(chunk.pagina, chunk.contenido, UMBRAL, doc.filename)
        for doc in documentos.values()
        for chunk in doc.chunks
    ]
    dataset = [_caso("suma_asegurada"), _caso("receta_tortilla")]
    reporte = _correr(RetrieverSecuencial([al_umbral, al_umbral]), dataset=dataset)

    assert reporte.fuera_aceptados == (1, 1)  # al umbral exacto: aceptado
    assert not reporte.ok


def test_falla_si_las_etiquetas_no_cuadran_con_su_documento():
    documentos = _documentos_evaluados()
    bien = [
        _chunk(chunk.pagina, chunk.contenido, 0.60, doc.filename)
        for doc in documentos.values()
        for chunk in doc.chunks
    ]
    bajo = [
        _chunk(chunk.pagina, chunk.contenido, 0.30, doc.filename)
        for doc in documentos.values()
        for chunk in doc.chunks
    ]

    # (1) snippet que no está en NINGUNA póliza
    inventado = EvalCase(
        id="etiqueta_rota",
        pregunta="¿Qué dice la póliza?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("cláusula inventada que no está en el documento",),
    )
    reporte = _correr(
        RetrieverSecuencial([bien, bajo]), dataset=[inventado, _caso("receta_tortilla")]
    )
    assert not reporte.ok
    assert not reporte.etiquetas_verificadas
    assert any("etiqueta_rota" in e for e in reporte.errores_etiquetas)
    assert "EVAL fallo=etiquetas_no_verificadas" in reporte.lineas_resumen()

    # (2) snippet real pero etiquetado en la página que no es
    mal_paginado = EvalCase(
        id="pagina_mal_etiquetada",
        pregunta="¿Cuál es la franquicia?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),  # el snippet vive en la pág. 2
        snippets=("franquicia de 300 euros por siniestro",),
    )
    reporte2 = _correr(
        RetrieverSecuencial([bien, bajo]),
        dataset=[mal_paginado, _caso("receta_tortilla")],
    )
    assert any("no en las etiquetadas" in e for e in reporte2.errores_etiquetas)
    assert not reporte2.ok

    # (3) la cláusula gemela: el snippet existe, pero en la OTRA póliza —
    # la franquicia de lunas del auto no es evidencia del hogar (D29:
    # cada caso se verifica contra SU documento, no contra la base entera).
    cruzado = EvalCase(
        id="clausula_gemela",
        pregunta="¿Cuánto es la franquicia de lunas?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("franquicia de 150 euros por siniestro",),  # vive en auto
    )
    reporte3 = _correr(
        RetrieverSecuencial([bien, bajo]),
        dataset=[cruzado, _caso("receta_tortilla")],
    )
    assert any("clausula_gemela" in e for e in reporte3.errores_etiquetas)
    assert any("no está verbatim en hogar" in e for e in reporte3.errores_etiquetas)
    assert not reporte3.ok


def test_todo_guard_main_va_al_final_del_modulo():
    """Pin de la convención que evita el NameError del runner (regresión
    D28/D29): ``python -m app.evaluation.runner`` ejecuta el cuerpo del
    módulo de arriba a abajo y dispara ``main()`` AL LLEGAR al guard —
    cualquier ``def`` colocado DESPUÉS del guard aún no está ligado cuando
    ``main`` corre (``NameError: _verifica_etiquetas``, crash del smoke
    sección 18). La suite no podía verlo sola: los tests IMPORTAN el
    módulo, el guard no se dispara y todas las defs alcanzan a ligarse —
    importar y ejecutar son caminos distintos. El pin recorre TODOS los
    módulos de ``app/``: si hay guard ``__main__``, es la última
    sentencia del módulo.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    culpables: list[str] = []
    for ruta in sorted(app_dir.rglob("*.py")):
        cuerpo = ast.parse(ruta.read_text(encoding="utf-8")).body
        posiciones = [i for i, nodo in enumerate(cuerpo) if _es_guard_main(nodo)]
        if posiciones and posiciones != [len(cuerpo) - 1]:
            culpables.append(
                f"{ruta.relative_to(app_dir)}: guard en la(s) sentencia(s) "
                f"{[p + 1 for p in posiciones]} de {len(cuerpo)}"
            )
    assert not culpables, (
        "guard __main__ a mitad de módulo: ``python -m`` ejecutaría "
        "main() antes de ligar las defs posteriores — " + "; ".join(culpables)
    )


def _es_guard_main(nodo: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` a nivel de módulo (AST, no texto)."""
    return (
        isinstance(nodo, ast.If)
        and isinstance(nodo.test, ast.Compare)
        and isinstance(nodo.test.left, ast.Name)
        and nodo.test.left.id == "__name__"
        and len(nodo.test.ops) == 1
        and isinstance(nodo.test.ops[0], ast.Eq)
        and len(nodo.test.comparators) == 1
        and isinstance(nodo.test.comparators[0], ast.Constant)
        and nodo.test.comparators[0].value == "__main__"
    )
