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

## D11 — Chunking por página con solapamiento intra-página

**Contexto.** El enunciado pide chunking con solapamiento y citas verificables
a nivel de página.

**Decisión.** `app/domain/chunker.py` corta el texto **página por página**:
un chunk nunca cruza el límite de una página. Dentro de cada página se
empaquetan palabras enteras hasta `chunk_size` (800 caracteres por defecto)
y cada chunk nuevo arranca con las últimas palabras del anterior que quepan
en el presupuesto `chunk_overlap` (120). El texto se normaliza a espacios
simples (la extracción de PDF mete saltos de línea arbitrarios). Los
`page_number` se validan (≥ 1), las páginas en blanco no producen chunks y
los ordinales son globales y secuenciales. Único caso patológico permitido:
una palabra individual más larga que `chunk_size` se emite como chunk propio
(partirla rompería la literalidad de las citas).

**Por qué.** La cita es "documento + página + fragmento": si un chunk
cruzara dos páginas, la página citada sería ambigua y dejaría de poder
verificarse con un ctrl+F. Palabras enteras, nunca cortes a mitad de
palabra, por la misma razón de literalidad.

**Alternativas descartadas.** Chunking global ignorando páginas (citas
ambiguas). Particionar por oraciones (una cláusula de póliza puede superar
los 800 caracteres y el particionado se vuelve impredecible).
`RecursiveCharacterTextSplitter` de LangChain (misma lógica pero añade una
dependencia al núcleo, que debe quedar puro y sin frameworks).

## D12 — Citas literales: el fragmento es un substring exacto del chunk

**Contexto.** "Citas verificables" debe significar algo comprobable
mecánicamente, no una promesa.

**Decisión.** `app/domain/citations.py` construye cada cita con nombre de
documento, página (1-indexada) y un fragmento **literal** del contenido del
chunk almacenado: nunca paráfrasis, nunca reescritura del LLM. Si el chunk
supera el máximo (220 caracteres por defecto), se recorta por palabra
completa, pero lo citado sigue siendo un substring exacto. El contrato está
expresado como código (`quote_is_verbatim`) y los tests lo fijan. Los puntos
suspensivos de presentación los añade la UI al renderizar; el dominio los
omite para no romper el substring.

**Por qué.** Verificable = el evaluador abre el documento, va a la página
citada y encuentra ese texto. Cualquier transformación del fragmento rompe
esa promesa.

## D13 — Umbral de rechazo: todo-o-nada sobre el mejor resultado

**Contexto.** El enunciado exige que el sistema se niegue a responder cuando
la evidencia recuperada no alcanza un umbral de similitud definido.

**Decisión.** `app/domain/retrieval.py` responde solo si el chunk con MAYOR
similitud coseno alcanza el umbral (inclusivo). Si lo alcanza, responde con
los `top_k` mejores; si no, rechaza de forma explícita con un mensaje fijo
(`REJECTION_MESSAGE`) y **sin entregar contextos débiles** — nunca responde
"con lo que haya". Sin resultados también se rechaza (razón distinta).
La similitud llega ya convertida por el adapter: pgvector devuelve distancia
coseno y el dominio recibe `1 − distancia`.

El umbral por defecto es **0.45**: bge-m3 rara vez supera ~0.7 incluso con
paráfrasis, así que el 0.75 inicial habría rechazado casi todo. Es un punto
de partida razonado que se calibrará empíricamente con el set de evaluación
de la Fase 3.

**Alternativas descartadas.** Filtrar chunk a chunk por el umbral (permitiría
responder apoyado solo en resultados débiles). Responder siempre y dejar que
el LLM decida (el enunciado exige el rechazo del sistema, no del modelo).
Umbral doble mínimo+máximo (más perillas sin evidencia de que ayuden).

## D14 — Configuración: stack real por defecto, modo fake explícito

**Contexto.** El enunciado exige probar la ingesta y la consulta con un
proveedor simulado sin claves, pero el proyecto ya definió su proveedor real
(D10). Se pidió eliminar los valores "fake" como defecto y que toda la
configuración viva en `.env`.

