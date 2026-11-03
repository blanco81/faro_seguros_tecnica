"""Tests del servicio de ingesta (Fase 2 paso 2): registro y pipeline.

Se prueba el CASO DE USO completo con adapters reales locales
(extractores pypdf/texto plano) y el embedder fake (D01): extracción →
chunking → embeddings → persistencia vía el puerto DocumentRepository
(con el doble en memoria de tests/fakes.py).

Lo que estos tests fijan (los criterios B2/B3 del plan a nivel unitario):

- D03 (subida): mismo contenido → mismo documento, sin reprocesar.
- D03 (reintento): reejecutar el pipeline no duplica chunks; un error
  transitorio deja ``processing`` y la reentrada recupera.
- D04: las transiciones las valida el dominio; reprocesar desde
  ``ready`` o ``failed`` es legal.
- Faltales vs transitorios: PDF corrupto y sin texto marcan ``failed``
  con razón accionable; los errores de red se propagan sin marcar.
- Nunca se confía en el proveedor: embeddings con dimensión o cantidad
  equivocadas se rechazan antes de tocar la base (D02/D10).
"""

import hashlib
import io
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.adapters.extraction.extractors import build_default_extractor
from app.adapters.llm.fake_embeddings import FakeEmbeddingProvider
from app.domain.chunker import chunk_document
from app.domain.document import DocumentStatus
from app.ports.embeddings import EMBEDDING_DIMS, EmbeddingDimensionError
from app.services.ingestion import (
    FatalIngestionError,
    register_document,
    run_ingestion,
)
from tests.fakes import FakeDocumentRepository

#: Raíz del repo (backend/tests/test_ingestion.py → dos niveles arriba).
REPO_ROOT = Path(__file__).resolve().parents[2]
PDF_EJEMPLO = REPO_ROOT / "scripts" / "data" / "poliza-ejemplo.pdf"

#: Texto de póliza repetido (×6): con chunk 120+30 produce 8 chunks,
#: como el check 8 del smoke (mismo material, otra capa).
TEXTO_POLIZA = (
    "La póliza cubre daños por inundación en el hogar asegurado según "
    "los límites y condiciones descritos en la cláusula 4.1 "
) * 6

CHUNK = {"chunk_size": 120, "chunk_overlap": 30}


@pytest.fixture
def repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def extractor():
    return build_default_extractor()


@pytest.fixture
def embedder() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


def _registrar_texto(
    repo: FakeDocumentRepository, data: bytes | None = None
) -> tuple:
    """Registra un texto plano de una página con el texto de póliza."""
    return register_document(
        repo,
        filename="poliza.txt",
        mime_type="text/plain",
        data=data if data is not None else TEXTO_POLIZA.encode("utf-8"),
    )


def _ingerir(repo, extractor, embedder, documento, data, **kwargs):
    """run_ingestion con los defaults de chunk de estos tests."""
    opciones = {**CHUNK, **kwargs}
    return run_ingestion(
        repo, extractor, embedder, documento.id, data, **opciones
    )


