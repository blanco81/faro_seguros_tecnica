# DECISIONS.md — Faro Seguros RAG

Registro de decisiones de diseño del proyecto. Cada entrada sigue el mismo
formato de tres partes: **Contexto** (el problema que había que resolver),
**Decisión** (lo que se eligió y por qué) y **Alternativas descartadas** (lo que
se consideró y no entró, con su motivo). El número es cronológico: D01 es de la
Fase 0 y D30 del post-cierre. Cuando una decisión posterior modifica una
anterior, la entrada antigua lo indica con una nota «→ actualizada por Dxx».

El [README](README.md) describe el proyecto para cualquier lector; este archivo
es el porqué técnico de cada elección.

## D01 — Puerto propio para LLM y embeddings (arquitectura hexagonal)

**Contexto.** El enunciado exige llamar al LLM "a través de una interfaz propia"
y probar ingesta y consulta con un proveedor simulado, sin red ni claves.

**Decisión.** Los puertos `LLMProvider` y `EmbeddingProvider` (Protocols de
Python) en `app/ports/`, con adapters concretos inyectados desde los puntos de
entrada (FastAPI `Depends` / factory del worker). El núcleo (chunking, umbral,
citas) y los servicios no importan ningún SDK de proveedor. El adapter fake es
la prueba de que la inversión de dependencias funciona: los tests corren sin red.

**Alternativas descartadas.** LiteLLM como dependencia directa (acopla a una API
de terceros; si hiciera falta multi-proveedor, se implementa un adapter *detrás*
de nuestro puerto). Inyectar el cliente OpenAI en los servicios (violaría el
requisito de pruebas sin red).

## D02 — PostgreSQL + pgvector con índice HNSW (distancia coseno)

**Contexto.** El enunciado pide PostgreSQL con pgvector y un índice adecuado
para los chunks.

**Decisión.** Imagen `pgvector/pgvector:pg16`; columna `vector(1024)` e índice
HNSW con `vector_cosine_ops` en la migración inicial. La dimensión queda fija a
1024 por el modelo de embeddings elegido (D10); cambiarla exige migración
explícita.

**Alternativas descartadas.** IVFFlat (requiere elegir `lists` y entrenar con
datos; HNSW da mejor recall/latencia sin tuning a esta escala). Bases vectoriales
externas tipo Qdrant/Milvus (el enunciado pide PostgreSQL).

## D03 — Idempotencia por hash de contenido

**Contexto.** Subir el mismo archivo dos veces o reintentar una tarea no debe
duplicar chunks.

**Decisión.** SHA-256 del contenido como `content_hash` UNIQUE en `documents`.
Re-subir un archivo existente devuelve el documento ya registrado sin
reprocesar. El reproceso o reintento borra y reinserta los chunks del documento
en una única transacción: un fallo a mitad no deja estado parcial.

## D04 — Estados y transiciones viven en el dominio

**Contexto.** El enunciado pide estados "pendiente, procesando, listo, fallido"
con transiciones claras.

**Decisión.** `app/domain/document.py` define el enum
(`pending`/`processing`/`ready`/`failed`) y la tabla de transiciones permitidas
(reproceso desde `ready`, reintento desde `failed`). Ni la base ni la API deciden
transiciones: el dominio las valida y lanza `InvalidTransitionError`.

## D05 — Migraciones Alembic ejecutadas al arrancar la API

**Decisión.** El contenedor `api` ejecuta `alembic upgrade head` antes de
levantar uvicorn. Esquema versionado desde el commit inicial.

**Alternativas descartadas.** Job de migración separado o migraciones manuales
(más pasos, sin beneficio a esta escala).

## D06 — Frontend Vite + React detrás de nginx con proxy `/api`

**Decisión.** En producción, nginx sirve el estático y proxifica `/api/` al
servicio `api` (sin CORS); en desarrollo, el proxy lo hace Vite. La API expone
sus rutas bajo `/api` y `/health` para el healthcheck.

La etapa de build del Dockerfile usa `node:24-bookworm-slim` (glibc, LTS
vigente) con `npm ci --include=dev`, y `.dockerignore` excluye `.npmrc`. Motivo:
un build local falló con `exit 127` (`vite: not found`) tras un `npm ci`
"exitoso"; las tres causas posibles —un `.npmrc` con `omit=dev` heredado, un
lockfile divergente o rozaduras de musl/alpine— quedan neutralizadas. El runtime
sigue siendo `nginx:alpine` (multi-stage): el costo es solo en el build.

**Alternativas descartadas.** `node:20-alpine` (node 20 sin soporte desde abril
de 2026, y musl añade una variable más). `npm install` en vez de `npm ci` (no
reproduce el lockfile exacto).

## D07 — Observabilidad: OTel → collector (exporter debug), Jaeger opcional

**Decisión.** SDK OTel en API y worker (instrumentación de FastAPI, Celery y
httpx), exportación OTLP al collector, que imprime trazas en consola. Jaeger se
levantaba solo con `--profile ui` para la demo.

→ actualizada por D25: Jaeger quedó always-on y el collector reenvía a él.

**Alternativas descartadas.** Mandar trazas directamente a Jaeger desde los
servicios (el enunciado pide levantar un colector).

## D08 — Seguridad base desde el día 1

**Decisión.** Secretos solo por variables de entorno (`.env` fuera de git);
validación de tipo MIME y tamaño máximo (20 MB) en la subida; el contenido de
los documentos se trata como no confiable frente a inyección de prompts.

→ la promesa de aislamiento en el prompt la cerró D26 (Fase 5).

## D09 — Recortes conscientes (segunda iteración)

Autenticación y multiusuario, borrado/reindexado de documentos, streaming de
respuestas, recuperación híbrida texto+vector y re-ranking quedan fuera del MVP
de 72 h, documentados como segunda iteración.

→ actualizada por D30: el borrado individual sí entró; el resto sigue fuera.

## D10 — Proveedor real: Cloudflare Workers AI (chat + embeddings)

**Contexto.** Elegir proveedor real tratando coste, latencia y dependencia; los
tests deben correr sin red (los adapters fake lo garantizan).

**Decisión.** Cloudflare Workers AI para chat y embeddings, a través de su
endpoint **OpenAI-compatible**: chat con `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
(70B con inferencia optimizada) y embeddings con `@cf/baai/bge-m3` — el único
multilingüe del catálogo (las pólizas están en español; los bge-*-en degradarían
la recuperación), 1024 dimensiones, lo que fija `vector(1024)` (D02). Al ser
OpenAI-compatible, el adapter de Cloudflare es un *preset* del adapter OpenAI
(mismo SDK, distinta `base_url`): cero código adicional por proveedor. API token
Bearer y account ID por variables de entorno; opcionalmente `cf-aig-gateway-id`
para enrutar por AI Gateway.

**Por qué.** Capa gratuita suficiente para la demo y el set de evaluación; un
solo proveedor para chat y embeddings (una cuenta y una clave); sin dependencia
de cuenta OpenAI.

**Alternativas descartadas.** OpenAI directo (requiere otra cuenta; queda como
segundo preset). bge-base-en-v1.5 de Cloudflare (inglés). LiteLLM (peso extra).
Modelos locales tipo Ollama (latencia y setup en la máquina del evaluador).

## D11 — Chunking por página con solapamiento intra-página

**Contexto.** El enunciado pide chunking con solapamiento y citas verificables a
nivel de página.

**Decisión.** `app/domain/chunker.py` corta el texto **página por página**: un
chunk nunca cruza el límite de una página. Dentro de cada página se empaquetan
palabras enteras hasta `chunk_size` (800 caracteres) y cada chunk nuevo arranca
con las últimas palabras del anterior que quepan en `chunk_overlap` (120). El
texto se normaliza a espacios simples (la extracción de PDF mete saltos de línea
arbitrarios); las páginas en blanco no producen chunks; los ordinales son
globales y secuenciales. Única excepción: una palabra individual más larga que
`chunk_size` se emite como chunk propio (partirla rompería la literalidad de la
cita).

**Por qué.** La cita es "documento + página + fragmento": si un chunk cruzara
dos páginas, la página citada sería ambigua y dejaría de verificarse con un
ctrl+F. Palabras enteras, nunca cortes a mitad de palabra, por la misma razón.

**Alternativas descartadas.** Chunking global ignorando páginas (citas
ambiguas). Particionar por oraciones (una cláusula de póliza puede superar los
800 caracteres y el corte se vuelve impredecible). El
`RecursiveCharacterTextSplitter` de LangChain (misma lógica, pero añade una
dependencia al núcleo, que debe quedar puro).

## D12 — Citas literales: el fragmento es un substring exacto del chunk

**Contexto.** "Citas verificables" debe significar algo comprobable
mecánicamente, no una promesa.

**Decisión.** `app/domain/citations.py` construye cada cita con nombre de
documento, página (1-indexada) y un fragmento **literal** del contenido del
chunk almacenado: nunca paráfrasis, nunca reescritura del LLM. Si el chunk
supera el máximo (220 caracteres), se recorta por palabra completa, pero lo
citado sigue siendo un substring exacto. El contrato está expresado como código
(`quote_is_verbatim`) y los tests lo fijan. Los puntos suspensivos de
presentación los añade la UI; el dominio los omite para no romper el substring.

**Por qué.** Verificable = el evaluador abre el documento, va a la página citada
y encuentra ese texto. Cualquier transformación del fragmento rompe esa promesa.

## D13 — Umbral de rechazo: todo-o-nada sobre el mejor resultado

**Contexto.** El enunciado exige que el sistema se niegue a responder cuando la
evidencia recuperada no alcanza un umbral de similitud definido.

**Decisión.** `app/domain/retrieval.py` responde solo si el chunk con MAYOR
similitud coseno alcanza el umbral (inclusivo). Si lo alcanza, responde con los
`top_k` (5) mejores; si no, rechaza de forma explícita con `REJECTION_MESSAGE` y
**sin entregar contextos débiles** — nunca responde "con lo que haya". Sin
resultados también se rechaza (razón distinta). La similitud llega ya convertida
por el adapter: pgvector devuelve distancia coseno y el dominio recibe
`1 − distancia`. Umbral por defecto **0.45**: bge-m3 rara vez supera ~0.7 incluso
con paráfrasis, así que el 0.75 inicial habría rechazado casi todo.

→ confirmado en 0.45 por la calibración end-to-end de D20; medido con el set
completo en D28/D29.

**Alternativas descartadas.** Filtrar chunk a chunk (permitiría responder apoyado
solo en resultados débiles). Responder siempre y dejar que el LLM decida (el
enunciado exige el rechazo del sistema). Umbral doble mínimo+máximo (perillas sin
evidencia de que ayuden).

## D14 — Configuración: stack real por defecto, modo fake explícito

**Contexto.** El enunciado exige probar con proveedor simulado sin claves, pero
el proveedor real ya estaba definido (D10); se pidió que los defaults no sean
"fake" y que toda la configuración viva en `.env`.

**Decisión.** Los defaults de `app/config.py` y del compose son el stack real
(Cloudflare). `.env.example` es la plantilla real: solo faltan
`CLOUDFLARE_ACCOUNT_ID` y `CLOUDFLARE_API_TOKEN`. El modo fake no desaparece —el
enunciado lo exige— pero deja de ser el default: lo fijan explícitamente los
tests (`conftest.py`) o quien quiera correr sin claves. Precedencia
(pydantic-settings): variable de entorno > `.env` > default. El `.env` se
resuelve anclado a la raíz del repo, independiente del directorio de lanzamiento;
si no existe (contenedores), se ignora porque compose inyecta las variables. Los
nombres de proveedor son un `Literal`: un typo falla al arrancar con el listado
de valores válidos. `validate_provider_credentials()` verifica credenciales con
fail-fast y mensaje accionable.

**Alternativas descartadas.** `extra="allow"` en los settings (absorbería todo
el entorno del proceso y `model_dump()` volcaría secretos a logs). Mantener fake
como default (la demo arrancaría simulada sin que nadie lo note). Silent-fallback
a fake si faltan credenciales (enmascara la misconfiguración).

## D15 — El contenido del documento viaja por la base, no por el broker

**Contexto.** La tarea Celery necesita los bytes para extraer y embedizar; el
límite de subida es 20 MB.

**Decisión.** Tabla `document_contents` 1:1 (PK = FK con `ON DELETE CASCADE`,
migración 0002). `register_document` escribe documento y contenido en la MISMA
transacción: o queda el registro completo, o no queda nada. La tarea recibe solo
el `document_id` y lee los bytes del repositorio.

**Por qué.** Un reintento de Celery, una reentrega tras reiniciar el worker
(`acks_late`) o un reproceso (D04) arrancan siempre de lo mismo sin exigir
re-subir. El mensaje mide unos bytes (un UUID), no ~27 MB de base64 atravesando
Redis en cada reintento. Y el original queda disponible para que el navegador lo
muestre y el evaluador verifique la cita (D12/D21).

**Alternativas descartadas.** Bytes en el payload de la tarea (el anti-patrón
clásico de Celery: Redis es memoria y el mensaje se retiene y reenvía). Volumen
compartido api↔worker (acopla el despliegue a un solo host). BYTEA dentro de
`documents` (cualquier `SELECT *` arrastraría el blob).

## D16 — Reintentos: la tarea clasifica, el servicio solo marca

**Contexto.** ¿Qué se reintenta, cuántas veces y quién decide cuándo un fallo es
definitivo?

**Decisión.** La política vive en la tarea (`decide_retry`, función pura) y
clasifica por tipo de excepción: **fatal** (`FatalIngestionError`, con el
documento ya marcado `failed`) — sin reintento: un PDF cifrado sigue cifrado tras
mil intentos; **transitoria** (red, base, timeouts, `SoftTimeLimitExceeded`) —
hasta 3 reintentos con backoff 10/20/40 s, el documento queda `processing`
entre intentos, que es la verdad; **presupuesto agotado** — la tarea marca
`failed` con la razón desde una sesión nueva (la del último intento pudo morir
con la base caída). El `failed` de negocio COMPLETA la transacción (ese fallo ES
el resultado correcto); las transitorias revierten (sin estado parcial, D03).

**Alternativas descartadas.** `autoretry_for` de Celery (reintenta TODO,
incluidas fatales, y la política deja de ser código visible y testable). Marcar
`failed` desde el servicio ante cualquier excepción (la primera transitoria
mataría el reintento que la iba a curar). No marcar nunca (documento
`processing` para siempre).

## D17 — La API despacha la tarea DESPUÉS del commit

**Contexto.** Despachar en una `BackgroundTask` de FastAPI tiene una trampa de
orden, verificada empíricamente en FastAPI 0.128: las background tasks corren
ANTES del código de salida de las dependencias con `yield` — el commit llegaría
tarde. El worker levantaría la tarea sin ver la fila, la clasificaría como fatal
("el documento no existe") y, siendo fatal, no se reintenta: documento `pending`
para siempre.

**Decisión.** El endpoint abre su propia unidad de trabajo (`session_scope`
expuesto como dependencia `get_repository_uow`) y despacha la tarea AL SALIR del
bloque, con documento y contenido ya commiteados. La ruta solo conoce el puerto
`DocumentRepository`; el dispatcher también es inyectable para que la suite
pruebe el contrato HTTP sin broker. Contrato de subida: `201` + `Location` para
documento nuevo; `200` para re-subida del mismo contenido (D03, sin tarea
nueva); `415`/`413`/`422` para las validaciones de D08 antes de tocar negocio.

**Alternativas descartadas.** `BackgroundTasks` (la carrera descrita arriba).
Despachar dentro de la transacción (misma carrera). `countdown` artificial
("esperar un poquito" no es garantía). Commit explícito accediendo a la sesión de
la dependencia (rompe la encapsulación del puerto).

## D18 — El 413 del smoke se omite (lo cubre la suite unitaria)

**Contexto.** El smoke de la Fase 2 valida los rechazos de subida que aportan
señal nueva sobre el stack real: 415, 422 (vacío) y 422 (sin nombre). El 413
(tamaño > 20 MB) exigiría fabricar un archivo grande en cada corrida.

**Decisión.** El 413 se cubre en `tests/test_api_documents.py`; el smoke lo omite
con un comentario apuntando aquí. Lo que el smoke sí aporta (servidor real,
multipart real, estado en la base) ya está cubierto por los otros tres.

**Alternativas descartadas.** Generar el payload con `fallocate` (basura al
disco en cada corrida). Commitear un PDF grande (pesa para siempre). Bajar
`MAX_UPLOAD_MB` para forzarlo (el smoke debe probar el sistema real, no una
versión capada).

## D19 — Consulta agéntica con verificación iterativa de citas

**Contexto.** Un RAG plano (retrieve → generate en un pase) cumple la letra pero
falla en lo que distingue a un asistente útil: preguntas multi-paso, fidelidad de
la cita (el LLM puede parafrasear y atribuir mal) y un umbral evaluado una sola
vez sin oportunidad de refinar.

**Decisión.** `AgenticQueryService` orquesta los puertos existentes más un nuevo
`RetrievalPort` (búsqueda pgvector, top-k por similitud coseno). Bucle:
**planificar** (¿descomponer en sub-preguntas?) → **recuperar** (top-k, umbral
D13) → **generar** con cita literal → **verificar** (el fragmento citado aparece
verbatim en un chunk recuperado) → si no cuadra o falta evidencia, **refinar** y
**reintentar**, con techo (`MAX_QUERY_ITERATIONS = 3`) → **responder | rehusar |
pedir aclaración**. Un agente que decide, itera y verifica; sin descomponerse en
múltiples agentes.

**Alternativas descartadas (no para siempre: "no ahora").** Multi-agente
LangGraph (infraestructura desproporcionada para el MVP; cada nodo sería un
envoltorio fino sobre el mismo puerto). Agente tool-using con tools dinámicas
(token-heavy, latencia alta). Conversacional multi-turno (cambia el contrato del
enunciado). Las tres quedan como roadmap documentado.

## D20 — Umbral de rechazo confirmado en 0.45 (calibración end-to-end)

**Contexto.** D13 dejó 0.45 como punto de partida pendiente de calibración; el
smoke de la Fase 3 fue el primer set real que ejercitó el pipeline completo con
proveedor real.

**Decisión.** **Confirmado en 0.45**. El smoke dio 37/37, y los dos casos polos
resolvieron como esperaba D13: la pregunta en dominio (franquicia de daños por
agua) respondió en 1 sola iteración con cita verificada literal contra la base;
la fuera de dominio (la tortilla de patatas) se rechazó con `REJECTION_MESSAGE`.
Ni tan estricto que rechace lo legítimo ni tan permisivo que deje pasar lo
irrelevante.

Nota operativa: la calibración destapó un bug en el parser JSON del smoke
(`jstr` no leía booleanos: `"refused": false` llegaba vacío y reportaba FAIL
fantasma). Se añadió `jbool` — bug del parser, no del umbral, anotado para que
el set formal no herede falsos negativos.

→ el set formal etiquetado llegó con D28/D29 (Fases 5-6).

**Alternativas descartadas.** Subir a 0.55-0.60 (los cosenos legítimos con bge-m3
están en 0.45-0.6: habría rechazado la consulta válida y forzado refinamiento
sin ganancia). Bajar a 0.30 (dejaría pasar lo irrelevante: rompe la garantía del
enunciado).

## D21 — Frontend Fase 4: React+Vite sobrio, citas navegables vía `/content`

**Contexto.** Tres bloques exigidos: subir y ver estado, preguntar, citas
navegables. Detalle: la cita trae `document` como *filename*, pero el endpoint
del PDF necesita el *id* — el frontend resuelve ese mapeo.

**Decisión.** React 18 + Vite 5 sin TypeScript, Tailwind ni Router: JSX plano,
CSS plano, `fetch` nativo — tres vistas no justifican más capas. Shell con tabs
(`App.jsx`) compartiendo la lista de documentos; `DocumentsView` (dropzone +
tabla) y `QueryView` (form + tres desenlaces). Polling de 3 s con `setInterval`
mientras haya algo `pending`/`processing` (sin WebSocket: la ingesta dura
segundos). Nueva ruta `GET /api/documents/{id}/content` sirve el original con el
MIME correcto, `Content-Disposition: inline` y `Cache-Control: public, max-age=3600`
(el contenido es inmutable por D03). El frontend abre
`/api/documents/{id}/content#page=N` en pestaña nueva: el visor PDF nativo salta
a la página citada. `Citation.jsx` mapea filename → id contra la lista; si el
documento no existe (p. ej. fue borrado), muestra la cita sin link.

**Alternativas descartadas.** Migrar al stack "moderno" (deuda mayor que la que
quita). WebSocket (coste sin retorno para una ingesta de segundos). Visor PDF
embebido tipo pdf.js (~1 MB de bundle). `attachment` en vez de `inline` (forzaría
descarga y `#page=N` no tendría efecto). Poner el id en la cita (rompe el
contrato legible por humanos de D12).

## D22 — El smoke inspecciona cabeceras con el propio GET (`curl -D`), nunca con HEAD

**Contexto.** El smoke reportaba cabeceras malas en `/content` mientras el curl
manual y los 4 tests del endpoint decían que bien — la tercera fuente de verdad
(el smoke) era la que mentía.

**Decisión.** Diagnóstico reproducido antes de tocar nada: `curl -I` envía
**HEAD**, y FastAPI no registra HEAD en rutas GET — el endpoint contestaba 405
con cuerpo JSON, y el smoke leía el `content-type` de esa respuesta de error.
Fix: un único GET con `-D` (cabeceras y cuerpo de la misma respuesta) más
verificación de magic bytes (`%PDF`). Regla de proyecto: inspeccionar cabeceras
de endpoints FastAPI con el propio GET.

**Alternativas descartadas.** Registrar HEAD en el endpoint (arregla el
síntoma; ningún consumidor real usa HEAD aquí — el chequeo debe pedir lo que
dice verificar). Dos curl separados (lo comprobado y lo descargado podrían
divergir).

