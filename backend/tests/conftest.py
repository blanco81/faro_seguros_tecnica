"""Fija el modo fake para TODA la suite: tests sin red ni claves.

El enunciado exige probar la ingesta y la consulta con un proveedor
simulado. Como la variable de entorno gana sobre .env y sobre los
defaults (D14), fijarla aquí hermetiza la suite aunque exista un .env
local con credenciales reales.
"""

import os

os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDING_PROVIDER"] = "fake"

# La suite jamás toca la base (todos los tests van contra dobles, D01),
# pero FastAPI construye las dependencias ANTES de validar el cuerpo de
# la request: un DATABASE_URL ambiental inválido tumbaría tests de puro
# contrato (422) que nunca debieron llegar a hablar de base. SQLite en
# memoria: parsea sin driver, no conecta nunca y grita si alguien olvida
# sobrescribir la dependencia real.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

# Fase 5 (D23): sin collector en la suite — la instrumentación OTel debe
# quedarse en no-op (setup_telemetry devuelve None y create_app no toca
# la app). Los tests de telemetría usan providers EXPLÍCITOS que nunca
# pasan por el global, así que esto solo blindaría un create_app real.
os.environ["OTEL_ENABLED"] = "false"
