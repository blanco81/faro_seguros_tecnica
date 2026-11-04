#!/usr/bin/env python3
"""Verificación LOCAL (sin Docker) de la telemetría — Fase 5, pasos 1-3.

El smoke del stack (sección 17) es la prueba canónica contra el collector
de verdad; este script existe para lo que el sandbox/CI no puede levantar:
arranca un receptor OTLP/HTTP en 127.0.0.1 y comprueba, con la MISMA ruta
de producción (create_app + setup_telemetry reales, OTEL_ENABLED=true):

  A. el receptor recibe POSTs en /v1/traces (export OTLP funciona)
  B. los spans llegan con service.name=faro-api
  C. GET /health genera el span HTTP del servidor ("GET /health")
  D. una llamada httpx genera el span de cliente — el mismo camino que
     usa el SDK de OpenAI para hablar con el proveedor LLM/embeddings
  E. el productor Celery (send_task con broker memory://) INYECTA el
     contexto: las cabeceras del mensaje llevan traceparent (paso 3)
  F. la ejecución de una tarea (ping en modo eager) genera su span
     "tasks.ping/run" (la mitad consumidora del paso 3)
  G. POST /api/query en la app REAL emite los spans de dominio RAG
     (rag.consulta + rag.iteracion, D27) con el tracer POR DEFECTO —
     el mismo wiring de producción, sin inyectar nada

Uso:  python3 scripts/verifica_telemetria_local.py   (desde la raíz del repo)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Corre "desde la raíz del repo" (como dice el docstring): app/ vive en
# backend/, así que lo ponemos en el path sin exigir PYTHONPATH a mano.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

PUERTO = 14318

# El modo fake de proveedores evita el fail-fast de credenciales al importar
# celery_app (igual que la suite); la telemetría no depende del proveedor.
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["OTEL_ENABLED"] = "true"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{PUERTO}"

RESULTADOS: list[tuple[str, bool, str]] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    RESULTADOS.append((nombre, ok, detalle))


# --- receptor OTLP: guarda los cuerpos protobuf de /v1/traces -------------


class ReceptorOTLP(BaseHTTPRequestHandler):
    cuerpos: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 — API de http.server
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(n)
        if self.path == "/v1/traces":
            ReceptorOTLP.cuerpos.append(cuerpo)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silencio
        pass


def main() -> int:
    servidor = HTTPServer(("127.0.0.1", PUERTO), ReceptorOTLP)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # --- C: la app REAL instrumentada sirve y traza -----------------------
    from fastapi.testclient import TestClient

    from app.main import create_app
    from opentelemetry import trace

    app = create_app()
    with TestClient(app) as client:
        respuesta = client.get("/health")
    check(
        "GET /health responde 200",
        respuesta.status_code == 200,
        f"status={respuesta.status_code}",
    )

    provider_global = trace.get_tracer_provider()

    # --- G: el bucle RAG de la ruta REAL, con el tracer por defecto (D27) --
    # La ruta /api/query con dobles de retriever/embedder (sin base) — el
    # servicio NO recibe tracer: usa trace.get_tracer(), que delega en el
    # provider global que create_app acaba de montar. Es exactamente el
    # wiring de producción; los spans deben salir por el mismo OTLP.
    from app.adapters.http.query import get_query_service
    from app.domain.retrieval import RetrievedChunk
    from app.services.query import AgenticQueryService
    from tests.fakes import FakeRetriever, ScriptedLLM, StubEmbedder

    pregunta_rag = "¿Cuál es la franquicia aplicable a los daños por agua?"
    chunk_rag = RetrievedChunk(
        document="poliza-hogar.pdf",
        page=2,
        content=(
            "Cláusula 7.4 — Franquicia. Para la cobertura de daños por agua "
            "se aplica una franquicia de 300 euros por siniestro."
        ),
        score=0.87,
    )
    llm_rag = ScriptedLLM(
        json.dumps({"subquestions": [pregunta_rag]}),
        json.dumps(
            {
                "answer": "La franquicia es de 300 euros por siniestro.",
                "quote": "se aplica una franquicia de 300 euros por siniestro",
            }
        ),
    )
    app.dependency_overrides[get_query_service] = lambda: AgenticQueryService(
        llm=llm_rag,
        embedder=StubEmbedder(),
        retriever=FakeRetriever([chunk_rag]),
        rejection_threshold=0.45,
    )
    try:
        with TestClient(app) as client:
            respuesta_rag = client.post("/api/query", json={"question": pregunta_rag})
        check(
            "POST /api/query (con dobles) responde 200",
            respuesta_rag.status_code == 200
            and respuesta_rag.json().get("refused") is False,
            f"status={respuesta_rag.status_code}",
        )
    except Exception as exc:  # noqa: BLE001 — se reporta abajo
        check("POST /api/query (con dobles) responde 200", False, str(exc))

    # --- D: llamada httpx de cliente (el camino del SDK de OpenAI) --------
    import httpx

    try:
        httpx.get(f"http://127.0.0.1:{PUERTO}/no-post", timeout=5)
    except Exception:  # noqa: BLE001 — da igual el desenlace del servidor
        pass

    # --- E: productor Celery — inyección de contexto en el mensaje -------
    from celery.signals import before_task_publish

    cabeceras_publicacion: list[dict] = []

    @before_task_publish.connect
    def _capturar_headers(headers=None, **_kwargs: object) -> None:
        if headers is not None:
            cabeceras_publicacion.append(dict(headers))

    from app.adapters.tasks import celery_app as modulo_celery

    # broker y result backend EN MEMORIA: aquí no hay Redis (el stack real
    # lo pone compose); lo que se verifica es la señal de publicación, no
    # la entrega — esa la cubre el smoke contra el stack de verdad.
    modulo_celery.celery_app.conf.broker_url = "memory://"
    modulo_celery.celery_app.conf.result_backend = "cache+memory://"
    try:
        modulo_celery.celery_app.send_task("tasks.ping")
    except Exception as exc:  # noqa: BLE001 — se reporta abajo
        print(f"(aviso: send_task con memory:// dijo: {exc})")

    # --- F: consumidor Celery — la tarea ejecutada genera su span --------
    # (eager DESPUÉS del send_task: send_task siempre publica — con eager
    # puesto, Celery avisa de que lo ignora — y aquí queremos justamente
    # el camino de publicación para capturar la inyección de cabeceras).
    modulo_celery.celery_app.conf.task_always_eager = True
    try:
        resultado_ping = modulo_celery.ping.delay()
        check(
            "tasks.ping en eager devuelve pong",
            resultado_ping.result == "pong" or resultado_ping == "pong",
            f"resultado={resultado_ping!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check("tasks.ping en eager devuelve pong", False, str(exc))

    # --- vaciar los batch processors hacia el receptor -------------------
    try:
        provider_global.force_flush(timeout_millis=5000)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — algunos providers no lo tienen
        print(f"(aviso force_flush: {exc})")

    time.sleep(0.5)
    servidor.shutdown()

    # --- veredictos sobre lo recibido -------------------------------------
    cuerpos = b"".join(ReceptorOTLP.cuerpos)
    check(
        "A. el receptor recibe POSTs en /v1/traces",
        len(ReceptorOTLP.cuerpos) > 0,
        f"{len(ReceptorOTLP.cuerpos)} lote(s)",
    )
    check(
        "B. los spans llevan service.name=faro-api",
        b"faro-api" in cuerpos,
        "",
    )
    check(
        "C. span HTTP del servidor (GET /health)",
        b"GET /health" in cuerpos,
        "",
    )
    check(
        "D. span httpx de cliente (/no-post)",
        b"/no-post" in cuerpos,
        "",
    )
    check(
        "E. el mensaje Celery lleva traceparent inyectado",
        any("traceparent" in h for h in cabeceras_publicacion),
        f"{len(cabeceras_publicacion)} publicación(es) capturada(s)",
    )
    check(
        "F. span de ejecución de la tarea (tasks.ping/run)",
        b"tasks.ping" in cuerpos,
        "",
    )
    check(
        "G. spans de dominio RAG (rag.consulta + rag.iteracion, D27)",
        b"rag.consulta" in cuerpos and b"rag.iteracion" in cuerpos,
        "deben salir por el mismo OTLP que el resto (tracer por defecto)",
    )

    print()
    fallos = 0
    for nombre, ok, detalle in RESULTADOS:
        marca = "[ OK ]" if ok else "[FAIL]"
        fallos += 0 if ok else 1
        sufijo = f" — {detalle}" if detalle and not ok else ""
        print(f"  {marca} {nombre}{sufijo}")
    print()
    print(f"RESULTADO: {len(RESULTADOS) - fallos} OK / {fallos} FALLOS")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
