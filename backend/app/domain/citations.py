"""Construcción de citas verificables (decisión D12).

Contrato de literalidad: el fragmento de una cita es SIEMPRE un substring
exacto del contenido del chunk almacenado — nunca una paráfrasis ni una
reescritura del LLM. Así, quien evalúa puede abrir el documento, ir a la
página citada y encontrar ese texto con un simple ctrl+F.

Si el chunk supera el máximo de caracteres de la cita, se recorta por
palabra completa (nunca a medias), pero lo citado sigue siendo literal.
Los puntos suspensivos de presentación los añade la UI al renderizar;
el dominio los omite para no romper el substring.
"""

from dataclasses import dataclass

__all__ = [
    "DEFAULT_MAX_QUOTE_CHARS",
    "Citation",
    "build_citation",
    "quote_is_verbatim",
]

#: Límite por defecto del fragmento citado (caracteres).
DEFAULT_MAX_QUOTE_CHARS = 220


@dataclass(frozen=True)
class Citation:
    """Cita verificable: documento, página y fragmento literal."""

    document: str
    page: int
    quote: str


def build_citation(
    document: str,
    page: int,
    chunk_content: str,
    *,
    max_quote_chars: int = DEFAULT_MAX_QUOTE_CHARS,
) -> Citation:
    """Construye la cita de un chunk recuperado.

    Args:
        document: nombre del archivo del documento.
        page: página 1-indexada donde vive el chunk.
        chunk_content: contenido del chunk tal como está almacenado.
        max_quote_chars: máximo de caracteres del fragmento citado.

    Raises:
        ValueError: si el documento está vacío, la página es menor que 1
            o el máximo de caracteres no es positivo.
    """
    if not document:
        raise ValueError("document no puede estar vacío")
    if page < 1:
        raise ValueError(f"page debe ser >= 1 (recibido: {page})")
    if max_quote_chars <= 0:
        raise ValueError("max_quote_chars debe ser positivo")

    quote = _verbatim_prefix(chunk_content.strip(), max_quote_chars)
    return Citation(document=document, page=page, quote=quote)


def quote_is_verbatim(citation: Citation, chunk_content: str) -> bool:
    """True si el fragmento de la cita aparece literalmente en el chunk.

    Es el contrato D12 expresado como código: lo usan los tests y puede
    usarlo la API para autoverificar antes de responder.
    """
    return citation.quote in chunk_content


def _verbatim_prefix(text: str, limit: int) -> str:
    """Primeros ``limit`` caracteres de ``text``, recortados a palabra completa.

    El resultado siempre es un substring exacto de ``text``. Si la primera
    palabra ya excede el límite, se devuelve tal cual (substring igualmente).
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut
