"""Sesiones SQLAlchemy: una por request HTTP, una por tarea Celery.

- ``get_session`` — dependencia FastAPI: commit al salir bien, rollback
  si la request falla, close siempre. La API solo lee y crea filas;
  el commit por request evita perder el alta del documento.
- ``session_scope`` — contexto transaccional del worker: la tarea corre
  entera dentro y el commit ocurre solo si el pipeline terminó bien;
  cualquier excepción deja la base como estaba (D03: sin estado parcial).
  ``commit_on`` refina eso para los fallos de NEGOCIO: la excepción que
  se liste ahí también completa la transacción, porque el ``failed``
  que dejó marcado el servicio es el RESULTADO correcto de la tarea
  y debe sobrevivir al rollback (D16).

El motor se crea LAZY (``get_engine``): importar este módulo no debe
conectar ni exigir driver — los tests de la suite importan módulos que
cuelgan de aquí sin tener PostgreSQL corriendo (CI sin red, D01).
``pool_pre_ping`` descarta conexiones muertas tras reinicios de la base
(la ventana de 72 h incluye redeploys del stack).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

__all__ = ["get_engine", "get_session", "get_session_factory", "session_scope"]


@lru_cache
def get_engine():
    """Motor compartido por proceso (API o worker), creado al primer uso."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    # expire_on_commit=False: la tarea construye DocumentInfo DESPUÉS del
    # flush y a veces tras el commit (retorno del worker); sin esto, cada
    # acceso a un atributo dispararía un SELECT extra (o explotaría con
    # la sesión ya cerrada).
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Dependencia FastAPI: una sesión por request, commit al finalizar."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope(
    *, commit_on: tuple[type[BaseException], ...] = ()
) -> Iterator[Session]:
    """Contexto transaccional del worker: commit o rollback, nunca ambos.

    Args:
        commit_on: excepciones que también hacen commit. Caso de uso (D16):
            ``FatalIngestionError`` marca el documento ``failed`` ANTES de
            lanzarse (contrato de ``run_ingestion``); revertir esa marca la
            perdería y el documento quedaría eternamente ``processing``.
            Las transitorias siguen revirtiendo (D03: sin estado parcial).
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException as exc:
        if commit_on and isinstance(exc, commit_on):
            session.commit()
        else:
            session.rollback()
        raise
    finally:
        session.close()
