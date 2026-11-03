"""Chunking de documentos por página, con solapamiento (decisión D11).

Reglas que este módulo garantiza (y sus tests fijan):

1. Un chunk NUNCA cruza el límite de una página: la cita es
   "documento + página + fragmento", y un chunk a caballo entre dos páginas
   haría ambigua la página citada.
2. Dentro de una página se empaquetan palabras enteras hasta llenar
   ``chunk_size`` caracteres; nunca se corta una palabra por la mitad,
   porque el fragmento citado debe coincidir literalmente con el texto.
3. Cada chunk nuevo arranca con las últimas palabras del anterior que quepan
   en el presupuesto ``chunk_overlap`` (solapamiento intra-página).
4. El texto se normaliza a espacios simples: la extracción de PDF introduce
   saltos de línea arbitrarios que romperían la búsqueda literal.
"""

from dataclasses import dataclass

__all__ = [
    "DocumentChunk",
    "PageText",
    "chunk_document",
    "validate_chunk_config",
]


@dataclass(frozen=True)
class PageText:
    """Texto extraído de una página de un documento.

    ``page_number`` es 1-indexada, como en los visores de PDF.
    """

    page_number: int
    text: str


@dataclass(frozen=True)
class DocumentChunk:
    """Chunk listo para persistir: pertenece a una sola página.

    ``ordinal`` es la posición global del chunk dentro del documento
    (0-indexada); sirve para mantener el orden de inserción estable.
    """

    page_number: int
    ordinal: int
    content: str


def validate_chunk_config(chunk_size: int, chunk_overlap: int) -> None:
    """Valida la combinación de parámetros de chunking.

    Raises:
        ValueError: si el tamaño no es positivo, el solapamiento es
            negativo o el solapamiento no es estrictamente menor que el
            tamaño (con ``overlap >= size`` el solapamiento se comería
            el chunk entero).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size debe ser un entero positivo")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap no puede ser negativo")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap debe ser menor que chunk_size")


def chunk_document(
    pages: list[PageText], chunk_size: int, chunk_overlap: int
) -> list[DocumentChunk]:
    """Corta una lista de páginas en chunks con solapamiento.

    Args:
        pages: páginas del documento en orden (no se reordenan; el orden
            lo garantiza el adapter de extracción).
        chunk_size: máximo de caracteres por chunk.
        chunk_overlap: caracteres de solapamiento entre chunks de una
            misma página.

    Returns:
        Los chunks de todas las páginas, con ordinales globales secuenciales.
        Páginas vacías o en blanco no producen chunks.

    Raises:
        ValueError: si la configuración es inválida o alguna página tiene
            ``page_number`` menor que 1.
    """
    validate_chunk_config(chunk_size, chunk_overlap)

    chunks: list[DocumentChunk] = []
    for page in pages:
        if page.page_number < 1:
            raise ValueError(
                f"page_number debe ser >= 1 (recibido: {page.page_number})"
            )
        for words in _pack_words(page.text.split(), chunk_size, chunk_overlap):
            chunks.append(
                DocumentChunk(
                    page_number=page.page_number,
                    ordinal=len(chunks),
                    content=" ".join(words),
                )
            )
    return chunks


def _pack_words(
    words: list[str], chunk_size: int, chunk_overlap: int
) -> list[list[str]]:
    """Empaqueta palabras en grupos de a lo sumo ``chunk_size`` caracteres.

    Caso patológico permitido: una palabra individual más larga que
    ``chunk_size`` se emite como chunk propio (no hay forma de partirla
    sin romper la regla de palabras enteras). El bucle siempre avanza:
    cada chunk lleva al menos una palabra nueva.
    """
    packed: list[list[str]] = []
    current: list[str] = []
    length = 0  # len(" ".join(current))

    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and length + extra > chunk_size:
            packed.append(current)
            # Solapamiento: últimas palabras del chunk previo que quepan
            # en el presupuesto de caracteres del solapamiento.
            overlap: list[str] = []
            used = 0
            for previous_word in reversed(packed[-1]):
                cost = len(previous_word) + (1 if overlap else 0)
                if used + cost > chunk_overlap:
                    break
                overlap.insert(0, previous_word)
                used += cost
            if overlap == packed[-1]:
                # El chunk previo entero cabe en el solapamiento: recortar
                # para que el nuevo chunk no lo duplique por completo.
                overlap = overlap[1:]
            current = list(overlap)
            length = len(" ".join(current))
            extra = len(word) + (1 if current else 0)
            if length + extra > chunk_size:
                # Solapamiento + palabra nueva no caben: sin solapamiento.
                current, length, extra = [], 0, len(word)
        current.append(word)
        length += extra

    if current:
        packed.append(current)
    return packed