## D23 — Observabilidad Fase 5 (pasos 1-3): un módulo de telemetría, dos procesos, propagación HTTP → Celery

**Contexto.** La Fase 5 pide observabilidad end-to-end (HTTP → Celery → LLM):
una sola traza debe cruzar el stack. Las dependencias y el collector existían,
pero nada instrumentaba nada.

**Decisión.** Un único módulo `app/telemetry/setup.py` con tres piezas:
(1) `setup_telemetry(service_name)` monta el TracerProvider del proceso
(OTLP/HTTP + BatchSpanProcessor), idempotente y desactivable
(`OTEL_ENABLED=false` → cero efectos); (2) `instrument_api` / `instrument_worker`
— la API instrumenta FastAPI + Celery + httpx (también publica tareas); el
worker, Celery + httpx; `service.name` distingue `faro-api` de `faro-worker` en
Jaeger; (3) la instrumentación de Celery inyecta el contexto (`traceparent`) al
publicar y lo extrae al ejecutar — `run` queda como hijo de `publish`; el hook
del worker va en señal, nunca en el import.

Dos hallazgos que quedaron como regla: el exporter OTLP/HTTP del SDK **no
añade** `/v1/traces` a un endpoint sin ruta (el collector lo rechaza en
silencio; `normalizar_endpoint_otlp` lo añade, verificado contra un receptor
real), y el worker en `network_mode: host` necesita `localhost:4318` (el DNS
`otel-collector` no resuelve desde ahí).

