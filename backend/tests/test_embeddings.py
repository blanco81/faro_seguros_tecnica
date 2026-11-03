"""Tests de embeddings (Fase 2): fake determinista, adapter OpenAI-compatible
y factory. Todo sin red ni claves (D01/D14): el adapter real se ejercita
contra un transporte httpx simulado que responde el protocolo /embeddings.
"""

import json
import math

import httpx
import pytest
from openai import OpenAI

from app.adapters.factory import build_embedder, build_extractor
from app.adapters.llm.embeddings import OpenAICompatEmbeddingProvider
from app.adapters.llm.fake_embeddings import FakeEmbeddingProvider
from app.config import Settings, get_settings
from app.ports.embeddings import (
    EMBEDDING_DIMS,
    EmbeddingDimensionError,
    EmbeddingProvider,
)


def coseno(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


class TestFakeEmbeddingProvider:
    def test_determinista_entre_llamadas_e_instancias(self):
        texto = "La póliza cubre daños por inundación en el hogar asegurado"
        primera = FakeEmbeddingProvider().embed([texto])[0]
        segunda = FakeEmbeddingProvider().embed([texto])[0]

        assert primera == segunda  # idempotencia: reintentos no cambian nada

    def test_un_vector_por_texto_en_orden_y_con_la_dimension_del_esquema(self):
        textos = ["cláusula 4.1", "exclusiones", "franquicia de 300 euros"]
        vectores = FakeEmbeddingProvider().embed(textos)

        assert len(vectores) == len(textos)
        assert all(len(v) == EMBEDDING_DIMS for v in vectores)
        # Distintos textos → distintos vectores (el orden se conserva).
        assert vectores[0] != vectores[1] != vectores[2]

    def test_vectores_normalizados(self):
        vectores = FakeEmbeddingProvider().embed(["inundación hogar"])
        norma = math.sqrt(sum(c * c for c in vectores[0]))

        assert norma == pytest.approx(1.0)

    def test_textos_identicos_mismo_vector_y_coseno_uno(self):
        # La propiedad que sostiene la deduplicación por contenido (D03) y
        # el "reintento no duplica" de la tarea de ingesta (Fase 2).
        proveedor = FakeEmbeddingProvider()
        a, b = proveedor.embed(["franquicia de 300 euros", "franquicia de 300 euros"])

        assert a == b
        assert coseno(a, b) == pytest.approx(1.0)

    def test_palabras_compartidas_similitud_intermedia(self):
        # Con bolsa de palabras hasheada, "w x y" vs "w x y z" da coseno
        # sqrt(3/4) ≈ 0.87: relevancia parcial, como en un embedding real.
        proveedor = FakeEmbeddingProvider()
        base, ampliada = proveedor.embed(["alfa beta gamma", "alfa beta gamma delta"])
        similitud = coseno(base, ampliada)

        assert 0.0 < similitud < 1.0
        assert similitud == pytest.approx(math.sqrt(3 / 4), abs=1e-9)

    def test_textos_disjuntos_son_ortogonales(self):
        proveedor = FakeEmbeddingProvider()
        a, b = proveedor.embed(["alfa beta gamma", "delta epsilon zeta"])

        assert coseno(a, b) == pytest.approx(0.0, abs=1e-12)

    def test_texto_sin_palabras_no_devuelve_vector_cero(self):
        # El vector cero tiene coseno NaN en pgvector: envenenaría la
        # recuperación. El fake debe defenderse igual que un proveedor real.
        [vector] = FakeEmbeddingProvider().embed(["!!! ???"])

        assert any(c != 0.0 for c in vector)

    def test_lote_vacio_devuelve_vacio(self):
        assert FakeEmbeddingProvider().embed([]) == []


# --- Adapter OpenAI-compatible, contra transporte simulado -----------------


def respuesta_embedding(indices: list[int], dims: int = EMBEDDING_DIMS) -> dict:
    """Cuerpo de respuesta /embeddings: un vector constante por posición."""
    return {
        "object": "list",
        "model": "prueba",
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.5 + 0.01 * i] * dims}
            for i in indices
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def proveedor_con_mock(
    handler, batch_size: int = 32
) -> tuple[OpenAICompatEmbeddingProvider, list[httpx.Request]]:
    """Provider real contra MockTransport; registra cada request HTTP."""
    peticiones: list[httpx.Request] = []

    def interceptor(request: httpx.Request) -> httpx.Response:
        peticiones.append(request)
        return handler(request)

    cliente = OpenAI(
        api_key="clave-de-prueba",
        base_url="http://proveedor.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(interceptor)),
    )
    proveedor = OpenAICompatEmbeddingProvider(
        model="@cf/baai/bge-m3",
        api_key="clave-de-prueba",
        batch_size=batch_size,
        client=cliente,
    )
    return proveedor, peticiones