**Decisión.** Los defaults de `app/config.py` y de `docker-compose.yml` son
el stack real: Cloudflare Workers AI con `@cf/meta/llama-3.3-70b-instruct-
fp8-fast` (chat) y `@cf/baai/bge-m3` (embeddings). `.env.example` es la
plantilla real: solo faltan por llenar `CLOUDFLARE_ACCOUNT_ID` y
`CLOUDFLARE_API_TOKEN`. El modo fake NO desaparece —el enunciado lo exige—
pero deja de ser el valor por defecto: lo fijan explícitamente los tests
(`tests/conftest.py` fija `LLM_PROVIDER`/`EMBEDDING_PROVIDER`) o quien quiera
correr sin claves.

Precedencia (pydantic-settings): variable de entorno > `.env` > default —
los valores literales de `config.py` son SOLO fallback (lo fijan tests
parametrizados). El `.env` se resuelve anclado a la raíz del repo
(`ENV_FILE = REPO_ROOT / ".env"`), independiente del directorio desde donde
se lance uvicorn/alembic/celery/pytest (antes era relativo al CWD y un
`.env` de la raíz quedaba ignorado al lanzar desde `backend/`); si no existe
(contenedores), se ignora porque compose inyecta las variables reales. Los
nombres de proveedor son un `Literal`: un typo falla al arrancar con el
listado de valores válidos. `validate_provider_credentials()` verifica las
credenciales del proveedor elegido con fail-fast y mensaje accionable; la
conecta el factory de adapters al arrancar (Fase 2).

**Alternativas descartadas.** `extra="allow"` para cargar cualquier variable
del `.env` sin declararla: pydantic-settings absorbería TODO el entorno del
proceso (PATH incluida) y `model_dump()` volcaría secretos a logs y trazas;
las variables se declaran tipadas — una línea, validación gratis. Mantener
fake como default (la demo arrancaría en modo simulado sin que nadie lo
note). Silent-fallback a fake si faltan credenciales (enmascara la
misconfiguración; mejor morir con mensaje claro).

## D15 — El contenido del documento viaja por la base, no por el broker

**Contexto.** La tarea Celery de ingesta necesita los bytes del archivo
para extraer texto y embedizar, y el límite de subida es 20 MB (D08).

**Decisión.** Tabla `document_contents` 1:1 (PK = FK con `ON DELETE
CASCADE`, migración 0002), en el puerto `DocumentRepository` como
`save_content`/`get_content`. `register_document` escribe documento y
contenido en la MISMA transacción: o queda el registro completo, o no
queda nada. La tarea recibe solo el `document_id` y lee los bytes del
repositorio.

**Por qué.** Un reintento de Celery, una reentrega tras reiniciar el
worker (`acks_late`) o un reproceso manual (D04) arrancan siempre de lo
mismo — "dime qué documento" — sin exigir re-subir el archivo. El
mensaje de la tarea mide unos bytes (un UUID), no ~27 MB de base64
atravesando Redis en cada reintento. Y el original queda disponible
para que la Fase 4 lo sirva al navegador y el evaluador verifique la
cita contra el documento real (D12).

**Alternativas descartadas.** Bytes en el payload de la tarea (el
anti-patrón clásico de Celery: Redis es memoria; con `acks_late` el
mensaje se retiene y se reenvía). Volumen compartido api↔worker (acopla
el despliegue a un solo host y exige limpiar huérfanos). BYTEA dentro
de `documents` (TOAST lo soporta, pero cualquier `SELECT *` del agregado
arrastraría el blob; la tabla aparte lo deja fuera de cada lectura).

## D16 — Reintentos: la tarea clasifica, el servicio solo marca

**Contexto.** Ingesta asíncrona con reintentos: ¿qué se reintenta, cuántas
veces y quién decide cuándo un fallo es definitivo?

**Decisión.** La política vive en la TAREA (`decide_retry`, función pura
en `adapters/tasks/ingestion_task.py`) y clasifica por tipo de
excepción:

