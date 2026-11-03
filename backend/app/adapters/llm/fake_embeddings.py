"""Embeddings simulados: deterministas, sin red ni claves (D01/D14).

El enunciado exige probar ingesta y consulta con un proveedor simulado.
Este fake no es un stub que devuelve números al azar: reproduce las
propiedades algebraicas de las que dependen el umbral de rechazo (D13) y
la idempotencia (D03), para que los tests signifiquen algo:

- **texto idéntico → vector idéntico** (coseno 1.0): reintentos y
  re-subidas no cambian el embedding;
- **textos que comparten palabras → similitud intermedia** (0 < coseno < 1):
  permite construir preguntas "relevantes" que superan el umbral y
  preguntas flojas que lo incumplen, sin red;
- **textos sin palabras en común → coseno 0.0**: el rechazo también se
  puede probar de punta a punta.

Implementación: bolsa de palabras hasheada. Cada token se proyecta con
MD5 a un índice de la dimensión, se acumula y el vector se normaliza.
MD5 (no el ``hash`` nativo) para que el resultado sea estable entre
procesos y máquinas. Los tokens son palabras completas en minúsculas:
"Cláusula" y "cláusula" comparten índice, como en un embedding real.
"""

import hashlib
import math
import re
from collections.abc import Sequence

from app.ports.embeddings import EMBEDDING_DIMS, Embedding

__all__ = ["FakeEmbeddingProvider"]

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _indice(token: str, dims: int) -> int:
    """Proyección estable del token a [0, dims)."""
    # MD5 (no criptográfico): estable entre procesos y máquinas.
    digesto = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digesto[:8], "big") % dims


class FakeEmbeddingProvider:
    """EmbeddingProvider simulado con similitudes reales entre textos."""

    def __init__(self, dims: int = EMBEDDING_DIMS) -> None:
        if dims <= 0:
            raise ValueError(f"dims debe ser positivo (recibido: {dims})")
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        return [self._uno(texto) for texto in texts]

    def _uno(self, texto: str) -> Embedding:
        vector = [0.0] * self._dims
        tokens = _TOKEN_RE.findall(texto.lower())
        for token in tokens:
            vector[_indice(token, self._dims)] += 1.0
        if not any(vector):
            # Texto sin tokens (solo signos): vector unitario determinista
            # en lugar del vector cero, cuya similitud coseno es NaN en
            # pgvector y envenenaría la recuperación.
            vector[_indice(texto, self._dims)] = 1.0
        norma = math.sqrt(sum(coordenada * coordenada for coordenada in vector))
        return [coordenada / norma for coordenada in vector]