class TestOpenAICompatEmbeddingProvider:
    def test_devuelve_un_vector_por_texto_en_orden(self):
        proveedor, peticiones = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0, 1, 2]))
        )

        vectores = proveedor.embed(["a", "b", "c"])

        assert len(vectores) == 3
        assert vectores[0][0] == pytest.approx(0.5)  # posición 0
        assert vectores[2][0] == pytest.approx(0.5 + 0.02)  # posición 2
        # La llamada lleva el modelo y los textos como manda el protocolo.
        cuerpo = json.loads(peticiones[0].content)
        assert cuerpo["model"] == "@cf/baai/bge-m3"
        assert cuerpo["input"] == ["a", "b", "c"]

    def test_autenticacion_bearer_y_ruta_del_endpoint(self):
        proveedor, peticiones = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0]))
        )
        proveedor.embed(["a"])

        assert peticiones[0].url.path == "/v1/embeddings"
        assert peticiones[0].headers["Authorization"] == "Bearer clave-de-prueba"

    def test_trocea_en_lotes_fisicos_y_concatena_en_orden(self):
        # 5 textos, batch_size 2 → 3 llamadas HTTP; resultado: 5 vectores.
        # El mock numera las posiciones GLOBALMENTE (continúa entre lotes)
        # para poder verificar que la concatenación preserva el orden.
        servidos = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            lote = json.loads(request.content)["input"]
            inicio = servidos[0]
            servidos[0] += len(lote)
            return httpx.Response(
                200, json=respuesta_embedding(list(range(inicio, servidos[0])))
            )

        proveedor, peticiones = proveedor_con_mock(handler, batch_size=2)

        vectores = proveedor.embed(["a", "b", "c", "d", "e"])

        assert len(peticiones) == 3
        assert servidos[0] == 5
        assert len(vectores) == 5
        assert [v[0] for v in vectores] == pytest.approx(
            [0.5, 0.51, 0.52, 0.53, 0.54]
        )

    def test_ordena_por_index_ante_proveedores_desordenados(self):
        # Defensa del contrato: el proveedor devuelve los items sueltos y
        # el adapter los reordena por "index".
        proveedor, _ = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([2, 0, 1]))
        )

        vectores = proveedor.embed(["a", "b", "c"])

        assert vectores[0][0] == pytest.approx(0.5)
        assert vectores[2][0] == pytest.approx(0.5 + 0.02)

    def test_dimension_incorrecta_falla_con_el_modelo_y_lo_que_falta(self):
        proveedor, _ = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0], dims=8))
        )

        with pytest.raises(EmbeddingDimensionError) as exc:
            proveedor.embed(["a"])

        mensaje = str(exc.value)
        assert "@cf/baai/bge-m3" in mensaje
        assert "8" in mensaje
        assert "1024" in mensaje  # lo que el esquema exige (D10/D02)

    def test_cantidad_incorrecta_de_vectores_falla(self):
        # El proveedor pierde un embedding: el adapter no puede devolver
        # silenciosamente menos de lo pedido.
        proveedor, _ = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0]))
        )

        with pytest.raises(EmbeddingDimensionError, match="3 textos"):
            proveedor.embed(["a", "b", "c"])

    def test_cabecera_de_gateway_cuando_se_configura(self):
        # Preset Cloudflare vía AI Gateway (D10): la cabecera viaja en
        # cada llamada de embeddings.
        peticiones: list[httpx.Request] = []

        def interceptor(request: httpx.Request) -> httpx.Response:
            peticiones.append(request)
            return httpx.Response(200, json=respuesta_embedding([0]))

        cliente = OpenAI(
            api_key="clave",
            base_url="http://proveedor.test/v1",
            default_headers={"cf-aig-gateway-id": "mi-gateway"},
            http_client=httpx.Client(transport=httpx.MockTransport(interceptor)),
        )
        proveedor = OpenAICompatEmbeddingProvider(
            model="m", api_key="clave", client=cliente
        )

        proveedor.embed(["a"])

        assert peticiones[0].headers["cf-aig-gateway-id"] == "mi-gateway"

    def test_lote_vacio_no_hace_llamadas(self):
        proveedor, peticiones = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0]))
        )

        assert proveedor.embed([]) == []
        assert peticiones == []

    def test_batch_size_invalido_se_rechaza_al_construir(self):
        with pytest.raises(ValueError, match="batch_size"):
            OpenAICompatEmbeddingProvider(model="m", api_key="k", batch_size=0)


class TestFactory:
    def test_en_la_suite_el_proveedor_es_el_simulado(self):
        # conftest fija LLM/EMBEDDING_PROVIDER=fake: el factory debe
        # devolver el fake (el enunciado exige correr así, sin red).
        assert isinstance(build_embedder(), FakeEmbeddingProvider)

    def test_ambos_adapters_cumplen_el_puerto(self):
        # Protocol runtime_checkable: fake y real son EmbeddingProvider.
        assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)

        proveedor, _ = proveedor_con_mock(
            lambda request: httpx.Response(200, json=respuesta_embedding([0]))
        )
        assert isinstance(proveedor, EmbeddingProvider)

    def test_preset_cloudflare_con_credenciales_construye_el_adapter_real(self):
        settings = Settings(
            _env_file=None,
            llm_provider="cloudflare",
            embedding_provider="cloudflare",
            cloudflare_account_id="cuenta123",
            cloudflare_api_token="token",
        )
        proveedor = build_embedder(settings)

        assert isinstance(proveedor, OpenAICompatEmbeddingProvider)

    def test_preset_cloudflare_sin_credenciales_falla_rapido(self):
        settings = Settings(
            _env_file=None,
            llm_provider="cloudflare",
            embedding_provider="cloudflare",
            cloudflare_account_id=None,
            cloudflare_api_token=None,
        )
        with pytest.raises(ValueError) as exc:
            build_embedder(settings)

        assert "CLOUDFLARE_ACCOUNT_ID" in str(exc.value)  # mensaje accionable

    def test_preset_openai_con_api_key(self):
        settings = Settings(
            _env_file=None,
            llm_provider="openai",
            embedding_provider="openai",
            openai_api_key="sk-prueba",
        )
        assert isinstance(build_embedder(settings), OpenAICompatEmbeddingProvider)

    def test_el_extractor_del_factory_despacha_ambos_mimes(self):
        extractor = build_extractor()

        assert extractor.supported_mimes == frozenset(
            {"application/pdf", "text/plain"}
        )
        assert set(get_settings().allowed_mime_types) <= extractor.supported_mimes