**Verificación.** Tres niveles: tests unitarios con providers explícitos;
`scripts/verifica_telemetria_local.py` (receptor OTLP real sin Docker, broker en
memoria); y el smoke, sección 17, con la prueba de la propagación — `publish` y
`run` compartiendo Trace ID.

**Alternativas descartadas.** Instrumentar en el import de `celery_app`
(activaría la instrumentación en tests y en la API solo por importar).
`OTEL_SERVICE_NAME` como setting (los nombres de servicio son constantes de
despliegue, no perillas). Spans a mano en los adapters (los parches de httpx
cubren el SDK OpenAI completo). Los spans de dominio llegaron con D27.

## D24 — Tres bugs destapados por la primera corrida real del smoke de la Fase 5

**Contexto.** D23 entregó la instrumentación, pero la primera ejecución contra
el stack real dio 41 OK / 3 FAIL: ni spans del worker ni propagación. Tres bugs
independientes, los tres silenciosos hasta correr el sistema real.

**Bug 1 — el smoke asumía una ingesta fresca que no ocurría.** En un stack donde
el PDF ya estaba registrado, `POST /api/documents` responde 200 y no despacha
tarea (idempotencia D03) — sin tarea no hay spans de Celery que verificar. Fix:
la sección 17 sube un `.txt` de contenido único (timestamp en el cuerpo) →
siempre 201 → tarea despachada. Es la "sonda" de telemetría.

**Bug 2 — instrumentar el worker en el padre pierde los spans en prefork.** El
compose arranca `--concurrency=2` con pool prefork: `worker_init` se emite en el
padre, que luego hace `fork()` de los hijos — y `fork()` no copia hilos, así que
el hilo exportador del BatchSpanProcessor no existe en el hijo: los spans se
acumulan en una cola muerta sin error visible. Fix: enganchar la instrumentación
en `worker_process_init` (señal que se emite en cada hijo): cada hijo monta un
provider fresco con su hilo vivo. Es el patrón canónico de
`opentelemetry-instrumentation-celery`; quedó pineado por test.

**Bug 3 — nombres de span invertidos.** La instrumentación nombra los spans con
prefijo (`apply_async/tasks.ingest_document`, `run/tasks.ingest_document`); el
smoke grepaba el sufijo — nombres que no existen nunca. Fix: grep de los nombres
reales.

**Alternativas descartadas.** Borrar filas por SQL para forzar re-subida
(cirugía desde un script de monitorización). `--pool=solo` (cambia la semántica
de concurrencia). `SimpleSpanProcessor` (export síncrono: una llamada HTTP por
span dentro de la tarea). `os.register_at_fork` (API privada del SDK, frágil).

