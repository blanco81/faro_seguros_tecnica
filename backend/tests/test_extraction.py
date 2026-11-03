"""Tests del adapter de extracción (Fase 2): PDF con pypdf y texto plano.

El PDF de ejemplo (scripts/data/poliza-ejemplo.pdf) es un documento REAL
de dos páginas generado una vez y commiteado: los asserts de contenido por
página garantizan que la extracción preserva acentos, orden y numeración —
las tres propiedades de las que depende la literalidad de las citas (D11/D12).
"""

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.adapters.extraction.extractors import (
    PdfExtractor,
    PlainTextExtractor,
    build_default_extractor,
)
from app.config import get_settings
from app.domain.chunker import PageText
from app.ports.extraction import ExtractionError, UnsupportedMimeTypeError

#: Raíz del repo (backend/tests/test_extraction.py → dos niveles arriba).
REPO_ROOT = Path(__file__).resolve().parents[2]
PDF_EJEMPLO = REPO_ROOT / "scripts" / "data" / "poliza-ejemplo.pdf"


@pytest.fixture
def bytes_pdf_ejemplo() -> bytes:
    return PDF_EJEMPLO.read_bytes()


class TestPdfExtractor:
    def test_pdf_real_dos_paginas_en_orden(self, bytes_pdf_ejemplo):
        paginas = PdfExtractor().extract(bytes_pdf_ejemplo, "application/pdf")

        assert [p.page_number for p in paginas] == [1, 2]
        assert "inundación" in paginas[0].text
        assert "Exclusiones" in paginas[1].text

    def test_pdf_preserva_acentos_y_clausulas(self, bytes_pdf_ejemplo):
        # La literalidad de las citas (D12) empieza aquí: si la extracción
        # rompiera la codificación, el ctrl+F del evaluador no encontraría nada.
        paginas = PdfExtractor().extract(bytes_pdf_ejemplo, "application/pdf")

        assert "Cláusula 4.1" in paginas[0].text
        assert "Cláusula 7.3" in paginas[1].text

    def test_pdf_en_blanco_da_pagina_con_texto_vacio(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        buffer = io.BytesIO()
        writer.write(buffer)

        paginas = PdfExtractor().extract(buffer.getvalue(), "application/pdf")

        assert paginas == [PageText(page_number=1, text="")]

    def test_pdf_cifrado_se_rechaza_con_mensaje_accionable(self, bytes_pdf_ejemplo):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(bytes_pdf_ejemplo)))
        writer.encrypt("secreto")
        buffer = io.BytesIO()
        writer.write(buffer)

        with pytest.raises(ExtractionError) as exc:
            PdfExtractor().extract(buffer.getvalue(), "application/pdf")

        assert "cifrado" in str(exc.value)
        assert "contraseña" in str(exc.value)  # dice qué hacer el usuario

    def test_bytes_basura_se_rechazan_como_corruptos(self):
        with pytest.raises(ExtractionError) as exc:
            PdfExtractor().extract(b"esto no es un pdf", "application/pdf")

        assert "ilegible" in str(exc.value)


class TestPlainTextExtractor:
    def test_texto_plano_es_una_sola_pagina(self):
        texto = "Cláusula 4.1: cubre daños por inundación."
        paginas = PlainTextExtractor().extract(texto.encode("utf-8"), "text/plain")

        assert paginas == [PageText(page_number=1, text=texto)]

    def test_texto_plano_utf8_con_acentos(self):
        paginas = PlainTextExtractor().extract(
            "póliza · cláusula · año".encode(), "text/plain"
        )

        assert "póliza · cláusula · año" in paginas[0].text

    def test_texto_plano_latin1_se_decodifica(self):
        # Típico de .txt exportados desde Windows en español.
        paginas = PlainTextExtractor().extract(
            "daños por inundación".encode("latin-1"), "text/plain"
        )

        assert "daños por inundación" in paginas[0].text


class TestMimeDispatcherExtractor:
    def test_despacha_por_mime_al_extractor_correcto(self, bytes_pdf_ejemplo):
        dispatcher = build_default_extractor()

        por_pdf = dispatcher.extract(bytes_pdf_ejemplo, "application/pdf")
        por_texto = dispatcher.extract(b"texto de la poliza", "text/plain")

        assert len(por_pdf) == 2
        assert por_texto == [PageText(page_number=1, text="texto de la poliza")]

    def test_mime_desconocido_se_rechaza_listando_soportados(self):
        dispatcher = build_default_extractor()

        with pytest.raises(UnsupportedMimeTypeError) as exc:
            dispatcher.extract(b"...", "application/vnd.ms-excel")

        mensaje = str(exc.value)
        assert "application/vnd.ms-excel" in mensaje
        assert "application/pdf" in mensaje
        assert "text/plain" in mensaje

    def test_el_despachador_cubre_los_mimes_permitidos_por_config(self):
        # D08: la API validará contra settings.allowed_mime_types; si alguien
        # añade un tipo ahí sin extractor detrás, este test lo delata al instante.
        dispatcher = build_default_extractor()

        assert set(get_settings().allowed_mime_types) <= dispatcher.supported_mimes
