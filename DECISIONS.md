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

## D18 — El 413 del smoke se omite (lo cubre la suite unitaria)

**Contexto.** El smoke de la Fase 2 (sección 14) valida los rechazos de
subida D08 que aportan señal nueva sobre el stack real: 415 (MIME no
permitido), 422 (archivo vacío), 422 (sin nombre). El 413 (tamaño >
`MAX_UPLOAD_MB`) no se ejercita porque fabricar un archivo de >20 MB en
cada corrida —para ver un HTTP que la suite unitaria ya cubre— es
despilfarro de I/O y bytes temporales.

**Decisión.** El 413 se cubre en `tests/test_api_documents.py`; el smoke
lo omite con un comentario apuntando aquí. El contrato (código + mensaje)
es el mismo sin importar quién lo dispare; lo que el smoke sí aporta
(servidor real, multipart real, estado en la base) ya está cubierto por
los otros tres rechazos de la sección 14.

**Alternativas descartadas.** Generar un payload grande con `fallocate`
en el smoke (lento, escribe basura al disco en cada run). Subir un PDF
grande commiteado (pesa en el repo para siempre). Bajar `MAX_UPLOAD_MB` a
1 MB en el smoke para forzar el 413 con un archivo chico (cambia la
configuración del sistema bajo prueba — el smoke debe probar el sistema
real, no una versión capada).

## D19 — Consulta agéntica con verificación iterativa de citas

**Contexto.** El enunciado exige RAG con citas verificables y rechazo
cuando la evidencia no alcanza (D13). Un RAG plano (retrieve → generate
en un solo pase) cumple la letra pero falla en los casos que distinguen
a un asistente útil para un analista de siniestros: (a) preguntas
multi-documento/multi-paso (un siniestro toca póliza + anexo + carta),
donde una sola recuperación no trae todo lo necesario; (b) fidelidad de
la cita — el LLM puede parafrasear y atribuir mal el fragmento; (c) el
umbral de rechazo (D13) se evalúa una sola vez, sin oportunidad de
refinar la consulta. La programación agéntica (requisito orientado de
último momento) encaja como evolución natural de la Fase 3, no como
pegote: convierte los pasos single-shot en un bucle con verificación.

**Decisión.** La Fase 3 se implementa como un **servicio agéntico
acotado** — `AgenticQueryService` — que orquesta los puertos existentes
(`LLMProvider`, `DocumentRepository`) más un nuevo `RetrievalPort`
(búsqueda pgvector, top-k por similitud coseno sobre `chunks.embedding`).
Bucle: **planificar** (¿la pregunta necesita descomposición en
sub-preguntas?) → **recuperar** (top-k chunks, umbral D13) → **generar**
respuesta con cita literal → **verificar** (el fragmento citado aparece
verbatim en un chunk recuperado) → si no cuadra o la evidencia es
insuficiente, **refinar** la consulta y **reintentar**, con **techo**
(máx `MAX_QUERY_ITERATIONS = 3`) → **responder | rehusar | pedir
aclaración**. El agente decide, itera y verifica; sin descomponerse en
múltiples agentes. Detalle de implementación (puertos, tests, umbral de
parada, plan paso a paso) en el documento de diseño `Fase_3_Diseno_Agenticо`.

**Alternativas descartadas (no para siempre, "no ahora").** Multi-agente
LangGraph (router → retriever → synthesizer → verifier → compliance):
claridad arquitectónica, pero infraestructura pesada (graph state, nodos,
edges) desproporcionada para un MVP con el plazo de entrega actual; cada
nodo sería un envoltorio fino sobre el mismo puerto. Agente tool-using
con tools dinámicas (`search_chunks`, `get_document`, `compare_clauses`):
potente pero token-heavy, latencia alta, y la superficie de tools añade
complejidad de testing sin valor visible al analista en esta fase.
Conversacional multi-turno con memoria: cambia el contrato del enunciado
(pregunta-respuesta) y el alcance de la Fase 4. Estas tres quedan como
roadmap documentado.

## D20 — Umbral de rechazo confirmado en 0.45 (calibración verificada end-to-end)

