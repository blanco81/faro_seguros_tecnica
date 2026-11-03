"""Puerto TextExtractor: bytes crudos → páginas de texto.

Contrato de la Fase 2 (ingesta). La extracción produce una lista de
``PageText`` (dominio) en el MISMO orden en que aparecen en el documento:
el chunker confía en ese orden para construir los ordinales globales
(decisión D11). Los ``page_number`` son 1-indexados, como los visores.

Los extractores son locales (pypdf, decodificación de texto): no usan red
ni claves, así que NO existe un adapter fake de este puerto — el "proveedor
simulado" que exige el enunciado aplica a LLM y embeddings (D01).
"""

from typing import Protocol, runtime_checkable

from app.domain.chunker import PageText

__all__ = [
    "ExtractionError",
    "TextExtractor",
    "UnsupportedMimeTypeError",
]


class UnsupportedMimeTypeError(ValueError):
    """El MIME no tiene extractor asociado.

    La API valida el tipo en la subida (D08) y responde 415 antes de
    llegar aquí; este error es la defensa del núcleo si algo se cuela.
    """


class ExtractionError(ValueError):
    """El contenido no pudo extraerse (PDF cifrado, corrupto, ilegible).

    El mensaje es la razón de fallo que verá el usuario en el estado del
    documento: debe ser accionable, no un traceback.
    """


@runtime_checkable
class TextExtractor(Protocol):
    """Extrae el texto de un documento, página a página."""

    def extract(self, data: bytes, mime_type: str) -> list[PageText]:
        """Convierte ``data`` (bytes crudos) en páginas de texto.

        Args:
            data: contenido binario del documento tal como se subió.
            mime_type: tipo MIME declarado en la subida.

        Returns:
            Una página por página física del documento, en orden, con
            ``page_number`` 1-indexado. Una página sin texto extraíble
            entra con ``text`` vacío (el chunker la salta); las páginas
            en blanco NO se omiten de la numeración.

        Raises:
            UnsupportedMimeTypeError: sin extractor para ese MIME.
            ExtractionError: contenido ilegible (cifrado, corrupto...).
        """
        ...
