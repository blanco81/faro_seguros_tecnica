"""Caso de uso de consulta: el agente RAG con verificación de citas (D19).

Bucle agéntico ACOTADO — decide, itera y verifica dentro de una verja::

    planificar → recuperar → generar → verificar cita → refinar ↺ → responder

- **planificar**: una llamada al LLM decide si la pregunta se descompone
  en sub-preguntas (multi-documento) o pide aclaración (ambigüedad).
- **recuperar**: cada (sub-)pregunta se embediza (bge-m3, D10) y se
  busca por similitud coseno (``RetrievalPort``); los resultados se
  fusionan y deduplican, y ``decide_retrieval`` (D13) decide si la
  evidencia alcanza.
- **generar**: el LLM redacta la respuesta y COPIA literalmente el
  fragmento que la respalda (``quote``).
- **verificar**: el ``quote`` debe aparecer, verbatim y normalizando
  espacios, en algún chunk recuperado (D12). Es la garantía "citas
  verificadas", no solo "verificables".
- **refinar**: si la cita no cuadra, la evidencia no alcanza o el JSON
  no llegó, el LLM reformula la consulta y se reintenta — con techo.

El techo (``MAX_QUERY_ITERATIONS``) acota coste y latencia: un agente
que decide e itera dentro de una verja, no un sistema autónomo abierto
(alternativas descartadas en D19: multi-agente, tool-using, multi-turno).

Errores de proveedor (red, timeout) NO se capturan aquí: suben y la API
los traduce a 503 — un fallo de transporte no es un rechazo del dominio.
"""

import json
import re
import uuid
from dataclasses import dataclass

from app.domain.citations import Citation
from app.domain.retrieval import (
    REJECTION_MESSAGE,
    RetrievedChunk,
    decide_retrieval,
)
from app.ports.embeddings import EmbeddingProvider
from app.ports.llm import LLMProvider
from app.ports.retrieval import RetrievalPort

__all__ = [
    "MAX_QUERY_ITERATIONS",
    "UNVERIFIED_MESSAGE",
    "AgenticQueryService",
    "QueryAnswer",
]

#: Techo de iteraciones del bucle (D19): 1 intento base + refinamiento
#: por query mal formulada + refinamiento por cita sin verificar. Constante
#: a conciencia (mismo criterio que D13/D16): sin set de evaluación que
#: justifique más perillas.
MAX_QUERY_ITERATIONS = 3

#: Rechazo específico del bucle: hubo evidencia y borradores, pero ninguno
#: pudo respaldarse con una cita literal verificada.
UNVERIFIED_MESSAGE = (
    "Encontré fragmentos relacionados, pero no pude verificar ninguna cita "
    "literal que respalde la respuesta con certeza. Pruebe reformular la "
    "pregunta con más detalle."
)

#: Razones de rechazo que el endpoint expone tal cual.
_REASON_NO_RESULTS = "no_results"
_REASON_BELOW_THRESHOLD = "below_threshold"
_REASON_UNVERIFIED = "unverified_citation"
_REASON_EMPTY = "empty_question"

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class QueryAnswer:
    """Resultado de la consulta para el endpoint (contrato de la Fase 4).

    ``trace`` es el registro de iteraciones (motivo de cada reintento):
    hoy alimenta los tests; en la Fase 5 alimenta la observabilidad.
    """

    answer: str | None
    citations: tuple[Citation, ...]
    refused: bool
    reason: str | None
    message: str | None
    clarification: str | None
    iterations: int
    trace: tuple[dict, ...]


