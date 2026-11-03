# DECISIONS.md — Faro Seguros RAG

Registro de decisiones de diseño: contexto → decisión → alternativas descartadas.
Se actualiza al cierre de cada fase del cronograma.

## D01 — Puerto propio para LLM y embeddings (arquitectura hexagonal)

**Contexto.** El enunciado exige llamar al LLM "a través de una interfaz propia" y
probar la ingesta y la consulta "con un proveedor de LLM y embeddings simulado",
sin red ni claves reales.

**Decisión.** Definir los puertos `LLMProvider` y `EmbeddingProvider` en
`app/ports/` (Protocols de Python) con adapters concretos (`openai_adapter`,
`fake_llm`, ...) inyectados desde los puntos de entrada (FastAPI `Depends` /
factory del worker). El núcleo (chunking, umbral, citas) y los servicios no
importan ningún SDK de proveedor. El adapter fake es la prueba de que la inversión
de dependencias funciona: los tests de integración corren sin red.

**Alternativas descartadas.** LiteLLM como dependencia directa (acopla el código a
una API de terceros y añade peso; si hiciera falta multi-proveedor real, se
implementa un adapter *detrás* de nuestro puerto). Inyectar el cliente OpenAI en
los servicios (violaría el requisito de pruebas sin red).

## D02 — PostgreSQL + pgvector con índice HNSW (distancia coseno)

**Contexto.** El enunciado pide almacenamiento en PostgreSQL con pgvector y un
índice adecuado para los chunks.

**Decisión.** Imagen `pgvector/pgvector:pg16`; columna `vector(1024)` e índice
HNSW con `vector_cosine_ops` en la migración inicial. La dimensión queda fija a
1024 (`@cf/baai/bge-m3`, decisión D10); cambiarla exige migración explícita.

**Alternativas descartadas.** IVFFlat (requiere elegir `lists` y entrenar con
datos; HNSW da mejor recall/latencia sin tuning a esta escala). Bases vectoriales
externas tipo Qdrant/Milvus (el enunciado pide PostgreSQL).

## D03 — Idempotencia por hash de contenido

**Contexto.** Subir el mismo archivo dos veces o reintentar una tarea no debe
duplicar chunks.

**Decisión.** SHA-256 del contenido como `content_hash` UNIQUE en `documents`.
Re-subir un archivo existente devuelve el documento ya registrado sin reprocesar.
El reproceso/reintento borra y reinserta los chunks del documento en una única
transacción, de modo que un fallo a mitad no deja estado parcial.

## D04 — Estados y transiciones viven en el dominio

**Contexto.** El enunciado pide estados "pendiente, procesando, listo, fallido"
con transiciones claras.

**Decisión.** `app/domain/document.py` define el enum (`pending`/`processing`/
`ready`/`failed`) y la tabla de transiciones permitidas (con reproceso desde
`ready` y reintento desde `failed`). Ni la base ni la API deciden transiciones:
el dominio las valida y lanza `InvalidTransitionError`.

## D05 — Migraciones Alembic ejecutadas al arrancar la API

**Decisión.** El comando del contenedor `api` ejecuta `alembic upgrade head`
antes de levantar uvicorn. Esquema versionado desde el commit inicial.

**Alternativas descartadas.** Job de migración separado o migraciones manuales
(más pasos, sin beneficio a esta escala).

## D06 — Frontend Vite + React detrás de nginx con proxy `/api`

**Decisión.** En producción, nginx sirve el estático y proxifica `/api/` al
servicio `api` (sin CORS en producción). En desarrollo, el proxy lo hace Vite.
La API expone sus rutas bajo el prefijo `/api` y `/health` para el healthcheck.

