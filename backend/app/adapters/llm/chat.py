"""Adapter de chat vía endpoint OpenAI-compatible (D10, Fase 3).

Gemelo del adapter de embeddings (``adapters/llm/embeddings.py``): mismo
preset doble (Cloudflare Workers AI / OpenAI), mismo protocolo — solo
cambia ``/chat/completions`` por ``/embeddings``. Un solo estilo de
adapter para todo el proveedor.

``temperature=0.0`` es parte del contrato del puerto (ver ``ports/llm.py``):
la consulta RAG debe ser reproducible. Un borrador creativo del LLM
parafrasearía la cláusula y la verificación verbatim (D12/D19) lo
rechazaría — creatividad y citas literales son incompatibles.
"""

from openai import OpenAI

__all__ = ["OpenAICompatChatProvider"]


class OpenAICompatChatProvider:
    """Chat contra cualquier endpoint OpenAI-compatible.

    Args:
        model: nombre del modelo en el proveedor (llama-3.3-70b en D10).
        api_key: clave/bearer del proveedor.
        base_url: raíz del endpoint (None = API oficial de OpenAI).
        extra_headers: cabeceras adicionales (p. ej. cf-aig-gateway-id).
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
        timeout: float = 30.0,
        max_retries: int = 2,
        client: OpenAI | None = None,
    ) -> None:
        self._model = model
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            timeout=timeout,
            max_retries=max_retries,
        )

    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Un prompt → una respuesta (temperatura 0, sin streaming)."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        respuesta = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.0,
        )
        return (respuesta.choices[0].message.content or "").strip()