class TestRegisterDocument:
    def test_registra_nuevo_en_pendiente_con_hash_sha256(self, repo):
        data = TEXTO_POLIZA.encode("utf-8")

        documento, creado = _registrar_texto(repo, data)

        assert creado is True
        assert documento.status == DocumentStatus.PENDING
        assert documento.content_hash == hashlib.sha256(data).hexdigest()
        assert documento.size_bytes == len(data)
        assert documento.page_count is None

    def test_resubir_mismo_contenido_devuelve_el_existente(self, repo):
        primera, creado1 = _registrar_texto(repo)
        segunda, creado2 = _registrar_texto(repo)

        assert creado1 is True and creado2 is False
        assert segunda.id == primera.id
        assert len(repo.list_documents()) == 1

    def test_resubir_devuelve_el_documento_aunque_este_fallido(
        self, repo, extractor, embedder
    ):
        # D03: la deduplicación es por CONTENIDO, no por estado. Un
        # documento fallido re-subido no se reprocesa: devuelve el
        # registro con su motivo para que el analista decida.
        data = b"esto no es un pdf"
        documento, _ = register_document(
            repo, filename="roto.pdf", mime_type="application/pdf", data=data
        )
        with pytest.raises(FatalIngestionError):
            _ingerir(repo, extractor, embedder, documento, data,
                     chunk_size=800, chunk_overlap=120)
        assert repo.get(documento.id).status == DocumentStatus.FAILED

        reenviado, creado = register_document(
            repo, filename="roto.pdf", mime_type="application/pdf", data=data
        )

        assert creado is False
        assert reenviado.id == documento.id
        assert reenviado.status == DocumentStatus.FAILED
        assert len(repo.list_documents()) == 1

    def test_contenido_distinto_crea_documento_nuevo(self, repo):
        primero, _ = _registrar_texto(repo)
        otro, creado = _registrar_texto(
            repo, (TEXTO_POLIZA + "Anexo: terremoto.").encode("utf-8")
        )

        assert creado is True
        assert otro.id != primero.id
        assert otro.content_hash != primero.content_hash
        assert len(repo.list_documents()) == 2

    def test_el_contenido_crudo_queda_guardado_con_el_alta(self, repo):
        # D15: la tarea Celery solo recibe el document_id — los bytes
        # tienen que estar ya en la base, en la MISMA transacción.
        data = TEXTO_POLIZA.encode("utf-8")
        documento, _ = _registrar_texto(repo, data)

        assert repo.get_content(documento.id) == data

    def test_resubir_no_reescribe_el_contenido(self, repo):
        # Relación 1:1 (D15): si el camino de deduplicación intentara un
        # segundo save_content, el doble lo castiga con ValueError.
        primera, _ = _registrar_texto(repo)
        segunda, creado = _registrar_texto(repo)

        assert creado is False
        assert repo.get_content(primera.id) == repo.get_content(segunda.id)
        assert repo.get_content(primera.id) is not None

    def test_contenido_solo_para_documentos_registrados(self, repo):
        # Un UUID cualquiera (tarea con documento borrado): nada que leer.
        assert repo.get_content(uuid.uuid4()) is None

    def test_filename_se_trunca_al_limite_del_esquema(self, repo):
        documento, _ = register_document(
            repo, filename="x" * 300, mime_type="text/plain", data=b"contenido"
        )

        assert len(documento.filename) == 255  # String(255) en la migración

    def test_filename_vacio_se_rechaza(self, repo):
        with pytest.raises(ValueError, match="filename"):
            register_document(
                repo, filename="", mime_type="text/plain", data=b"data"
            )


