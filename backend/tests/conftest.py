"""Fija el modo fake para TODA la suite: tests sin red ni claves.

El enunciado exige probar la ingesta y la consulta con un proveedor
simulado. Como la variable de entorno gana sobre .env y sobre los
defaults (D14), fijarla aquí hermetiza la suite aunque exista un .env
local con credenciales reales.
"""

import os

os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDING_PROVIDER"] = "fake"
