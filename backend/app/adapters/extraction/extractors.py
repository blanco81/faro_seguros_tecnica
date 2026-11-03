"""Adapters de extracción: PDF (pypdf) y texto plano, con despacho por MIME.

Los dos son locales (sin red ni claves). Puntos que fijan los tests:

- **Orden y numeración**: una ``PageText`` por página física, en orden,
  ``page_number`` 1-indexado — el chunker construye los ordinales globales
  sobre esa garantía (D11).
- **Espacios**: pypdf devuelve saltos de línea arbitrarios; NO se
  normalizan aquí — lo hace el chunker (D11), que es quien garantiza la
  literalidad de las citas.
- **Fallos con mensaje accionable**: PDF cifrado, corrupto o página
  ilegible se reportan como ``ExtractionError`` con la razón en texto
  claro: ese string es el ``failure_reason`` que verá el usuario.
"""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PyPdfError

from app.domain.chunker import PageText
from app.ports.extraction import (
    ExtractionError,
    TextExtractor,
    UnsupportedMimeTypeError,
)

__all__ = [
    "MimeDispatcherExtractor",
    "PdfExtractor",
    "PlainTextExtractor",
]


class PdfExtractor:
    """Extrae texto de PDFs página a página con pypdf."""

    def extract(self, data: bytes, mime_type: str) -> list[PageText]:
        try:
            reader = PdfReader(io.BytesIO(data))
        except PdfReadError as exc:
            raise ExtractionError(
                f"PDF ilegible o corrupto: {exc}. Verifique que el archivo "
                "sea un PDF válido."
            ) from exc

        if reader.is_encrypted:
            # PDFs con contraseña de propietario pero de lectura libre usan
            # cadena vacía; los que exigen contraseña real se rechazan.
            try:
                decrypted = reader.decrypt("")
            except (PyPdfError, ValueError):
                # PdfReadError, DependencyError (AES sin lib crypto), etc.:
                # cualquier fallo de pypdf al descifrar cuenta como rechazo.
                decrypted = False
            if not decrypted:
                raise ExtractionError(
                    "El PDF está cifrado: la extracción de texto requiere un "
                    "archivo sin contraseña. Descárguelo descifrado y "
                    "vuelva a subirlo."
                )

        pages: list[PageText] = []
        try:
            iterable = enumerate(reader.pages, start=1)
            for index, page in iterable:
                try:
                    text = page.extract_text() or ""
                except Exception as exc:  # una sola página mala no tira el documento
                    raise ExtractionError(
                        f"No se pudo extraer el texto de la página {index}: {exc}"
                    ) from exc
                pages.append(PageText(page_number=index, text=text))
        except PdfReadError as exc:
            # xref/tabla de páginas corruptas que no explotaron al construir
            raise ExtractionError(
                f"PDF ilegible o corrupto al leer las páginas: {exc}. "
                "Verifique que el archivo sea un PDF válido."
            ) from exc
        return pages


class PlainTextExtractor:
    """Texto plano: el archivo entero es la página 1.

    Las pólizas en texto llegan en UTF-8 o Latin-1 (típico de Windows en
    español); se intenta UTF-8 primero y Latin-1 como respaldo — Latin-1
    nunca falla (mapea los 256 bytes), así que la decodificación es total.
    """

    def extract(self, data: bytes, mime_type: str) -> list[PageText]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return [PageText(page_number=1, text=text)]


class MimeDispatcherExtractor:
    """Elige el extractor según el MIME declarado en la subida.

    La API valida el tipo contra la lista permitida (D08) antes de llegar
    aquí; este despachador es la frontera interna: si un MIME sin adapter
    se cuela, falla con ``UnsupportedMimeTypeError`` en vez de adivinar.
    """

    def __init__(self, extractors: dict[str, TextExtractor]) -> None:
        self._extractors = dict(extractors)

    @property
    def supported_mimes(self) -> frozenset[str]:
        """MIMEs con extractor disponible (para validación y tests)."""
        return frozenset(self._extractors)

    def extract(self, data: bytes, mime_type: str) -> list[PageText]:
        extractor = self._extractors.get(mime_type)
        if extractor is None:
            soportados = ", ".join(sorted(self._extractors))
            raise UnsupportedMimeTypeError(
                f"Tipo no soportado para extracción: '{mime_type}' "
                f"(soportados: {soportados})"
            )
        return extractor.extract(data, mime_type)


def build_default_extractor() -> MimeDispatcherExtractor:
    """Despachador con los extractores del MVP (PDF y texto plano)."""
    return MimeDispatcherExtractor(
        {
            "application/pdf": PdfExtractor(),
            "text/plain": PlainTextExtractor(),
        }
    )
