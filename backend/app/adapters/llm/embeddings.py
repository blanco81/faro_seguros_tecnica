"""Adapter de embeddings vía endpoint OpenAI-compatible (decisión D10).

Un solo adapter, dos presets (Cloudflare Workers AI y OpenAI): ambos hablan
el mismo protocolo ``/embeddings``; solo cambian ``base_url`` y la clave
(el token de Cloudflare va como ``Authorization: Bearer``, que es exactamente
lo que envía el SDK de OpenAI). El preset lo elige el factory según
``EMBEDDING_PROVIDER``.

Decisiones relevantes:

- **Lotes físicos**: ``EMBEDDING_BATCH_SIZE`` textos por llamada HTTP
  (32 por defecto). Los proveedores limitan el tamaño del lote; trocear
  aquí mantiene el puerto limpio — el servicio pide "embediza estos 200
  chunks" y no sabe cómo se parte.
- **Reintentos**: el SDK reintenta errores de transporte (``max_retries``);
  los reintentos del pipeline completo los hace la tarea Celery, que es
  idempotente (borra y reinserta). Sin lógica de reintento propia: una sola
  responsabilidad.
- **Validación dura de dimensión**: si el proveedor devuelve otra dimensión
  que ``EMBEDDING_DIMS``, falla aquí con mensaje claro — un modelo mal
  configurado debe morir en la ingesta, no corromper la base vectorial.
"""

from collections.abc import Sequence

from openai import OpenAI

from app.ports.embeddings import (
    EMBEDDING_DIMS,
    Embedding,
    EmbeddingDimensionError,
)

__all__ = ["OpenAICompatEmbeddingProvider"]


class OpenAICompatEmbeddingProvider:
    """Embeddings contra cualquier endpoint OpenAI-compatible.

    Args:
        model: nombre del modelo en el proveedor (p. ej. @cf/baai/bge-m3).
        api_key: clave/bearer del proveedor.
        base_url: raíz del endpoint (None = API oficial de OpenAI).
        extra_headers: cabeceras adicionales (p. ej. cf-aig-gateway-id).
        batch_size: textos por llamada HTTP.
        timeout: segundos por llamada.
        max_retries: reintentos del SDK para errores de transporte.
        client: cliente OpenAI inyectable (tests con MockTransport, sin red).
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        batch_size: int = 32,
        timeout: float = 30.0,
        max_retries: int = 2,
        client: OpenAI | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size debe ser positivo (recibido: {batch_size})")
        self._model = model
        self._batch_size = batch_size
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            timeout=timeout,
            max_retries=max_retries,
        )

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        if not texts:
            return []

        vectors: list[Embedding] = []
        for start in range(0, len(texts), self._batch_size):
            lote = list(texts[start : start + self._batch_size])
            respuesta = self._client.embeddings.create(
                model=self._model, input=lote
            )
            # El contrato OpenAI ordena por "index"; ordenar explícitamente
            # blinda contra proveedores que devuelvan los datos sueltos.
            datos = sorted(respuesta.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in datos)

        self._validate_shapes(len(texts), vectors)
        return vectors

    def _validate_shapes(self, esperados: int, vectors: list[Embedding]) -> None:
        if len(vectors) != esperados:
            raise EmbeddingDimensionError(
                f"El proveedor devolvió {len(vectors)} embeddings para "
                f"{esperados} textos (modelo '{self._model}')."
            )
        for posicion, vector in enumerate(vectors):
            if len(vector) != EMBEDDING_DIMS:
                raise EmbeddingDimensionError(
                    f"El modelo '{self._model}' devolvió {len(vector)} "
                    f"dimensiones (posición {posicion}); el esquema exige "
                    f"{EMBEDDING_DIMS} (@cf/baai/bge-m3, decisión D10). "
                    "Corrija EMBEDDING_MODEL o migre el esquema (D02)."
                )