La etapa de build del Dockerfile usa `node:24-bookworm-slim` (glibc, LTS
vigente) con `npm ci --include=dev`, y `.dockerignore` excluye `.npmrc`. Motivo:
un build local falló con `exit 127` (`vite: not found`) tras un `npm ci`
"exitoso"; las tres causas posibles eran (a) una configuración `omit=dev`
heredada de un `.npmrc` del entorno, (b) un lockfile divergente en la copia
local, o (c) rozaduras de musl/alpine. Las tres quedan neutralizadas: el flag
fuerza las devDependencies, el lockfile queda congelado en git y verificado por
CI, y glibc coincide con el entorno donde se generó el lockfile. El runtime
sigue siendo `nginx:alpine` (multi-stage), así que el costo es solo en la
etapa de build.

**Alternativas descartadas.** Mantener `node:20-alpine` (node 20 sin soporte
desde abril de 2026 y musl añade una variable más sin beneficio); usar
`npm install` en vez de `npm ci` (no reproduce el lockfile exacto).

## D07 — Observabilidad: OTel → collector (exporter debug), Jaeger opcional

**Decisión.** SDK OTel en API y worker (instrumentación de FastAPI, Celery y
httpx), exportación OTLP al collector que imprime trazas en consola. Jaeger se
levanta solo con `--profile ui` para la demo de la entrevista.

**Alternativas descartadas.** Mandar trazas directamente a Jaeger desde los
servicios (el enunciado pide levantar un colector o exportador).

## D08 — Seguridad base desde el día 1

**Decisión.** Secretos solo por variables de entorno (`.env` fuera de git);
validación de tipo MIME y tamaño máximo en la subida; el contenido de los
documentos se trata como no confiable frente a inyección de prompts (se aísla
en el prompt, Fase 5).

## D09 — Recortes conscientes (segunda iteración)

Autenticación y multiusuario, borrado/reindexado de documentos, streaming de
respuestas, recuperación híbrida texto+vector y re-ranking quedan fuera del MVP
de 72 h y documentados como segunda iteración.

## D10 — Proveedor real: Cloudflare Workers AI (chat + embeddings)

**Contexto.** El enunciado pregunta qué proveedor de LLM y embeddings se elige,
cómo se tratan coste, latencia y dependencia. Los tests deben correr sin red ni
claves (los adapters fake lo garantizan), pero la demo necesita un proveedor real.

**Decisión.** Cloudflare Workers AI para chat y embeddings, a través de su
endpoint **OpenAI-compatible**
(`https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1`):

- **Chat**: `@cf/meta/llama-3.3-70b-instruct-fp8-fast` — 70B con inferencia
  optimizada (2-4x según Cloudflare), activo y mantenido.
- **Embeddings**: `@cf/baai/bge-m3` — **multilingüe** (las pólizas están en
  español), 1024 dimensiones, contexto largo. Esto fija `vector(1024)` en el
  esquema (D02).

Al ser OpenAI-compatible, el adapter de Cloudflare es un *preset* del adapter
OpenAI (mismo SDK, distinta `base_url`/token): cero código adicional por
proveedor. Autenticación con API token (`Authorization: Bearer`), account ID por
variable de entorno; opcionalmente cabecera `cf-aig-gateway-id` para enrutar por
AI Gateway.

**Por qué:** capa gratuita de Workers AI suficiente para la demo y el set de
evaluación; un solo proveedor para chat y embeddings (una sola cuenta y clave);
sin dependencia de cuenta OpenAI; bge-m3 es el único embedding multilingüe del
catálogo de Workers AI (bge-*-en son solo inglés y degradarían la recuperación
en español).

**Alternativas descartadas.** OpenAI directo (requiere cuenta/clave OpenAI para
la demo; queda soportado como segundo preset). bge-base-en-v1.5 de Cloudflare
(inglés; mal ajuste con documentos en español). LiteLLM (peso extra; el preset
OpenAI-compatible ya da lo necesario). Modelos locales tipo Ollama (latencia y
setup en la máquina del evaluador).

## Uso de IA generativa

Metodología: análisis del enunciado y planificación con IA; generación asistida
de esqueleto, configuración y tests; revisión línea a línea y verificación con
los tests automatizados antes de cada commit. Se completará al final con qué se
corrigió de lo generado por la IA.
