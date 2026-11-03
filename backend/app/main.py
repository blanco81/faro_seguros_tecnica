"""Punto de entrada HTTP (adapter primario).

Rutas de negocio bajo el prefijo /api (llegan en Fases 2 y 3);
/health queda fuera para los healthchecks de Docker.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    return app


app = create_app()