**Contexto.** D13 dejó `REJECTION_THRESHOLD = 0.45` como "punto de partida
razonado" pendiente de calibración con el set de evaluación de la Fase 3.
La Fase 3 ya está implementada (D19) y el smoke end-to-end
(`scripts/smoke_stack.sh`, sección 15) es el primer set real de
comprobaciones que ejercita el pipeline completo con proveedor real
(Cloudflare Workers AI: `@cf/baai/bge-m3` para embeddings y
`@cf/meta/llama-3.3-70b-instruct-fp8-fast` para chat) sobre un documento
ingestado de verdad (`poliza-ejemplo.pdf`, 2 páginas, 4 chunks).

**Decisión.** El umbral se **confirma en 0.45** — sin subir ni bajar. La
verificación con `make smoke` arrojó **37/37 comprobaciones OK**, y en la
sección 15 (la que decide la calibración) los dos casos críticos
resolvieron como esperaba D13:

- **Pregunta en dominio** ("¿Cuál es la franquicia aplicable a la
  cobertura de daños por agua?") → `refused: false`, respuesta generada
  en **1 sola iteración** del bucle agéntico, cita `poliza-ejemplo.pdf`
  página 2 verificada literal contra la base con
  `regexp_replace(content, '\s+', ' ', 'g')` (D12).
- **Pregunta fuera de dominio** ("¿Cuál es la receta tradicional de la
  tortilla de patatas?") → `refused: true` con `REJECTION_MESSAGE` (D13).

El bucle no tuvo que refinar en el caso válido (el primer intento ya
cruzó el umbral y la cita cuadró verbatim) — confirmación de que 0.45 no
es ni tan estricto que rechace lo legítimo ni tan permisivo que deje
pasar lo irrelevante, para este par consulta/documento con bge-m3.

Nota operativa: durante la calibración se encontró un bug en el parser
JSON del smoke (`jstr` casaba con strings citados pero no con bools ni
`null`, así que `"refused": false` se leía como vacío y reportaba FAIL
fantasma). Se añadió `jbool` (extrae `true`/`false` sin comillas) para
los campos booleanos; `jstr` sigue usándose para strings. Bug del
parser, no del umbral — pero importante dejarlo anotado para que el set
de evaluación formal no herede falsos negativos.

**Alternativas descartadas.** Subir a 0.55–0.60 ("más conservador"):
con bge-m3, cosenos típicos para consultas legítimas sobre pólizas
están en el rango 0.45–0.6; subir el umbral habría rechazado la
consulta válida en la primera iteración y forzado refinamiento (con
latencia y gasto de tokens) sin ganancia de precisión. Bajar a 0.30
("más permisivo"): dejaría pasar la pregunta fuera de dominio, que
justamente rompe la garantía del enunciado (no responder cuando la
evidencia no alcanza). Calibración con set formal etiquetado (precision@
k, recall@k) y curva ROC: queda para la Fase 5 (observabilidad) y la
iteración de evaluación; por ahora el smoke end-to-end con los dos casos
polo (uno válido, uno irrelevante) es la calibración mínima que el
enunciado exige para cerrar la Fase 3.

## D21 — Frontend Fase 4: React+Vite sobrio, citas navegables vía `/content`

**Contexto.** El enunciado pide "subir y ver estado, preguntar, citas
navegables" — tres bloques que cubren todo el flujo del analista de
siniestros: cargar la póliza, seguir su ingesta asíncrona, consultar el
agente RAG y verificar la cita abriendo el documento original en la
página citada. La Fase 0 dejó un `App.jsx` placeholder servido por
nginx con proxy `/api` al backend; la Fase 4 lo sustituye por la UI real.
Una restricción: las citas del agente (D19) traen `{document, page,
quote}` donde `document` es el **filename**, no el id del documento — y
el endpoint para servir el PDF necesita el id. El frontend tiene que
resolver ese mapeo.

**Decisión.**

- **Stack:** React 18 + Vite 5 ya estaban (Fase 0); se mantienen sin
  añadir TypeScript, Tailwind, shadcn/ui ni React Router. JSX plano,
  CSS plano con variables, `fetch` nativo. Es una MVP de tres vistas
  (subida/listado, consulta, cita) — cualquier capa extra sería peso
  sin retorno. Tema "asegurador sobrio": azul marino `#0F2A4A`,
  system-ui, tablas densas, badges de estado (D04) con color por estado.
- **Shell con tabs:** `App.jsx` mantiene la lista de documentos en su
  estado y la comparte con las dos vistas. `DocumentsView` muestra la
  subida + lista; `QueryView` muestra el form + resultado + citas.
- **Polling 3s:** `setInterval` que llama a `GET /api/documents` cada 3
  segundos mientras haya al menos un documento en `pending`/`processing`.
  En cuanto todos están `ready`/`failed`, para. Sin WebSocket: la
  ingesta de pólizas dura segundos y la complejidad de un canal WS no se
  justifica para esta MVP.
- **Subida:** zona drag&drop + `<input type=file>` clásico bajo el mismo
  `label` (clic en la zona abre el selector). MIME aceptados: PDF y
  texto plano. El backend valida (D08) tipo/tamaño/nombre — el frontend
  confía en el 415/413/422 y muestra el `detail`.
- **Cita navegable:** nueva ruta `GET /api/documents/{id}/content` sirve
  el contenido crudo guardado en `document_contents` (D15) con el MIME
  original, `Content-Disposition: inline` (¡no `attachment`!) y
  `Cache-Control: public, max-age=3600` (el contenido es inmutable por
  D03 — mismo hash = mismos bytes). El frontend abre
  `/api/documents/{id}/content#page=N` en pestaña nueva: el visor de PDF
  nativo del navegador salta a la página citada. El mapeo filename → id
  lo hace `Citation.jsx` buscando en la lista de documentos del shell
  por `filename`. Si no encuentra el documento (p. ej., fue borrado),
  muestra la cita sin link — el `quote` sigue siendo información útil.

**Alternativas descartadas.** Migrar a TypeScript + Tailwind + shadcn/ui
+ React Router (stack "moderno"): apilaría migración de build,
configuración de TS, `tailwind.config`, dependencias nuevas y surface
de testing sin valor visible para 3 vistas; la deuda que añadiría es
mayor que la que quita. **WebSocket** en vez de polling: real-time pero
con coste de servidor WS, reconexión, manejo de sesiones — la ingesta
toma segundos y el analista mira la lista, no la mira en vivo.
**Visor PDF embebido** (iframe con `/content` o `pdf.js` propio):
integrado pero pesa con PDFs grandes y `pdf.js` añade ~1 MB de bundle.
**`Content-Disposition: attachment`**: forzaría descarga; el navegador
no abriría el visor y `#page=N` no tendría efecto — por eso `inline`.
**Mapear filename → id en el backend** (que la cita trajera el id
directamente): rompería el contrato del dominio (D12) donde la cita es
"documento, página, fragmento" legible por un humano, no un UUID.

## D22 — El smoke inspecciona cabeceras con el propio GET (`curl -D`), nunca con HEAD

**Contexto.** Al cerrar la Fase 4, el smoke 16c reportaba
`GET /content -> 200 pero headers malos (ct='application/json', cd='')`
para `GET /api/documents/{id}/content`, mientras el `curl` manual al mismo
URL devolvía el PDF binario y los 4 tests del endpoint (MIME, `inline`,
cacheable, 404) estaban verdes. Tres fuentes de verdad con dos diciendo
"bien" y una diciendo "mal" — la tercera era la del smoke.

**Decisión.** Diagnóstico reproducido antes de tocar nada: el smoke extraía
las cabeceras con `curl -I`, que envía **HEAD**, y FastAPI no registra HEAD
en rutas GET (`APIRoute` no lo añade — a diferencia de Starlette puro, que
sí lo hace). El endpoint contestaba `405 Method Not Allowed` con cuerpo
JSON, y el smoke leía el `content-type` **de esa respuesta de error**, no
del endpoint. Fix en el smoke: un único GET con `-D <fichero>` volcando las
cabeceras (el mismo patrón de la sección 10 con `alta.headers`) — cuerpo y
cabeceras salen de la misma respuesta — más verificación de magic bytes
(`%PDF`): la cita solo es navegable si lo que se sirve es de verdad un PDF.
Validación: FastAPI 0.128 con el endpoint replicado mostró GET → 200
`application/pdf` `inline` y HEAD → 405 `application/json`; la lógica vieja
del smoke reprodujo el fallo carácter por carácter contra un uvicorn real y
la nueva pasó (40/40 OK en el stack). Regla para el resto del proyecto:
inspeccionar cabeceras de endpoints FastAPI con el propio GET (`curl -D`),
nunca pidiendo un HEAD que el framework no sirve.

**Alternativas descartadas.** Registrar HEAD en el endpoint
(`methods=["GET", "HEAD"]`): arregla el síntoma, pero ningún consumidor real
manda HEAD a `/content` (el visor del navegador abre con GET) — el chequeo
debe pedir exactamente lo que dice verificar. Dos curl separados (uno para
el cuerpo, otro para las cabeceras): duplica peticiones y abre la puerta a
que lo comprobado y lo descargado diverjan; `-D` garantía de que son la
misma respuesta.

## D23 — Observabilidad Fase 5 (pasos 1-3): un módulo de telemetría, dos procesos, propagación HTTP → Celery

**Contexto.** La Fase 5 pide observabilidad end-to-end (HTTP → Celery →
LLM). Las dependencias (`opentelemetry-*`, incluidas las instrumentaciones
de FastAPI/Celery/httpx), el servicio `otel-collector` y las variables
`OTEL_ENABLED`/`OTEL_EXPORTER_OTLP_ENDPOINT` existían desde fases
anteriores — pero `app/telemetry/` estaba vacío: nada instrumentaba nada.
El objetivo de estos primeros pasos es que una sola traza cruce el stack:
`POST /api/documents` (span HTTP en faro-api) → publicación de la tarea
(span productor) → `tasks.ingest_document/run` (span en faro-worker) →
llamadas de embeddings (span httpx), y lo mismo por el camino de la
consulta (`POST /api/query` → bucle agéntico → LLM).

**Decisión.** Un único módulo `app/telemetry/setup.py` con tres piezas:

1. **`setup_telemetry(service_name)`** monta el TracerProvider del
   proceso (OTLP/HTTP + `BatchSpanProcessor`). Idempotente (guard de
   módulo: `set_tracer_provider` es una vez por proceso y `main.py`
   crea la app a nivel de módulo Y bajo demanda) y desactivable:
   `OTEL_ENABLED=false` → `None` y cero efectos (así hermetiza la suite
   la variable del conftest, igual que `LLM_PROVIDER=fake`).
2. **`instrument_api` / `instrument_worker`**: qué instrumenta cada
   proceso. La API también es PRODUCTOR de tareas Celery, así que
   instrumenta FastAPI + Celery + httpx (LLM); el worker instrumenta
   Celery + httpx (embeddings). `service.name` distingue `faro-api` de
   `faro-worker` en Jaeger. Los instrumentores de librería se aplican
   una vez por proceso (set de guard): son parches de clase y repetirlos
   solo genera avisos.
3. **Propagación HTTP → Celery**: la instrumentación de Celery inyecta
   el contexto (cabeceras `traceparent`) al publicar y lo extrae al
   ejecutar — `run` queda como hijo de `publish`. El hook del worker va
   en la señal `worker_init` (nunca en el import): así la suite y el
   proceso API, que importan `celery_app`, no instrumentan nada.

Dos hallazgos de implementación que quedan como regla:

- **El exporter OTLP/HTTP del SDK NO añade `/v1/traces`** a un endpoint
  sin ruta: exporta a `/` y el collector lo rechaza en silencio.
  `normalizar_endpoint_otlp` lo añade (verificado contra un receptor
  real antes de escribir el código, no contra la documentación).
- **El worker heredaba del anchor `http://otel-collector:4318`**, un DNS
  que no resuelve desde `network_mode: host` — sus trazas se habrían
  perdido sin error visible. El compose ahora fija `localhost:4318`
  para api Y worker (el collector mapea 4318 al host).

**Verificación.** Tres niveles: (a) unitaria — `tests/test_telemetry.py`
(8 tests: normalización del endpoint, no-op desactivado, idempotencia,
span HTTP con ruta plantilla, service.name en el Resource) con providers
explícitos que nunca tocan el global; suite completa 180/180;
(b) local sin Docker — `scripts/verifica_telemetria_local.py` arranca un
receptor OTLP real y comprueba export, spans HTTP/httpx, inyección de
`traceparent` en el mensaje Celery (broker `memory://`) y span `run`
(8/8 OK); (c) el smoke, nueva sección 17: spans de `faro-api` y
`faro-worker` en los logs del collector y — la prueba de la propagación
— `publish` y `run` compartiendo Trace ID (intersección de los IDs de
los últimos 15 min, tolerante a re-ejecuciones del propio smoke).

**Alternativas descartadas.** Instrumentar en el import de
`celery_app.py`: activaría la instrumentación en tests y en la API (que
no ejecuta tareas) solo por importar el módulo. `OTEL_SERVICE_NAME`
como setting: los nombres de servicio son constantes de despliegue, no
perillas — mismo criterio de "sin teclas sin evidencia" que D16.
Instrumentación en cada adapter (llm/embeddings) a mano: los parches de
httpx cubren el SDK de OpenAI completo (que corre sobre httpx) sin
tocar los adapters. Span de dominio (scores, iteraciones del agente):
siguiente paso de la Fase 5 — primero la espina dorsal, luego la
instrumentación con semántica de negocio.

**Pendiente de la Fase 5.** Spans de dominio RAG (score vs umbral por
iteración, desenlaces), métricas precision@k/recall@k (D20), perfil
`ui` con Jaeger ya presente en el compose (falta el exportador
collector → Jaeger en `infra/otel-collector-config.yaml`), y el
aislamiento del contexto no confiable en el prompt (D08).

## D24 — Tres bugs destapados por la primera corrida real del smoke de la Fase 5

**Contexto.** D23 entregó la instrumentación y la sección 17 del smoke, pero
la primera ejecución contra el stack de verdad dio 41 OK / 3 FAIL: spans de
`faro-api` presentes, pero ni `faro-worker` ni la tarea `tasks.ingest_document`
ni la propagación (trace ID compartido). Tres bugs independientes, los tres
silenciosos hasta correr el sistema real.

**Bug 1 — el smoke asumía una ingesta fresca que no ocurría.** La subida de la
póliza (sección 10) es idempotente por diseño (D03): en un stack donde el PDF
ya estaba registrado, `POST /api/documents` responde **200 y no despacha
tarea nueva**. Sin tarea despachada en esta corrida no hay spans de Celery que
verificar → checks 2-4 fallaban por construction. **Fix:** la sección 17 sube
ahora un `.txt` de contenido único (un `date +%s%N` en el cuerpo) → siempre
201 → tarea despachada → spans reales; `text/plain` está admitido por la
validación (D08) y la extracción (`PlainTextExtractor`). La sonda no toca los
documentos de las secciones 10-16 (distinto `doc_id`).

**Bug 2 — instrumentar el worker en `worker_init` (padre) pierde los spans en
prefork.** El compose arranca `celery worker --concurrency=2` (pool **prefork**,
el default en Linux): `worker_init` se emite en el **padre**, que luego
`fork()`a los hijos. La instrumentación conectaba los signal receivers y
montaba el `TracerProvider`+`BatchSpanProcessor` en el padre; los hijos
heredan el provider, pero **`fork()` no copia hilos** → el hilo exportador del
BSP no existe en el hijo → los spans del `run` se acumulan en una cola muerta
y **nunca se exportan**, sin error visible. **Fix:** enganchar la
instrumentación en `worker_process_init` (señal que se emite en **cada hijo**
del pool al arrancar): cada hijo monta un provider fresco con su propio hilo
exportador vivo. Es el patrón documentado por `opentelemetry-instrumentation-celery`.
El padre queda sin instrumentar (con prefork no ejecuta tareas). Pinned por
`test_worker_se_instrumenta_en_cada_hijo_prefork`.

**Bug 3 — nombres de span invertidos en el check de propagación.** La
instrumentación nombra los spans como `<operación>/<tarea>` (prefijo, ver
fuente): `apply_async/tasks.ingest_document` (productor) y
`run/tasks.ingest_document` (consumidor). El smoke grepaba
`tasks.ingest_document/publish` y `.../run` (sufijo) → **esos nombres no
existen nunca** → el `comm -12` de trace IDs daba siempre vacío → el check 4
fallaba aunque todo funcionara. **Fix:** grep de los nombres reales.

**Alternativas descartadas.** (b1) Subir de nuevo la póliza tras borrar sus
filas por SQL: cirugía de la tabla desde un script de monitorización — la
sonda única es más simple y no muta documentos reales. (b2) `--pool=solo`:
evitaría el fork, pero cambia la semántica de concurrencia del worker de
producción. (b3) `SimpleSpanProcessor` (export síncrono, sin hilo): fork-safe,
pero añade una llamada HTTP síncrona por span dentro de la tarea. (b4)
`os.register_at_fork` reseteando el provider global del SDK: requiere tocar
API privada de `opentelemetry.trace` (`_TRACER_PROVIDER`), frágil entre
versiones. `worker_process_init` es la solución canónica y la más limpia.

**Verificación.** Suite 181/181 (nuevo test de wiring de la señal); verificación
local `scripts/verifica_telemetria_local.py` 8/8 (productor + eager); smoke
sección 17 reescrita con sonda única y nombres reales — el `make smoke` contra
el stack es la prueba canónica end-to-end.

## D25 — Jaeger always-on + exporter collector → Jaeger (cierra el paso 5 de la Fase 5)

**Contexto.** D23 dejó Jaeger en el compose como servicio opt-in
(`profiles: ["ui"]`) y sin exporter en el collector: arrancaba solo bajo
demanda y, aun arrancado, no recibía nada (el collector solo exportaba a
`debug`). Era el «paso 5» pendiente de la Fase 5. Cerrarlo significa que
Jaeger arranque con el resto y reciba trazas de verdad, con verificación
automatizada de que las recibe.

**Decisión.** Dos cambios mínimos:

1. `infra/otel-collector-config.yaml`: nuevo exporter `otlp/jaeger`
   (endpoint `jaeger:4317`, `tls.insecure: true`) añadido al pipeline de
   trazas junto al `debug`. Collector y Jaeger comparten la red
   `intergraf_net`, así que el DNS `jaeger` resuelve desde el collector sin
   exponer más puertos al host.
2. `docker-compose.yml`: quitado `profiles: ["ui"]` del servicio `jaeger` —
   ahora arranca con `docker compose up` (`make up`) como los demás, y la
   sección 1 del smoke lo verifica como uno más de la lista de servicios.

**Verificación.** Smoke sección 17 +2 checks nuevos contra la API REST de
Jaeger (`/api/services` lista `faro-api` y `faro-worker`; `/api/traces?service=faro-api`
contiene `faro-worker` = una traza que cruza servicios, la propagación vista
desde Jaeger) → 48/48 esperados. Suite 183/183 (pin del YAML del collector y
de la ausencia de `profiles` en el compose). La UI en
http://localhost:16686 muestra la cascada HTTP → `apply_async/tasks.ingest_document`
→ `run/tasks.ingest_document` → httpx de embeddings, con el anidamiento
padre↔hijo entre los dos servicios.

**Alternativas descartadas.** Mantener Jaeger opt-in y solo añadir el
exporter: arrancar `--profile ui` para ver trazas es fricción innecesaria en
una demo de entrevista — el coste de Jaeger all-in-one (RAM) es asumible en
este stack y la verificación automatizada lo justifica. Exponer 4317/4318 de
Jaeger al host: innecesario, el collector habla con Jaeger por la red interna
`intergraf_net`. `depends_on` del collector → Jaeger: el exporter OTLP del
collector reintenta conexiones con backoff, así que no hace falta forzar el
orden de arranque (y así un Jaeger caído no tira el collector ni el smoke).

## Uso de IA generativa

Metodología: análisis del enunciado y planificación con IA; generación asistida
de esqueleto, configuración y tests; revisión línea a línea y verificación con
los tests automatizados antes de cada commit. Se completará al final con qué se
corrigió de lo generado por la IA.