class AgenticQueryService:
    """El bucle de consulta: puertos entran, una QueryAnswer sale."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        embedder: EmbeddingProvider,
        retriever: RetrievalPort,
        rejection_threshold: float,
        max_iterations: int = MAX_QUERY_ITERATIONS,
        top_k: int = 5,
    ) -> None:
        if not 0 <= rejection_threshold <= 1:
            raise ValueError(
                f"rejection_threshold debe estar en [0, 1] "
                f"(recibido: {rejection_threshold})"
            )
        if max_iterations < 1:
            raise ValueError(f"max_iterations debe ser >= 1 (recibido: {max_iterations})")
        self._llm = llm
        self._embedder = embedder
        self._retriever = retriever
        self._threshold = rejection_threshold
        self._max_iterations = max_iterations
        self._top_k = top_k

    # --- el bucle -----------------------------------------------------------

    def answer(
        self, question: str, *, document_id: uuid.UUID | None = None
    ) -> QueryAnswer:
        """Responde, rehúsa o pide aclaración (nunca lanza por negocio)."""
        consulta = question.strip()
        if not consulta:
            return self._refuse(
                reason=_REASON_EMPTY,
                message=REJECTION_MESSAGE,
                iterations=0,
                trace=(),
            )

        subpreguntas = self._plan(consulta)
        if isinstance(subpreguntas, str):
            # El planificador pidió aclaración antes de buscar nada.
            return QueryAnswer(
                answer=None,
                citations=(),
                refused=False,
                reason=None,
                message=None,
                clarification=subpreguntas,
                iterations=0,
                trace=(),
            )

        queries: list[str] = subpreguntas
        trace: list[dict] = []

        for iteracion in range(1, self._max_iterations + 1):
            chunks = self._retrieve(queries, document_id)
            if not chunks:
                return self._refuse(
                    reason=_REASON_NO_RESULTS,
                    message=REJECTION_MESSAGE,
                    iterations=iteracion,
                    trace=trace,
                )

            decision = decide_retrieval(chunks, self._threshold, self._top_k)
            if not decision.answered:
                # Evidencia por debajo del umbral (D13): la query quizá
                # está mal formulada — refinarla puede cruzar el umbral.
                trace.append(
                    {
                        "iteracion": iteracion,
                        "motivo": decision.reason,
                        "mejor_score": decision.best_score,
                    }
                )
                if iteracion == self._max_iterations:
                    return self._refuse(
                        reason=_REASON_BELOW_THRESHOLD,
                        message=REJECTION_MESSAGE,
                        iterations=iteracion,
                        trace=trace,
                    )
                queries = self._refine(
                    consulta, "la evidencia recuperada no alcanza el umbral", None
                )
                continue

            borrador = self._generate(consulta, decision.chunks)
            if borrador is None or not borrador.get("answer"):
                # Sin respuesta utilizable: el modelo dijo que el contexto
                # no contiene la respuesta, o devolvió algo unparseable.
                trace.append(
                    {"iteracion": iteracion, "motivo": "empty_or_unparseable"}
                )
                if iteracion == self._max_iterations:
                    return self._refuse(
                        reason=_REASON_BELOW_THRESHOLD,
                        message=REJECTION_MESSAGE,
                        iterations=iteracion,
                        trace=trace,
                    )
                queries = self._refine(
                    consulta, "el contexto recuperado no contiene la respuesta", None
                )
                continue

            chunk = self._chunk_con_cita(borrador.get("quote", ""), decision.chunks)
            if chunk is not None:
                # Cita verificada (D12): el fragmento es literal del chunk.
                cita = Citation(
                    document=chunk.document,
                    page=chunk.page,
                    quote=borrador["quote"].strip(),
                )
                return QueryAnswer(
                    answer=borrador["answer"].strip(),
                    citations=(cita,),
                    refused=False,
                    reason=None,
                    message=None,
                    clarification=None,
                    iterations=iteracion,
                    trace=tuple(trace),
                )

            trace.append({"iteracion": iteracion, "motivo": "citation_mismatch"})
            if iteracion == self._max_iterations:
                return self._refuse(
                    reason=_REASON_UNVERIFIED,
                    message=UNVERIFIED_MESSAGE,
                    iterations=iteracion,
                    trace=trace,
                )
            queries = self._refine(
                consulta,
                "la cita del borrador no coincide literalmente con ningún fragmento",
                borrador.get("answer", ""),
            )

        # Inalcanzable: el for siempre retorna en la última iteración.
        raise RuntimeError("bucle de consulta sin desenlace")  # pragma: no cover

    # --- pasos del bucle ------------------------------------------------------

    def _plan(self, consulta: str) -> list[str] | str:
        """Planifica: sub-preguntas, o un string pidiendo aclaración.

        Degrada con gracia: si el LLM no devuelve JSON parseable, se
        busca con la pregunta original (el planificador no puede tumbar
        la consulta).
        """
        respuesta = self._llm.chat(
            f"TAREA: PLANIFICAR\nPREGUNTA: {consulta}\n\n"
            "Decide si la pregunta necesita dividirse en sub-preguntas para "
            "buscar en documentos (típico cuando referencia varios documentos "
            "o varios temas). Si es ambigua de forma irresoluble (por "
            "ejemplo, \"la póliza\" cuando hay varias sin decir cuál), pide "
            "aclaración en lugar de planificar.\n"
            "Devuelve SOLO un JSON válido:\n"
            '{"subquestions": ["...", ...]}\n'
            'o bien:\n{"clarification": "pregunta breve de aclaración"}',
            system="Eres un planificador de consultas de un asistente de seguros. "
            "Respondes únicamente JSON.",
        )
        datos = _extrae_json(respuesta)
        if datos is None:
            return [consulta]
        if datos.get("clarification"):
            return str(datos["clarification"])
        subpreguntas = [str(q).strip() for q in datos.get("subquestions", []) if str(q).strip()]
        return subpreguntas or [consulta]

    def _retrieve(
        self, queries: list[str], document_id: uuid.UUID | None
    ) -> list[RetrievedChunk]:
        """Embediza cada query, busca y fusiona con deduplicación."""
        vistos: dict[tuple[str, int, str], RetrievedChunk] = {}
        embeddings = self._embedder.embed(queries)
        for embedding in embeddings:
            for chunk in self._retriever.search(
                embedding, top_k=self._top_k, document_id=document_id
            ):
                clave = (chunk.document, chunk.page, chunk.content)
                previo = vistos.get(clave)
                if previo is None or chunk.score > previo.score:
                    vistos[clave] = chunk
        return sorted(vistos.values(), key=lambda c: c.score, reverse=True)

    def _generate(self, consulta: str, chunks: tuple[RetrievedChunk, ...]) -> dict | None:
        """Redacta la respuesta con cita literal; None si no hay JSON válido."""
        fragmentos = "\n\n".join(
            f"FRAGMENTO {i} (documento {c.document}, página {c.page}):\n{c.content}"
            for i, c in enumerate(chunks, start=1)
        )
        respuesta = self._llm.chat(
            f"TAREA: RESPONDER\nPREGUNTA: {consulta}\n\n"
            f"Contexto recuperado (fragmentos literales de los documentos):\n\n"
            f"{fragmentos}\n\n"
            "Responde la pregunta BASÁNDOTE SOLO en los fragmentos anteriores. "
            "En \"quote\" copia un fragmento LITERAL (máximo 220 caracteres) "
            "de uno de los fragmentos que respalda tu respuesta: quien "
            "evaluará hará ctrl+F sobre el documento y debe encontrarlo "
            "exactamente. Si los fragmentos no contienen la respuesta, "
            "devuelve answer vacío.\n"
            "Devuelve SOLO un JSON válido:\n"
            '{"answer": "...", "quote": "..."}',
            system="Eres un asistente de siniestros de una aseguradora. Citas "
            "literalmente las cláusulas. Respondes únicamente JSON.",
        )
        return _extrae_json(respuesta)

    def _refine(self, consulta: str, motivo: str, borrador: str | None) -> list[str]:
        """Reformula la consulta (no responde). Degrada a la original."""
        contexto_borrador = f"\nÚltimo borrador rechazado: {borrador}" if borrador else ""
        respuesta = self._llm.chat(
            f"TAREA: REFINAR\nPREGUNTA: {consulta}\n\n"
            f"El intento anterior falló: {motivo}.{contexto_borrador}\n\n"
            "Reformula la pregunta para mejorar la recuperación de documentos "
            "(más específica, sinónimos, descomposición). NO respondas la "
            "pregunta: SOLO reformúlala.\n"
            "Devuelve SOLO un JSON válido:\n"
            '{"query": "..."}',
            system="Eres un reformulador de consultas. Respondes únicamente JSON.",
        )
        datos = _extrae_json(respuesta)
        query = str(datos.get("query", "")).strip() if datos else ""
        return [query] if query else [consulta]

    # --- verificación y composición ------------------------------------------

    @staticmethod
    def _chunk_con_cita(quote: str, chunks: tuple[RetrievedChunk, ...]) -> RetrievedChunk | None:
        """El chunk que contiene la cita verbatim (espacios normalizados).

        Es el check D12 con la tolerancia mínima indispensable: colapsar
        espacios en blanco (el LLM copia a veces con saltos distintos) sin
        abrir la puerta a paráfrasis — la normalización no cambia las
        palabras, solo el whitespace.
        """
        cita = _normaliza(quote)
        if not cita:
            return None
        for chunk in chunks:
            if cita in _normaliza(chunk.content):
                return chunk
        return None

    @staticmethod
    def _refuse(
        *, reason: str, message: str, iterations: int, trace: list[dict] | tuple
    ) -> QueryAnswer:
        return QueryAnswer(
            answer=None,
            citations=(),
            refused=True,
            reason=reason,
            message=message,
            clarification=None,
            iterations=iterations,
            trace=tuple(trace),
        )


def _normaliza(texto: str) -> str:
    """Colapsa todo el whitespace a espacios simples (para comparar verbatim)."""
    return " ".join(texto.split())


def _extrae_json(texto: str) -> dict | None:
    """Extrae el primer objeto JSON de la respuesta del LLM (o None).

    Tolerante a que el modelo envuelva el JSON en ```json ... ``` o añada
    texto alrededor — pero SIN inventar: si no hay objeto parseable, None.
    """
    try:
        datos = json.loads(texto)
        return datos if isinstance(datos, dict) else None
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(texto)
    if match:
        try:
            datos = json.loads(match.group(0))
            return datos if isinstance(datos, dict) else None
        except json.JSONDecodeError:
            return None
    return None