class TestRunIngestion:
    def test_texto_plano_queda_listo_con_chunks_y_embeddings(
        self, repo, extractor, embedder
    ):
        documento, _ = _registrar_texto(repo)

        resultado = _ingerir(repo, extractor, embedder, documento,
                             TEXTO_POLIZA.encode("utf-8"))

        esperados = chunk_document(
            extractor.extract(TEXTO_POLIZA.encode("utf-8"), "text/plain"),
            CHUNK["chunk_size"],
            CHUNK["chunk_overlap"],
        )
        assert resultado.status == DocumentStatus.READY
        assert resultado.page_count == 1
        assert resultado.chunk_count == len(esperados) >= 3
        assert resultado.failure_reason is None

    def test_los_chunks_persistidos_reflejan_el_chunker(
        self, repo, extractor, embedder
    ):
        documento, _ = _registrar_texto(repo)
        _ingerir(repo, extractor, embedder, documento, TEXTO_POLIZA.encode("utf-8"))

        persistidos = repo.chunks_of(documento.id)
        esperados = chunk_document(
            extractor.extract(TEXTO_POLIZA.encode("utf-8"), "text/plain"),
            CHUNK["chunk_size"],
            CHUNK["chunk_overlap"],
        )
        assert [c.ordinal for c in persistidos] == list(range(len(esperados)))
        assert [c.page_number for c in persistidos] == [
            e.page_number for e in esperados
        ]
        assert [c.content for c in persistidos] == [e.content for e in esperados]
        assert all(len(c.embedding) == EMBEDDING_DIMS for c in persistidos)

    def test_reejecucion_no_duplica_chunks(self, repo, extractor, embedder):
        # D03 (reintento): correr el pipeline dos veces deja exactamente
        # los mismos chunks — replace_chunks sustituye, no concatena.
        documento, _ = _registrar_texto(repo)
        data = TEXTO_POLIZA.encode("utf-8")

        primera = _ingerir(repo, extractor, embedder, documento, data)
        segunda = _ingerir(repo, extractor, embedder, documento, data)

        assert segunda.chunk_count == primera.chunk_count
        assert len(repo.list_documents()) == 1  # tampoco duplicó documentos
        assert segunda.status == DocumentStatus.READY

    def test_pdf_real_de_dos_paginas_queda_listo(self, repo, extractor, embedder):
        # Criterio B3 del plan en versión unitaria: un PDF REAL queda
        # «listo» con chunks persistidos.
        data = PDF_EJEMPLO.read_bytes()
        documento, _ = register_document(
            repo, filename="poliza-ejemplo.pdf",
            mime_type="application/pdf", data=data,
        )

        resultado = _ingerir(repo, extractor, embedder, documento, data,
                             chunk_size=800, chunk_overlap=120)

        assert resultado.status == DocumentStatus.READY
        assert resultado.page_count == 2
        assert resultado.chunk_count >= 1

    def test_pdf_corrupto_marca_failed_con_razon_accionable(
        self, repo, extractor, embedder
    ):
        documento, _ = register_document(
            repo, filename="roto.pdf", mime_type="application/pdf",
            data=b"esto no es un pdf",
        )

        with pytest.raises(FatalIngestionError) as exc:
            _ingerir(repo, extractor, embedder, documento,
                     b"esto no es un pdf", chunk_size=800, chunk_overlap=120)

        assert "ilegible" in str(exc.value)
        fallado = repo.get(documento.id)
        assert fallado.status == DocumentStatus.FAILED
        assert "ilegible" in (fallado.failure_reason or "")

    def test_pdf_sin_texto_marca_failed_con_sugerencia_ocr(
        self, repo, extractor, embedder
    ):
        # PDF de páginas en blanco: extracción OK, chunking → 0 chunks.
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
        documento, _ = register_document(
            repo, filename="escaneo.pdf", mime_type="application/pdf", data=data
        )

        with pytest.raises(FatalIngestionError) as exc:
            _ingerir(repo, extractor, embedder, documento, data,
                     chunk_size=800, chunk_overlap=120)

        assert "texto extraíble" in str(exc.value)
        assert "OCR" in (repo.get(documento.id).failure_reason or "")

    def test_documento_inexistente_es_fatal_sin_estado(self, repo, extractor, embedder):
        fantasma = SimpleNamespace(id=uuid.uuid4())

        with pytest.raises(FatalIngestionError, match="no existe"):
            _ingerir(repo, extractor, embedder, fantasma, b"data")

    def test_error_transitorio_no_marca_failed_y_la_reentrada_recupera(
        self, repo, extractor
    ):
        # El guion del reintento (B3): el proveedor falla una vez con un
        # 503 → la excepción se PROPAGA (no es fatal: no marca failed) y
        # el documento queda «processing». Celery reintenta; la segunda
        # pasada reentra por processing → … → ready sin duplicar nada.

        class _EmbedderCon503:
            """Simula un proveedor que falla la primera llamada."""

            def __init__(self) -> None:
                self._inner = FakeEmbeddingProvider()
                self.ya_fallo = False

            def embed(self, texts):
                if not self.ya_fallo:
                    self.ya_fallo = True
                    raise RuntimeError("503 Service Unavailable")
                return self._inner.embed(texts)

        documento, _ = _registrar_texto(repo)
        embedder = _EmbedderCon503()
        data = TEXTO_POLIZA.encode("utf-8")

        with pytest.raises(RuntimeError, match="503"):
            _ingerir(repo, extractor, embedder, documento, data)

        intermedio = repo.get(documento.id)
        assert intermedio.status == DocumentStatus.PROCESSING
        assert intermedio.failure_reason is None

        final = _ingerir(repo, extractor, embedder, documento, data)
        assert final.status == DocumentStatus.READY
        assert final.chunk_count >= 3

    def test_reproceso_desde_ready_y_desde_failed_es_valido(
        self, repo, extractor, embedder
    ):
        # D04: ready→processing (reproceso) y failed→processing
        # (reintento) son transiciones legales del dominio.
        documento, _ = _registrar_texto(repo)
        data = TEXTO_POLIZA.encode("utf-8")

        listo = _ingerir(repo, extractor, embedder, documento, data)
        reprocesado = _ingerir(repo, extractor, embedder, documento, data)
        assert listo.status == reprocesado.status == DocumentStatus.READY

        # Reintento desde failed (D04): lo que hace la tarea al agotar los
        # reintentos de un error transitorio es marcar failed; si alguien
        # re-encola la tarea, la reentrada debe poder recuperarlo.
        repo.set_status(
            documento.id,
            DocumentStatus.FAILED,
            failure_reason="reintentos agotados: 503 del proveedor",
        )
        corregido = _ingerir(repo, extractor, embedder, documento, data)
        assert corregido.status == DocumentStatus.READY
        assert corregido.failure_reason is None  # se limpió el motivo