- **Fatal** (`FatalIngestionError`, la lanza `run_ingestion` ya habiendo
  marcado el documento `failed`): la tarea termina SIN reintentar — un
  PDF cifrado sigue cifrado tras mil reintentos.
- **Transitoria** (red, base, timeouts del proveedor,
  `SoftTimeLimitExceeded`): hasta 3 reintentos con backoff exponencial
  (10/20/40 s); el documento queda `processing` entre intentos, que es
  la verdad.
- **Presupuesto agotado**: la TAREA marca `failed` con la razón
  ("Reintentos agotados (4 intentos): …") desde una sesión nueva — la
  del último intento pudo morir con la propia base caída.

Transaccionalidad que lo sostiene: el `failed` de negocio COMPLETA la
transacción (`session_scope(commit_on=FatalIngestionError)`), porque ese
fallo ES el resultado correcto de la tarea; las transitorias revierten
(sin estado parcial, D03). El `max_retries` del decorador duplica el
techo de la política como red de seguridad ante refactorizaciones.

**Alternativas descartadas.** `autoretry_for` + `retry_backoff` de Celery
(reintenta TODO, incluidas las fatales: cuatro intentos de un PDF
cifrado; y la política dejaría de ser código visible y testable).
Marcar `failed` desde el servicio ante cualquier excepción (la primera
transitoria mataría el reintento que la iba a curar). No marcar nunca
(un documento que agotó reintentos quedaría `processing` para siempre).

## D17 — La API despacha la tarea DESPUÉS del commit

**Contexto.** El endpoint de subida (Fase 2 paso 4) debe encolar
`tasks.ingest_document` solo cuando el documento ya es visible para el
worker. La solución "natural" de FastAPI —despachar en una
`BackgroundTask`— tiene una trampa de orden que se verificó empíricamente
en FastAPI 0.128: las background tasks corren ANTES del código de salida
de las dependencias con `yield`, así que el commit de la sesión llegaría
tarde. El worker levantaría la tarea sin ver la fila, la clasificaría como
fatal ("el documento no existe") y —siendo fatal— no se reintenta: el
documento quedaría `pending` para siempre.

**Decisión.** El endpoint abre su propia unidad de trabajo con `with`
(el mismo `session_scope` del worker, expuesto como dependencia
inyectable `get_repository_uow`) y despacha la tarea AL SALIR del bloque,
con documento y contenido (D15) ya commiteados. La regla hexagonal se
mantiene: la ruta solo conoce el puerto `DocumentRepository`; el adapter
ORM vive en la pieza de composición. El dispatcher también es inyectable
(`get_ingest_dispatcher`, import lazy de la tarea) para que la suite
pruebe el contrato HTTP sin broker.

Contrato de la subida: `201` + `Location` para documento nuevo (tarea
despachada); `200` para re-subida del mismo contenido (D03: sin tarea
nueva, se devuelve el estado que YA tiene). Validaciones D08 antes de
tocar negocio: `415` MIME no permitido, `413` tamaño, `422` nombre vacío
o archivo vacío. Seguimiento: `GET /api/documents` (listado) y
`GET /api/documents/{id}` (estado, páginas, chunks, motivo de fallo).

**Alternativas descartadas.** `BackgroundTasks` (orden verificado:
dispatch antes del commit — la carrera descrita arriba). Despachar dentro
de la transacción (misma carrera, sin necesidad de background task).
`countdown` artificial en la tarea (esperar "un poquito" no es una
garantía). Commit explícito dentro del endpoint accediendo a la sesión
de la dependencia (rompe la encapsulación del puerto). Ruff: se añade
`backend/ruff.toml` ignorando B008, el falso positivo canónico del
idioma `Depends(...)` de FastAPI (en defaults de ruff desde 0.17, que es
lo que instala el CI con su pin `ruff>=0.8`).

## Uso de IA generativa

Metodología: análisis del enunciado y planificación con IA; generación asistida
de esqueleto, configuración y tests; revisión línea a línea y verificación con
los tests automatizados antes de cada commit. Se completará al final con qué se
corrigió de lo generado por la IA.
