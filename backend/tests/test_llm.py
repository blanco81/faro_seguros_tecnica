"""Tests del adapter de chat OpenAI-compatible (Fase 3, D10).

Mismo patrón que ``test_embeddings``: cliente OpenAI sobre MockTransport
de httpx — sin red, inspeccionando cada request que sale al proveedor.
"""

import json

import httpx
from openai import OpenAI

from app.adapters.llm.chat import OpenAICompatChatProvider


def _cuerpo_chat(contenido):
    return {
        "id": "chatcmpl-prueba",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": contenido},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _proveedor(contenido="hola", cabeceras=None):
    peticiones = []

    def interceptor(request: httpx.Request) -> httpx.Response:
        peticiones.append(request)
        return httpx.Response(200, json=_cuerpo_chat(contenido))

    cliente = OpenAI(
        api_key="clave-de-prueba",
        base_url="http://proveedor-de-prueba/v1",
        default_headers=cabeceras,
        http_client=httpx.Client(transport=httpx.MockTransport(interceptor)),
    )
    proveedor = OpenAICompatChatProvider(
        model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        api_key="clave-de-prueba",
        client=cliente,
    )
    return proveedor, peticiones


def test_chat_devuelve_el_contenido_de_la_respuesta():
    proveedor, _ = _proveedor("La franquicia es de 300 euros.")
    assert proveedor.chat("¿Cuál es la franquicia?") == "La franquicia es de 300 euros."


def test_chat_envia_system_y_user_con_temperatura_cero():
    proveedor, peticiones = _proveedor()
    proveedor.chat("la pregunta", system="eres un asistente")
    cuerpo = json.loads(peticiones[0].read())
    assert cuerpo["messages"] == [
        {"role": "system", "content": "eres un asistente"},
        {"role": "user", "content": "la pregunta"},
    ]
    # La reproducibilidad es parte del contrato del puerto (ports/llm.py):
    # una cita literal no admite creatividad.
    assert cuerpo["temperature"] == 0.0


def test_chat_sin_system_solo_envia_el_user():
    proveedor, peticiones = _proveedor()
    proveedor.chat("la pregunta")
    cuerpo = json.loads(peticiones[0].read())
    assert cuerpo["messages"] == [{"role": "user", "content": "la pregunta"}]


def test_chat_stripea_espacios_del_contenido():
    proveedor, _ = _proveedor("  respuesta con márgenes  \n")
    assert proveedor.chat("q") == "respuesta con márgenes"


def test_chat_contenido_nulo_devuelve_cadena_vacia():
    proveedor, _ = _proveedor(None)
    assert proveedor.chat("q") == ""


def test_chat_pasa_las_cabeceras_del_gateway():
    proveedor, peticiones = _proveedor(
        "hola", cabeceras={"cf-aig-gateway-id": "mi-gateway"}
    )
    proveedor.chat("q")
    assert peticiones[0].headers["cf-aig-gateway-id"] == "mi-gateway"
