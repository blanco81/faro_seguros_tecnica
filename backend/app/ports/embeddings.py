"""Puerto EmbeddingProvider: textos → vectores.

Contrato de la Fase 2 (ingesta) que reutiliza la Fase 3 (consulta):

- ``embed`` recibe un lote de textos y devuelve UN vector por texto,
  en el MISMO orden; jamás reordena ni deduplica.
- Cada vector tiene exactamente ``EMBEDDING_DIMS`` dimensiones (1024:
  @cf/baai/bge-m3, decisión D10, que fija ``vector(1024)`` en el
  esquema — cambiar la dimensión exige migración explícita, D02).
- La llamada es idempotente: mismos textos → mismos vectores. Es lo
  que permite reintentar la tarea Celery sin corromper nada.

El adapter fake (``adapters/llm/fake_embeddings.py``) implementa este
puerto sin red ni claves: es el "proveedor simulado" del enunciado y
corre en toda la suite (D01/D14).
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

__all__ = [
    "EMBEDDING_DIMS",
    "Embedding",
    "EmbeddingDimensionError",
    "EmbeddingProvider",
]

#: Dimensión FIJA de los embeddings del proyecto (D10: @cf/baai/bge-m3).
#: Única fuente de verdad: el esquema (adapters/db/models.py) y los
#: adapters de proveedor la importan de aquí.
EMBEDDING_DIMS = 1024

#: Un embedding: 1024 flotantes (no necesariamente normalizados — el
#: proveedor decide; la distancia coseno de pgvector no lo exige).
type Embedding = list[float]


class EmbeddingDimensionError(ValueError):
    """El proveedor devolvió vectores con otra dimensión a la del esquema.

    Casi siempre significa que ``EMBEDDING_MODEL`` apunta a un modelo
    distinto a @cf/baai/bge-m3: la base espera vector(1024) y hay que
    migrar el esquema (D02) o volver al modelo acordado (D10).
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Calcula embeddings para lotes de textos."""

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Embediza ``texts`` en una sola llamada lógica.

        Args:
            texts: textos a embedizar (puede ser grande: el adapter
                decide cómo trocearlo en lotes físicos).

        Returns:
            Un vector por texto de entrada, en el mismo orden, cada
            uno con ``EMBEDDING_DIMS`` dimensiones.

        Raises:
            EmbeddingDimensionError: el proveedor devolvió otra
                dimensión que la del esquema (modelo mal configurado).
        """
        ...