class TestProveedoresDesconfiables:
    """Nunca se confía en el proveedor: la base es vector(1024) (D02/D10)."""

    def test_dimension_equivocada_marca_failed(self, repo, extractor):
        class _EmbedderDimensionRota:
            def embed(self, texts):
                return [[0.5, 0.5] for _ in texts]  # 2 dims, no 1024

        documento, _ = _registrar_texto(repo)

        with pytest.raises(FatalIngestionError, match="dimensión"):
            _ingerir(repo, extractor, _EmbedderDimensionRota(), documento,
                     TEXTO_POLIZA.encode("utf-8"))

        assert repo.get(documento.id).status == DocumentStatus.FAILED

    def test_cantidad_equivocada_marca_failed(self, repo, extractor):
        class _EmbedderCantidadRota:
            def embed(self, texts):
                # Un embedding menos que textos: mapeo parcial.
                return [[0.0] * EMBEDDING_DIMS for _ in texts[:-1]]

        documento, _ = _registrar_texto(repo)

        with pytest.raises(FatalIngestionError, match="parcial"):
            _ingerir(repo, extractor, _EmbedderCantidadRota(), documento,
                     TEXTO_POLIZA.encode("utf-8"))

        assert repo.get(documento.id).status == DocumentStatus.FAILED

    def test_error_de_dimension_del_adapter_marca_failed(self, repo, extractor):
        # El adapter real (OpenAI-compatible) lanza EmbeddingDimensionError
        # con un modelo mal configurado; el servicio lo traduce a failed.
        class _EmbedderQueLanza:
            def embed(self, texts):
                raise EmbeddingDimensionError(
                    "El modelo 'X' devolvió 512 dimensiones"
                )

        documento, _ = _registrar_texto(repo)

        with pytest.raises(FatalIngestionError, match="512"):
            _ingerir(repo, extractor, _EmbedderQueLanza(), documento,
                     TEXTO_POLIZA.encode("utf-8"))

        fallado = repo.get(documento.id)
        assert fallado.status == DocumentStatus.FAILED
        assert "512" in (fallado.failure_reason or "")
