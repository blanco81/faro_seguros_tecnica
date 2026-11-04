"""Punto de entrada HTTP (adapter primario).

``/health`` queda fuera del prefijo para los healthchecks de Docker; el
negocio vive bajo ``/api`` — documentos (subida y seguimiento, Fase 2
paso 4) y la consulta con citas (Fase 3).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.http.documents import documents_router
from app.adapters.http.query import query_router
from app.config import get_settings
from app.telemetry import instrument_api


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Faro Seguros · RAG API", version="0.1.0")

    # Fase 5 (D23): spans HTTP de este proceso + inyección de contexto en
    # las tareas que publica + trazas de las llamadas al LLM (httpx). Con
    # OTEL_ENABLED=false es un no-op total (tests/CI sin collector).
    instrument_api(app)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health", tags=["infra"])
    def health() -> dict:
        return {"status": "ok"}

    # Fase 2 paso 4: subida de documentos y seguimiento del estado.
    app.include_router(documents_router)

    # Fase 3: consulta agéntica con cita verificada (D19).
    app.include_router(query_router)

    return app


app = create_app()