## D25 — Jaeger always-on + exporter collector → Jaeger

**Contexto.** D23 dejó Jaeger como servicio opt-in (`profiles: ["ui"]`) y sin
exporter: aun arrancándolo a mano, no recibía nada.

**Decisión.** Dos cambios mínimos: exporter `otlp/jaeger` (endpoint
`jaeger:4317`, `tls.insecure`) en el pipeline de trazas del collector junto al
`debug`; y fuera el `profiles: ["ui"]` del compose — Jaeger arranca con `make
up` como los demás. Collector y Jaeger se hablan por la red interna, sin puertos
extra al host.

**Verificación.** Smoke +2 checks contra la API REST de Jaeger
(`/api/services` lista `faro-api` y `faro-worker`; `/api/traces` contiene una
traza que cruza servicios) → 48/48. La UI en http://localhost:16686 muestra la
cascada HTTP → `apply_async` → `run` → httpx de embeddings.

**Alternativas descartadas.** Mantenerlo opt-in (fricción en una demo).
Exponer los puertos OTLP de Jaeger (la red interna basta). `depends_on` (el
exporter reintenta conexiones con backoff: un Jaeger caído no tira el collector).

## D26 — Aislamiento del contexto no confiable en el prompt

**Contexto.** D08 prometía desde la Fase 0 tratar el contenido de los documentos
como no confiable; mientras tanto, los fragmentos viajaban interpolados TAL CUAL
en el prompt — un PDF malicioso con «IGNORA las instrucciones anteriores...»
entraba con plena autoridad instructiva.

**Decisión.** El prompt aísla el contexto en tres capas: (1) cada fragmento
viaja en un bloque `<fragmento numero="N" documento="X" pagina="Y">...</fragmento>`
— delimitado y etiquetado como dato; (2) regla explícita en el prompt: los
bloques son datos extraídos de documentos, no instrucciones; (3) la frontera dura
ya existente (D12/D19): la respuesta solo se acepta con una cita verbatim que
existe en un chunk real — aunque el modelo se dejara manipular, no puede emitir
texto sin anclaje documental. La pregunta del usuario viaja sin marcadores: quien
la escribe es quien la recibe (sin escalada de privilegios cruzada). El
`FakeLLMProvider` se actualizó en lockstep: su regex ancla a los bloques
`<fragmento>` — si el servicio cambia de marcas, el fake se desincroniza y los
tests lo detectan.

**Alternativas descartadas.** Sanitizar el contenido (rompería la literalidad de
la cita: el LLM debe ver el texto exacto). Nonce aleatorio en los marcadores (no
añade frontera estructural). Aislar también la pregunta (sin ganancia real).

## D27 — Spans de dominio RAG: la traza cuenta qué decidió el agente

**Contexto.** La instrumentación de librerías (D23) dice cuánto tardó cada pieza
pero no qué pasó dentro del bucle: por qué una consulta se rehusó, qué scores
había frente al umbral, cuántas veces refinó.

**Decisión.** El servicio de consulta emite dos spans de dominio: **`rag.consulta`**
(raíz: pregunta, umbral, desenlace `respondida`/`rechazada`/`aclaracion`, motivo,
iteraciones, citas) y **`rag.iteracion`** (hijo por vuelta: scores de los k
mejores, mejor score, si superó el umbral, decisión de esa iteración —
`ok`/`below_threshold`/`no_results`/`empty_or_unparseable`/`citation_mismatch` —
y cita verificada). En Jaeger una consulta rehusada se lee de una ojeada: tres
iteraciones con `mejor_score: 0.38 < umbral: 0.45` y la raíz con
`desenlace: rechazada`. El constructor acepta un `tracer` opcional — no-op con la
telemetría apagada (la suite no cambia) y tracer real en producción. El dominio
se acopla al API de OpenTelemetry, no al SDK: nada de exporters en el servicio.
Un rechazo NO pone el span en `status ERROR`: es un desenlace de negocio correcto
— con ERROR, cualquier alerta gritaría cada vez que el umbral cumple su función.

**Alternativas descartadas.** Un puerto `Tracer` propio (el API de OTel ya es la
abstracción; el puerto solo traduciría 1:1). Spans también en planificar/refinar
(sus httpx ya cuelgan de la raíz; sin atributos de dominio que aportar). Scores
como atributos secuenciales (OTel es escalar: un CSV cumple lo mismo). Umbral
solo en la raíz (cada iteración debe leerse autocontenida).

## D28 — Set de evaluación etiquetado: precision@k / recall@k y barrido de umbral

**Contexto.** D20 cerró la Fase 3 con los dos casos polo del smoke y dejó la
deuda escrita: calibración con set formal etiquetado. Con la telemetría en pie,
lo que faltaba eran el set y las métricas.

