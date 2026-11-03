"""Puerto LLMProvider: chat de texto para la consulta (Fase 3).

La Fase 2 necesitaba embeddings; la Fase 3 además GENERA texto (planificar
sub-preguntas, redactar la respuesta con cita, refinar la consulta). Este
puerto es ese contrato: un prompt entra, un texto sale.

El puerto es deliberadamente mínimo — ni esquemas de mensajes, ni
herramientas, ni streaming: los servicios componen prompts y parsean
respuestas (JSON) por su cuenta; el adapter solo transporte. La
temperatura la fija el adapter en ``0.0``: la consulta RAG debe ser
reproducible, no creativa (una cita literal no admite creatividad).

El adapter fake (``adapters/llm/fake_llm.py``) implementa este puerto sin
red ni claves (proveedor simulado del enunciado, D01/D14) y produce
respuestas ÚTILES: extrae la cita del propio contexto que el servicio le
envía, para que el bucle agéntico corra de punta a punta en la suite.
"""

from typing import Protocol, runtime_checkable

__all__ = ["LLMProvider"]


@runtime_checkable
class LLMProvider(Protocol):
    """Conversa con un LLM instruct: un prompt → una respuesta."""

    def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Envía ``prompt`` (con ``system`` opcional) y devuelve la respuesta.

        Args:
            prompt: instrucción completa del usuario (el servicio ya
                incluyó el contexto que considera necesario).
            system: rol/restricciones del asistente, si aplica.

        Returns:
            El texto de respuesta, sin espacios de sobra. Los servicios
            que esperen JSON deben parsearlo y degradar con gracia si el
            modelo se desvía (el puerto no promete formato).
        """
        ...
