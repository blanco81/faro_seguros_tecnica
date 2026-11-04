"""Corre el set de evaluación contra el stack REAL (D28; tres pólizas D29).

    docker compose exec -T api python -m app.evaluation.runner
    (equivale a ``make eval``; el smoke lo corre como su sección 18)

Composición idéntica a la de producción: ``build_embedder`` (D14, el
proveedor configurado en el stack), ``PgVectorRetriever`` (D02/D13) y la
política ``decide_retrieval`` para la decisión aceptado/rechazado — la
evaluación mide el sistema que responde, no una variante de laboratorio.

Dos regímenes, cada pregunta en el que le aplica:

- **en dominio**: búsqueda ACOTADA a la póliza del caso (por id). Mide
  la calidad de embedizar + rankear sin el ruido de otros documentos
  acumulados (sondas del smoke, las OTRAS dos pólizas del set — que
  comparten léxico de oficio: franquicias, exclusiones, plazos). Números
  reproducibles de corrida a corrida.
- **fuera de dominio**: búsqueda sobre TODA la base. En producción el
  analista pregunta sin acotar y si CUALQUIER chunk supera el umbral el
  sistema respondería lo que no debe (D13) — la seguridad del umbral se
  mide con todos los distractores reales presentes.

El reporte lleva tres bloques: la tabla por caso (documento, mejor score,
P@1, P@5, R@5, hit@5), las medias, y el barrido de umbral — la «curva»
que D20 dejó pendiente: a cada umbral, qué fracción de preguntas en
dominio se aceptaría (más es mejor) y qué fracción de las fuera de
dominio colaría (menos es mejor). El umbral configurado en el stack va
marcado.

Sale 0 solo si: los tres documentos localizados y ready, etiquetas
verificadas contra los chunks reales de la base, hit@5 completo en
dominio y 0 aceptados fuera de dominio. Son exactamente las propiedades
que el smoke ya verificaba end-to-end por los dos casos polo (15A/15B),
ahora medidas sobre el set completo — con la particularidad de que hit@5
sobre pólizas de 4-6 chunks ≤ top_k=5 es estructural (los top-5
contienen todos los chunks; si el chunking cambia, el mismo check se
convierte en prueba de ranking). P@1, P@5 y R@5 se REPORTAN como
evidencia, no son puerta de salida: los suelos de calidad se fijarán con
corridas reales acumuladas (mismo criterio que D20 — sin teclas sin
evidencia).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app.adapters.db.models import Chunk, Document
from app.adapters.db.session import session_scope
from app.adapters.factory import build_embedder, build_retriever
from app.config import get_settings
from app.domain.document import DocumentStatus
from app.domain.retrieval import RetrievedChunk, decide_retrieval
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
from app.ports.embeddings import EmbeddingProvider
from app.ports.retrieval import RetrievalPort

__all__ = [
    "BARRIDO_UMBRAL",
    "TOP_K",
    "ChunkDocumento",
    "DocumentoEvaluado",
    "ReporteEvaluacion",
    "ResultadoCaso",
    "run_evaluation",
]

#: El k del sistema en producción: el mismo top_k por defecto del servicio
#: de consulta (AgenticQueryService). La evaluación mide EL sistema, no
#: una rejilla de k's — un k distinto sería otro sistema.
TOP_K = 5

#: Umbrales del barrido (la evidencia de calibración que D20 dejó
#: pendiente): la franja donde vive el 0.45 del stack.
BARRIDO_UMBRAL: tuple[float, ...] = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


@dataclass(frozen=True)
class ChunkDocumento:
    """Un chunk del documento etiquetado, sin score.

    Para verificar las etiquetas solo hacen falta página y contenido; el
    score es cosa de la búsqueda, no de la verdad conocida.
    """

    pagina: int
    contenido: str


@dataclass(frozen=True)
class DocumentoEvaluado:
    """Una póliza del set ya localizada en la base (D29).

    ``clave`` es la que llevan los casos del dataset; ``chunks`` son los
    reales de la base — la verdad contra la que se verifican las
    etiquetas y el acote de la búsqueda en dominio.
    """

    clave: str
    document_id: uuid.UUID
    filename: str
    chunks: tuple[ChunkDocumento, ...]


@dataclass(frozen=True)
class ResultadoCaso:
    """Lo medido sobre un caso.

    Las métricas P/R/hit son ``None`` en los casos fuera de dominio (no
    se definen: no hay páginas relevantes); ``aceptado`` es la decisión
    D13 de esa pregunta — lo que ``decide_retrieval`` diría con esos
    candidatos y ese umbral. ``documento`` es la clave de la póliza del
    caso (vacío en las fuera de dominio).
    """

    id: str
    documento: str
    mejor_score: float | None
    precision_1: float | None
    precision_5: float | None
    recall_5: float | None
    hit_5: bool | None
    aceptado: bool

    def aceptado_al(self, umbral: float) -> bool:
        """¿El mejor score de este caso alcanzaría ESE umbral?

        Para el barrido: la política D13 es todo-o-nada sobre el mejor
        resultado, así que reproducirla a otro umbral solo necesita el
        mejor score (>= inclusivo, como ``decide_retrieval``).
        """
        return self.mejor_score is not None and self.mejor_score >= umbral


@dataclass(frozen=True)
class ReporteEvaluacion:
    """El resultado completo de una corrida + su render para humanos."""

    documentos: tuple[str, ...]
    umbral: float
    proveedor: str
    en_dominio: tuple[ResultadoCaso, ...]
    fuera_dominio: tuple[ResultadoCaso, ...]
    errores_etiquetas: tuple[str, ...]

    @property
    def etiquetas_verificadas(self) -> bool:
        return not self.errores_etiquetas

    @property
    def hit5(self) -> tuple[int, int]:
        """(casos con toda la evidencia en los top-5, casos en dominio)."""
        aciertos = sum(1 for r in self.en_dominio if r.hit_5)
        return aciertos, len(self.en_dominio)

    @property
    def fuera_aceptados(self) -> tuple[int, int]:
        """(fuera de dominio aceptados por el umbral, casos fuera)."""
        aceptados = sum(1 for r in self.fuera_dominio if r.aceptado)
        return aceptados, len(self.fuera_dominio)

    @property
    def ok(self) -> bool:
        return (
            bool(self.en_dominio)
            and self.etiquetas_verificadas
            and self.hit5[0] == self.hit5[1]
            and self.fuera_aceptados[0] == 0
        )

    def lineas_resumen(self) -> list[str]:
        """Las líneas de máquina que el smoke grepea (formato estable)."""
        if not self.etiquetas_verificadas:
            fallo = "etiquetas_no_verificadas"
        elif self.hit5[0] < self.hit5[1]:
            fallo = "evidencia_incompleta"
        elif self.fuera_aceptados[0] > 0:
            fallo = "fuera_de_dominio_aceptado"
        else:
            fallo = None
        lineas = [
            f"EVAL resultado={'OK' if self.ok else 'FALLO'}",
            f"EVAL hit5={self.hit5[0]}/{self.hit5[1]}",
            (f"EVAL fuera_dominio_aceptados={self.fuera_aceptados[0]}"
            f"/{self.fuera_aceptados[1]}"),
        ]
        if fallo is not None:
            lineas.append(f"EVAL fallo={fallo}")
        return lineas

    # --- el render -------------------------------------------------------

    def __str__(self) -> str:
        ancho_id = max(
            [len(r.id) for r in self.en_dominio + self.fuera_dominio]
            + [len("MEDIA")]
        )
        lineas = [
            "=" * 80,
            (" Evaluación de recuperación — set etiquetado "
            "(D28/D29; la calibración formal de D20)"),
            (f" Documentos ({len(self.documentos)}): "
            + " · ".join(self.documentos)),
            (f" Umbral del stack: {self.umbral:.2f} · top_k: {TOP_K}"),
            f" Embeddings: {self.proveedor}" if self.proveedor else "",
            "=" * 80,
            "",
            (f"En dominio ({len(self.en_dominio)} casos — búsqueda acotada a "
            "la póliza del caso):"),
            (f"  {'id':<{ancho_id}}  doc     mejor    P@1    P@5    R@5  "
            "hit@5"),
        ]
        for r in self.en_dominio:
            lineas.append(
                f"  {r.id:<{ancho_id}}  {r.documento:<5}  {_fmt(r.mejor_score)}  "
                f"{r.precision_1:.3f}  {r.precision_5:.3f}  "
                f"{r.recall_5:.3f}  {'sí' if r.hit_5 else 'NO'}"
            )
        media = _media(self.en_dominio)
        lineas.append(
            f"  {'MEDIA':<{ancho_id}}         {_fmt(media['mejor'])}  "
            f"{_fmt(media['p1'])}  {_fmt(media['p5'])}  "
            f"{_fmt(media['r5'])}  {self.hit5[0]}/{self.hit5[1]}"
        )
        lineas += [
            "",
            (f"Fuera de dominio ({len(self.fuera_dominio)} casos — búsqueda "
            "sobre TODA la base):"),
            f"  {'id':<{ancho_id}}  mejor  aceptado@{self.umbral:.2f}",
        ]
        for r in self.fuera_dominio:
            lineas.append(
                f"  {r.id:<{ancho_id}}  {_fmt(r.mejor_score)}  "
                f"{'SÍ' if r.aceptado else 'no'}"
            )
        lineas += ["", "Barrido de umbral (evidencia de calibración, D20):"]
        lineas.append("  umbral                     " + self._fila_umbrales())
        lineas.append(
            "  en dominio aceptado        "
            + self._fila_tasas(self.en_dominio)
        )
        lineas.append(
            "  fuera de dominio aceptado  "
            + self._fila_tasas(self.fuera_dominio)
        )
        lineas += [
            "",
            "  La franja útil del umbral: la más baja que deja en 0% los fuera",
            "  de dominio (D13) aceptando el máximo de preguntas en dominio.",
        ]
        if self.errores_etiquetas:
            lineas += ["", "Etiquetas que no cuadran con su documento:"]
            lineas += [f"  - {error}" for error in self.errores_etiquetas]
        lineas += [""] + self.lineas_resumen()
        return "\n".join(lineas)

    def _umbrales_barrido(self) -> tuple[float, ...]:
        """El barrido pedido + el umbral configurado (marcado), ordenado."""
        return tuple(sorted(set(BARRIDO_UMBRAL) | {self.umbral}))

    def _fila_umbrales(self) -> str:
        """La fila de umbrales: celdas de 6 a la derecha, el del stack marcado."""
        return "  ".join(
            f"[{t:.2f}]" if abs(t - self.umbral) < 1e-9 else f"{t:>6.2f}"
            for t in self._umbrales_barrido()
        )

    def _fila_tasas(self, resultados: tuple[ResultadoCaso, ...]) -> str:
        """Una fila de tasas de aceptación alineada con la de umbrales."""
        return "  ".join(
            f"{self._tasa(resultados, t):>6.0%}" for t in self._umbrales_barrido()
        )

    def _tasa(self, resultados: tuple[ResultadoCaso, ...], umbral: float) -> float:
        """Fracción de casos aceptados a ESE umbral."""
        if not resultados:
            return 0.0
        return sum(1 for r in resultados if r.aceptado_al(umbral)) / len(resultados)


def run_evaluation(
    *,
    embedder: EmbeddingProvider,
    retriever: RetrievalPort,
    dataset: Sequence[EvalCase],
    documentos: Mapping[str, DocumentoEvaluado],
    umbral: float,
    proveedor: str = "",
) -> ReporteEvaluacion:
    """Corre el set contra los puertos inyectados y compone el reporte.

    La suite la ejercita con dobles (D01); ``main`` la llama con los
    adapters reales del stack. Las etiquetas se verifican ANTES de medir:
    un set que no cuadra con su documento no produce métricas, produce un
    error accionable.
    """
    paginas_por_documento = {
        doc.clave: max((c.pagina for c in doc.chunks), default=0)
        for doc in documentos.values()
    }
    validate_eval_set(dataset, paginas_por_documento=paginas_por_documento)
    errores = _verifica_etiquetas(dataset, documentos)

    en_dominio: list[ResultadoCaso] = []
    fuera_dominio: list[ResultadoCaso] = []
    for caso in dataset:
        # Fuera de dominio busca sobre TODA la base; en dominio, acotado
        # a LA póliza del caso.
        acote = None if caso.fuera_de_dominio else documentos[caso.documento].document_id
        chunks = _busca(embedder, retriever, caso.pregunta, acote)
        decision = decide_retrieval(chunks, umbral, TOP_K)
        if caso.fuera_de_dominio:
            fuera_dominio.append(
                ResultadoCaso(
                    id=caso.id,
                    documento="",
                    mejor_score=decision.best_score,
                    precision_1=None,
                    precision_5=None,
                    recall_5=None,
                    hit_5=None,
                    aceptado=decision.answered,
                )
            )
        else:
            en_dominio.append(
                ResultadoCaso(
                    id=caso.id,
                    documento=caso.documento,
                    mejor_score=decision.best_score,
                    precision_1=precision_at_k(chunks, caso.paginas_relevantes, 1),
                    precision_5=precision_at_k(chunks, caso.paginas_relevantes, TOP_K),
                    recall_5=recall_at_k(chunks, caso.paginas_relevantes, TOP_K),
                    hit_5=evidence_hit_at_k(chunks, caso.snippets, TOP_K),
                    aceptado=decision.answered,
                )
            )

    return ReporteEvaluacion(
        documentos=tuple(doc.filename for doc in documentos.values()),
        umbral=umbral,
        proveedor=proveedor,
        en_dominio=tuple(en_dominio),
        fuera_dominio=tuple(fuera_dominio),
        errores_etiquetas=errores,
    )


def main() -> int:
    """Entrada del módulo (``python -m app.evaluation.runner``)."""
    settings = get_settings()
    embedder = build_embedder(settings)  # fail-fast D14: el proveedor del stack
    with session_scope() as session:
        problemas: list[str] = []
        resueltos: dict[str, DocumentoEvaluado] = {}
        for esperado in DOCUMENTOS_EVAL:
            documento = session.execute(
                select(Document).where(Document.content_hash == esperado.sha256)
            ).scalar_one_or_none()
            if documento is None:
                problemas.append(
                    f"- {esperado.filename} no está en la base: súbelo "
                    f"(POST /api/documents con scripts/data/{esperado.filename})"
                )
                continue
            if documento.status is not DocumentStatus.READY:
                problemas.append(
                    f"- {esperado.filename} está en estado "
                    f"'{documento.status.value}' y necesita estar 'ready' "
                    "(la ingesta procesa chunks y embeddings; mira "
                    f"GET /api/documents/{documento.id})"
                )
                continue
            filas = session.execute(
                select(Chunk.page_number, Chunk.content).where(
                    Chunk.document_id == documento.id
                )
            ).all()
            resueltos[esperado.clave] = DocumentoEvaluado(
                clave=esperado.clave,
                document_id=documento.id,
                filename=documento.filename,
                chunks=tuple(
                    ChunkDocumento(pagina=pagina, contenido=contenido)
                    for pagina, contenido in filas
                ),
            )
        if problemas:
            print(
                "FALLO: faltan documentos del set (D29: las tres pólizas):\n"
                + "\n".join(problemas)
            )
            return 1
        reporte = run_evaluation(
            embedder=embedder,
            retriever=build_retriever(session),
            dataset=EVAL_SET,
            documentos=resueltos,
            umbral=settings.rejection_threshold,
            proveedor=f"{settings.embedding_provider} · {settings.embedding_model}",
        )
    print(reporte)
    return 0 if reporte.ok else 1


# --- piezas privadas ---------------------------------------------------------


def _busca(
    embedder: EmbeddingProvider,
    retriever: RetrievalPort,
    pregunta: str,
    document_id: uuid.UUID | None,
) -> list[RetrievedChunk]:
    """Embediza la pregunta y busca con el top_k del sistema."""
    embedding = embedder.embed([pregunta])[0]
    return retriever.search(embedding, top_k=TOP_K, document_id=document_id)


def _verifica_etiquetas(
    dataset: Sequence[EvalCase], documentos: Mapping[str, DocumentoEvaluado]
) -> tuple[str, ...]:
    """Cada snippet debe existir verbatim en un chunk de una página
    relevante DE SU DOCUMENTO.

    En las dos direcciones: el snippet está en la póliza del caso Y la
    página etiquetada como relevante es de las que de verdad lo contienen.
    Si algo no cuadra, el set miente y las métricas no significarían nada.
    """
    errores: list[str] = []
    for caso in dataset:
        if caso.fuera_de_dominio:
            continue
        chunks = documentos[caso.documento].chunks
        normalizados = [
            (chunk.pagina, normaliza_texto(chunk.contenido)) for chunk in chunks
        ]
        for snippet in caso.snippets:
            objetivo = normaliza_texto(snippet)
            paginas_donde_esta = {
                pagina
                for pagina, contenido in normalizados
                if objetivo and objetivo in contenido
            }
            if not paginas_donde_esta:
                errores.append(
                    f"{caso.id}: el snippet no está verbatim en "
                    f"{caso.documento}: «{snippet[:60]}…»"
                )
            elif not paginas_donde_esta & caso.paginas_relevantes:
                errores.append(
                    f"{caso.id}: el snippet está en la(s) página(s) "
                    f"{sorted(paginas_donde_esta)} de {caso.documento}, no "
                    f"en las etiquetadas como relevantes "
                    f"{sorted(caso.paginas_relevantes)}"
                )
    return tuple(errores)


def _fmt(score: float | None) -> str:
    """Un score para las columnas numéricas (em-dash si no hubo resultados)."""
    return "  —  " if score is None else f"{score:.3f}"


def _media(resultados: Sequence[ResultadoCaso]) -> dict[str, float | None]:
    """Medias de los campos numéricos de los casos en dominio."""
    if not resultados:
        return {"mejor": None, "p1": None, "p5": None, "r5": None}

    def _prom(campo: str) -> float | None:
        valores = [getattr(r, campo) for r in resultados]
        if any(v is None for v in valores):
            return None
        return sum(valores) / len(valores)  # type: ignore[arg-type]

    mejores = [r.mejor_score for r in resultados if r.mejor_score is not None]
    return {
        "mejor": sum(mejores) / len(mejores) if mejores else None,
        "p1": _prom("precision_1"),
        "p5": _prom("precision_5"),
        "r5": _prom("recall_5"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