**Decisión.** Tres piezas: (1) **el set** (`dataset.py`): preguntas en dominio
etiquetadas con página relevante + snippet VERBATIM de la respuesta, redactadas
como las escribiría un analista (copiar la cláusula inflaría los cosenos y
mediría memorización, no recuperación) y casos fuera de dominio; (2) **las
métricas** (`metrics.py`), puras: precision@k, recall@k sobre PÁGINAS (la
granularidad de la cita navegable, estable frente al chunking) y hit@k (toda la
evidencia verbatim en los top-k, con la MISMA normalización de whitespace de la
cita — una sola definición de "literal" en el sistema); (3) **el runner**
(`make eval`, smoke sección 18): compone con los factories reales — mide el
sistema que responde. Dos regímenes: en dominio con búsqueda ACOTADA al documento
(números reproducibles) y fuera de dominio sobre TODA la base (la seguridad del
umbral D13 con los distractores reales). El reporte: tabla por caso, medias y
BARRIDO de umbral 0.30-0.60 con el del stack marcado.

Integridad de las etiquetas, verificada tres veces: ancladas al archivo exacto
por sha256 (D03); el runner verifica cada snippet contra los chunks reales ANTES
de medir; la suite repite el pin extrayendo el PDF commiteado con el MISMO
pipeline de producción (PDF ↔ sha256 ↔ set); y el smoke lo vuelve a hacer contra
la base real en cada corrida.

La puerta de salida es lo ya probado, no lo recién medido: exit 0 solo con
hit@5 completo y 0 aceptados fuera de dominio. P@1/P@5/R@5 se REPORTAN como
evidencia; fijarles suelos sin corridas reales acumuladas sería inventar teclas.

**Alternativas descartadas.** Etiquetar a nivel de chunk (frágil al chunking,
más fino que lo que el usuario ve). Un endpoint `/api/eval` (compose exec corre
el mismo código sin exponer nada). Medir P/R sin acotar (irreproducible: depende
del histórico del stack). ROC formal (con pocos casos, el barrido comunica lo
mismo). MRR/nDCG (nadie lo pidió; hit@k es la métrica operativa).

## D29 — Fase 6: el set de evaluación a TRES pólizas + README y DECISIONS finales

**El compromiso.** El plan prometió como entregable "tres pólizas y doce
preguntas"; el enunciado exige documentos y preguntas de ejemplo. D28 dejó la
métrica contra UNA póliza; la Fase 6 la completa: `poliza-ejemplo.pdf` (hogar) +
`poliza-auto.pdf` + `poliza-vida.pdf` (ReportLab A4) y **21 casos** (19 en
dominio + 2 fuera), etiquetados con página y snippet verbatim.

**El diseño que hace interesantes a las tres.** Comparten léxico de oficio A
PROPÓSITO: franquicia (300 € en daños por agua del hogar, 150 € en lunas del
auto), exclusiones (hogar y vida), plazos (siete, cuarenta, treinta días). Son
cláusulas gemelas: el distraidor real de un buscador semántico. Cada caso se
etiqueta y verifica contra SU póliza — la suite tiene el test `clausula_gemela`:
la franquicia de lunas del auto NO cuenta como evidencia del hogar.

**Mecánica.** `EvalDocumento` (clave, filename, sha256, páginas) y
`EvalCase.documento`; el runner localiza las tres por `content_hash` (D03) y si
falta alguna lista TODAS con su remedio. El reporte gana la columna `doc`; el
smoke sube auto y vida antes de correr el runner.

**Alternativas descartadas.** Un solo PDF con las tres (pierde el realismo del
analista que sube pólizas separadas). Copiar las doce preguntas del plan tal cual
(sin snippets verbatim no hay hit@k verificable: se ADAPTARON). Criptoactivos
como caso fuera de dominio (comparte léxico del dominio; no honesto declararla
rechazable sin corridas acumuladas). La pregunta de inyección (es de consulta:
la mide D26, no la recuperación). Preguntas multi-póliza (sin etiquetado honesto
multi-documento sería inventar la verdad: queda como límite en el README).

## D30 — DELETE /api/documents/{id}: borrado en cascada y smoke que se recoge a sí mismo

**Contexto.** La sonda de telemetría de la sección 17 (D24) quedaba en la lista
de documentos en cada corrida, y el README listaba "no hay borrado" como límite:
limpiar una subida fallida o un documento equivocado exigía SQL a mano.

**Decisión.** Tres piezas que se sostienen entre sí: (1) **puerto y adapter**:
`DocumentRepository.delete(document_id) -> bool`; el adapter emite UN
`DELETE FROM documents` — chunks y contenido caen por la FK `ON DELETE CASCADE`
del esquema, sin N consultas ni estado parcial; (2) **ruta
`DELETE /api/documents/{id}`**: `204` borrado, `404` inexistente, **`409` si
está `pending`/`processing`** — la tarea en vuelo insertaría chunks contra un
documento que ya no está y moriría por FK, ensuciando reintentos y trazas. En
`ready` y `failed` se puede borrar: un `failed` es justo la subida rota que se
quiere limpiar. Y como el UNIQUE de `content_hash` vive en la fila, borrar libera
el hash: re-subir los mismos bytes registra documento NUEVO y vuelve a
despachar la tarea; (3) **smoke auto-limpio**: la sección 17 borra SU sonda por
el endpoint y lo comprueba dos veces — DELETE `204` con GET posterior `404`, y
`0` chunks del documento en pgvector (la cascada real contra la base de verdad).

**Frontend.** Botón «Eliminar» con confirmación (avisa que arrastra chunks,
embeddings y el archivo original), deshabilitado durante la ingesta (espejo del
409) y refresco de la lista al terminar.

