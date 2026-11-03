"""Punto de entrada HTTP (adapter primario).

``/health`` queda fuera del prefijo para los healthchecks de Docker; el
negocio vive bajo ``/api`` — documentos (subida y seguimiento, Fase 2
paso 4) y la consulta con citas (Fase 3).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.http.documents import documents_router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Faro Seguros · RAG API", version="0.1.0")

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

    return app


app = create_app()
