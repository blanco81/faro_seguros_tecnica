"""Tests de la API HTTP de documentos (Fase 2 paso 4).

Sin base ni broker (D01): las dos dependencias cableables de la ruta se
sustituyen con dobles — la unidad de trabajo devuelve el ``FakeDocumentRepository``
y el dispatcher registra los despachos en una lista. Lo que queda probado
es el CONTRATO HTTP de verdad (FastAPI en modo real): validaciones D08,
códigos 201/200/413/415/422/404, cabecera ``Location``, serialización y
el orden registro → despacho (D17).

El cableado contra PostgreSQL/Redis lo prueba el smoke de la Fase 2 paso 5.

Pinchazos deliberados de este archivo:

- La re-subida NO despacha (D03): el worker no debe reprocesar lo ya
  registrado — y el estado que se devuelve es el que YA tiene el documento.
- El contenido queda guardado junto al alta (D15): es lo que leerá la tarea.
- El límite de tamaño se compara con el Settings de la REQUEST (inyectable),
  no con un global: por eso el test puede usar un límite de 1 MB.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.adapters.http.documents import (
    get_ingest_dispatcher,
    get_repository_uow,
)
from app.config import Settings, get_settings
from app.main import app
from tests.fakes import FakeDocumentRepository

TEXTO_POLIZA = (
    "La póliza cubre daños por inundación en el hogar asegurado según "
    "los límites y condiciones descritos en la cláusula 4.1 "
) * 6


# --- dobles y fixtures -------------------------------------------------------


@contextmanager
def _uow_falso(repo: FakeDocumentRepository) -> Iterator[FakeDocumentRepository]:
    """Doble de la unidad de trabajo: sin base, el repo falso directo."""
    yield repo


@pytest.fixture
def repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def despachos() -> list[uuid.UUID]:
    """Ids que llegaron al 'broker' durante el test."""
    return []


@pytest.fixture
def cliente(repo, despachos):
    """Fábrica de TestClient con los dobles instalados.

    Acepta ``settings`` para probar los límites con valores pequeños
    (``max_upload_mb=1``) sin tocar el ``.env`` ni el entorno.
    """

    def _crear(settings: Settings | None = None) -> TestClient:
        app.dependency_overrides[get_repository_uow] = lambda: lambda: _uow_falso(repo)

        def _despachar(document_id: uuid.UUID) -> None:
            despachos.append(document_id)

        app.dependency_overrides[get_ingest_dispatcher] = lambda: _despachar
        if settings is not None:
            app.dependency_overrides[get_settings] = lambda: settings
        return TestClient(app)

    yield _crear
    app.dependency_overrides.clear()


def _subir(client: TestClient, *, nombre="poliza.txt", data: bytes, mime="text/plain"):
    return client.post("/api/documents", files={"file": (nombre, data, mime)})


# --- POST /api/documents ------------------------------------------------------


class TestSubida:
    def test_txt_nuevo_registra_pendiente_y_despacha(self, cliente, repo, despachos):
        client = cliente()
        data = TEXTO_POLIZA.encode("utf-8")

        respuesta = _subir(client, data=data)

        assert respuesta.status_code == 201, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["status"] == "pending"
        assert cuerpo["filename"] == "poliza.txt"
        assert cuerpo["mime_type"] == "text/plain"
        assert cuerpo["size_bytes"] == len(data)
        assert cuerpo["page_count"] is None
        assert cuerpo["failure_reason"] is None
        assert cuerpo["chunk_count"] == 0
        assert respuesta.headers["location"] == f"/api/documents/{cuerpo['id']}"

        # La tarea se despacha con el id registrado, y el contenido quedó
        # guardado junto al alta (D15): la tarea solo necesita el id.
        document_id = uuid.UUID(cuerpo["id"])
        assert despachos == [document_id]
        assert repo.get_content(document_id) == data

    def test_resubida_mismo_contenido_no_reprocesa(self, cliente, despachos):
        client = cliente()
        data = TEXTO_POLIZA.encode("utf-8")

        primera = _subir(client, data=data)
        segunda = _subir(client, nombre="otro-nombre.txt", data=data)

        assert primera.status_code == 201
        assert segunda.status_code == 200  # ya existía (D03)
        assert segunda.json()["id"] == primera.json()["id"]
        assert len(despachos) == 1  # una sola tarea, la de la primera subida

    def test_pdf_se_acepta_sin_parsearlo(self, cliente, despachos):
        # La API valida el tipo DECLARADO (D08); interpretar los bytes es
        # trabajo del worker — un PDF corrupto fallará ahí con su motivo.
        client = cliente()
        respuesta = _subir(
            client, nombre="poliza.pdf", data=b"%PDF-1.4 fake", mime="application/pdf"
        )

        assert respuesta.status_code == 201
        assert respuesta.json()["mime_type"] == "application/pdf"
        assert len(despachos) == 1

    def test_mime_con_charset_se_normaliza(self, cliente, repo):
        client = cliente()
        respuesta = _subir(client, data=b"hola", mime="text/plain; charset=utf-8")

        assert respuesta.status_code == 201
        assert respuesta.json()["mime_type"] == "text/plain"
        assert repo.get_content(uuid.UUID(respuesta.json()["id"])) == b"hola"

    def test_mime_no_permitido(self, cliente, repo, despachos):
        client = cliente()
        respuesta = _subir(
            client, nombre="foto.png", data=b"PNG-data", mime="image/png"
        )

        assert respuesta.status_code == 415
        assert "image/png" in respuesta.json()["detail"]
        # Rechazado antes de tocar el negocio: nada registrado ni despachado.
        assert despachos == []
        assert repo.list_documents() == []

    def test_archivo_demasiado_grande(self, cliente, despachos):
        client = cliente(settings=Settings(max_upload_mb=1))

        respuesta = _subir(client, data=b"x" * (1024 * 1024 + 1))

        assert respuesta.status_code == 413
        assert despachos == []

    def test_archivo_en_el_limite_exacto_pasa(self, cliente, despachos):
        client = cliente(settings=Settings(max_upload_mb=1))

        respuesta = _subir(client, data=b"x" * (1024 * 1024))

        assert respuesta.status_code == 201, respuesta.text
        assert len(despachos) == 1

    def test_archivo_vacio(self, cliente, despachos):
        client = cliente()

        respuesta = _subir(client, data=b"")

        assert respuesta.status_code == 422
        assert despachos == []

    def test_subida_sin_campo_file(self, cliente, despachos):
        client = cliente()

        respuesta = client.post("/api/documents")

        assert respuesta.status_code == 422  # campo requerido (FastAPI)
        assert despachos == []

    def test_subida_sin_nombre_de_archivo(self, cliente, despachos):
        client = cliente()

        respuesta = _subir(client, nombre="", data=b"hola")

        assert respuesta.status_code == 422
        assert despachos == []


# --- GET /api/documents y /api/documents/{id} ---------------------------------


class TestSeguimiento:
    def test_listado_el_mas_reciente_primero(self, cliente, despachos):
        client = cliente()
        _subir(client, nombre="primero.txt", data=b"uno uno uno")
        _subir(client, nombre="segundo.txt", data=b"dos dos dos")

        respuesta = client.get("/api/documents")

        assert respuesta.status_code == 200
        nombres = [documento["filename"] for documento in respuesta.json()]
        assert nombres == ["segundo.txt", "primero.txt"]

    def test_detalle_refleja_el_estado_del_documento(self, cliente):
        client = cliente()
        alta = _subir(client, data=TEXTO_POLIZA.encode("utf-8"))
        document_id = alta.json()["id"]

        respuesta = client.get(f"/api/documents/{document_id}")

        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["id"] == document_id
        assert cuerpo["status"] == "pending"
        assert cuerpo["content_hash"] == alta.json()["content_hash"]

    def test_detalle_inexistente_da_404(self, cliente):
        client = cliente()

        respuesta = client.get(f"/api/documents/{uuid.uuid4()}")

        assert respuesta.status_code == 404

    def test_detalle_con_uuid_invalido_da_422(self, cliente):
        client = cliente()

        respuesta = client.get("/api/documents/no-es-un-uuid")

        assert respuesta.status_code == 422


# --- GET /api/documents/{id}/content (Fase 4: cita navegable) ----------------


class TestContenido:
    """El endpoint que sirve el PDF original para que el frontend lo abra
    con ``#page=N`` — sin él, la cita es solo texto y no es 'navegable'."""

    def test_devuelve_los_bytes_con_mime_e_inline(self, cliente):
        client = cliente()
        # No es un PDF de verdad, pero el endpoint no parsea el contenido:
        # lo guarda (D15) y lo devuelve tal cual. Lo que importa es el
        # contrato HTTP (media_type, Content-Disposition, bytes idénticos).
        pdf_bytes = b"%PDF-1.4\n%fake pero con bytes literales\n"
        alta = _subir(
            client,
            nombre="poliza-ejemplo.pdf",
            data=pdf_bytes,
            mime="application/pdf",
        )
        document_id = alta.json()["id"]

        r = client.get(f"/api/documents/{document_id}/content")

        assert r.status_code == 200
        assert r.content == pdf_bytes
        assert r.headers["content-type"] == "application/pdf"
        cd = r.headers["content-disposition"]
        assert cd.startswith("inline"), f"esperaba inline, got: {cd}"
        assert 'filename="poliza-ejemplo.pdf"' in cd

    def test_marca_cacheable_porque_el_contenido_es_inmutable(self, cliente):
        # D03: mismo hash = mismos bytes. Reabrir la misma cita no debe
        # recargar el PDF del backend.
        client = cliente()
        alta = _subir(client, data=TEXTO_POLIZA.encode("utf-8"))
        document_id = alta.json()["id"]

        r = client.get(f"/api/documents/{document_id}/content")

        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "public" in cc and "max-age" in cc, f"esperaba cacheable, got: {cc}"

    def test_content_de_documento_inexistente_da_404(self, cliente):
        client = cliente()

        r = client.get(f"/api/documents/{uuid.uuid4()}/content")

        assert r.status_code == 404

    def test_content_no_colisiona_con_detalle_mismo_path(self, cliente):
        # Sanity: /{id} devuelve JSON (DocumentOut), /{id}/content
        # devuelve los bytes crudos. FastAPI debe enrutarlos por
        # especificidad (dos segmentos vs uno).
        client = cliente()
        alta = _subir(client, data=TEXTO_POLIZA.encode("utf-8"))
        document_id = alta.json()["id"]

        detalle = client.get(f"/api/documents/{document_id}")
        contenido = client.get(f"/api/documents/{document_id}/content")

        assert detalle.headers["content-type"].startswith("application/json")
        assert not contenido.headers["content-type"].startswith("application/json")
        assert detalle.json()["id"] == document_id