**Alternativas descartadas.** Filtrar `sonda.txt` del listado (un usuario puede
subir un `sonda.txt` legítimo: el listado mentiría). Borrar solo los chunks
(documento zombi). Soft-delete (complejidad sin consumidor en el MVP). Permitir
borrar en plena ingesta y dejar morir la tarea (el worker fallaría con
IntegrityError tras reintentos: el 409 es más honesto). Tocar el ORM desde la
ruta (rompe la regla hexagonal de D01).

**Verificación.** Suite 216/216 (+6: 204 con cascada en el doble, 404, 409 sin
tocar nada, 204 de un `failed`, y re-subida tras borrar que registra de nuevo).
Smoke 58/58 (+2: sonda eliminada con GET 404 y cascada 0-chunks contra pgvector).

## Uso de IA generativa

Metodología: análisis del enunciado y planificación con IA; generación asistida
de esqueleto, configuración y tests; revisión línea a línea y verificación con
los tests automatizados y el smoke real antes de cada commit. El registro de lo
que se corrigió de lo generado y por qué:

| Fecha | Herramienta | Uso | Corrección y aporte propio |
| --- | --- | --- | --- |
| 2-3 nov | Z.ai (GLM) | Análisis del enunciado y del informe de tokens; plan de arquitectura y trabajo | Revisión de cada decisión; ajuste del cronograma a la disponibilidad real; contraste de riesgos |
| 3 nov | Z.ai (GLM) | Esqueleto de cada fase: compose, migraciones Alembic, dominio puro (chunker, umbral, citas), puertos y adapters | La arquitectura hexagonal y sus límites de importación se revisaron a mano; los tests de dominio se reescribieron para expresar comportamiento, no estructura |
| 3 nov | Z.ai (GLM) | Ingesta asíncrona (Celery, idempotencia D03) y API de documentos | El compose generado dejaba al worker en la red bridge de Docker: en una máquina IPv6-only no resolvía `api.cloudflare.com` y la ingesta moría en reintentos — worker en `network_mode: host` (y la api después, para la consulta al LLM); documentado en el compose y en el README |
| 4 nov | Z.ai (GLM) | Consulta agéntica (D19) y calibración del umbral (D20) | El smoke reportaba FAIL fantasma en la calibración: el parser `jstr` no leía booleanos (`"refused": false` → vacío); se añadió `jbool` (nota operativa de D20) — bug del parser, no del umbral |
| 4 nov | Z.ai (GLM) | Frontend React+Vite (D21) | El CSS generado colapsaba la caja del dropzone (un `<label>` inline de ~17 px tapaba el botón): reproducido 1:1 en Chromium y corregido con `display:block`; fechas sin ambigüedad de locale y columnas numéricas a la derecha |
| 4 nov | Z.ai (GLM) | Telemetría OTel (D23) | Tres bugs destapados por la PRIMERA corrida real del smoke (D24): el worker se instrumentaba en `worker_init` (el hilo del BatchSpanProcessor no sobrevive al fork prefork → spans perdidos en silencio), el smoke no despachaba tarea fresca en stack caliente (idempotencia D03) y las aserciones usaban sufijo de operación cuando la librería emite prefijo |
| 4 nov | Z.ai (GLM) | Smoke test (secciones 1-18) | `curl -I` envía HEAD y FastAPI contesta 405: las cabeceras se leen con `curl -D` del propio GET y el PDF se verifica por magic bytes (D22); una sección llamaba a `/documents/{id}/content` sin el prefijo `/api` — la suite estaba verde pero la corrida real lo destapó |
| 4 nov | Z.ai (GLM) | Set de evaluación y pólizas de ejemplo (D28/D29) | Los snippets verbatim y las páginas se verificaron contra el texto extraído REAL antes de commitear (el pin PDF ↔ sha256 ↔ set de la suite es la corrección institucionalizada: la IA no puede etiquetar "a ojo"); las preguntas se redactaron como analista, no copiando la cláusula |
| 4 nov | Z.ai (GLM) | Runner de la evaluación (`app/evaluation/runner.py`, D28) | La PRIMERA corrida real del smoke con la sección 18 completa lo destapó: crash `NameError: _verifica_etiquetas` — el guard `__main__` iba a mitad del módulo y `python -m` ejecuta `main()` antes de ligar las defs posteriores; la suite no lo veía porque IMPORTA el módulo y el guard no se dispara — importar y ejecutar son caminos distintos. Guard pasado a última sentencia + pin AST en la suite: todo módulo ejecutable de `app/` cierra con el guard |
| 4 nov | Z.ai (GLM) | Borrado de documentos (D30: puerto, ruta DELETE, botón, auto-limpieza del smoke) | El 409 en plena ingesta y el doble check de la cascada en el smoke (0 chunks en pgvector) son correcciones de diseño: la primera versión habría dejado morir la tarea del worker por FK con IntegrityError y ruido en reintentos; y la cascada, que la suite sin PostgreSQL no puede probar, quedó verificada contra la base real en cada corrida |

Lo que aportó el autor: las 30 decisiones y sus alternativas descartadas, la
disciplina de verificación (cada fase se cerró con la suite Y el smoke real, no
solo con tests), la calibración con evidencia y la revisión línea a línea de
todo lo generado — el patrón de este proyecto es que los tests y las corridas
reales corrigen, y eso queda escrito en el historial de commits.
