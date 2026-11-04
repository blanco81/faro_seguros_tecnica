# Faro Seguros · Asistente RAG con citas verificables

**Faro Seguros RAG** es un asistente para analistas de siniestros. El analista sube
pólizas y anexos (PDF o texto plano), el sistema las procesa en segundo plano y, a
partir de ese momento, responde preguntas en lenguaje natural **con citas
verificables**: cada respuesta indica de qué documento, de qué página y con qué
fragmento literal se sostiene.

La regla que ordena todo el proyecto: **si la evidencia no alcanza, el sistema se
calla**. Antes que inventar una respuesta apoyada en contexto débil, rechaza de
forma explícita. Para un analista de seguros, un asistente que inventa es peor que
uno que admite no saber.

> Prueba técnica TopDevCrew · Ingeniero AI Full Stack Semi-Senior
> Estado: **entrega completa — Fases 0 a 6 cerradas, más el post-cierre (D30)**.
> 216 tests automatizados · smoke end-to-end de 18 secciones / 58 comprobaciones.

Dos términos del título, explicados:

- **RAG** (Retrieval-Augmented Generation, generación aumentada por recuperación):
  en lugar de responder solo con lo que el modelo "sabe", primero se **buscan los
  fragmentos relevantes** entre los documentos subidos y se entregan al modelo
  como contexto. El modelo redacta apoyado en esa evidencia.
- **Cita verificable**: la respuesta no solo menciona su fuente — el fragmento
  citado aparece **literalmente, carácter por carácter**, en el documento original.
  Cualquiera puede abrir el PDF en la página citada y comprobarlo con un ctrl+F.

## El stack, en una línea

FastAPI (API) · Celery + Redis (procesamiento en segundo plano) · PostgreSQL 16 +
pgvector (base de datos con búsqueda vectorial) · Alembic (migraciones de esquema)
· React + Vite (frontend) · OpenTelemetry + Jaeger (observabilidad) · Docker
Compose (los siete servicios, un comando).

## Cómo funciona

Dos flujos explican todo el sistema: qué pasa cuando se sube un documento
(ingesta) y qué pasa cuando se hace una pregunta (consulta).

### El flujo de la ingesta — del PDF a la base vectorial

```
 (1) SUBIR                    (2) VALIDAR Y GUARDAR
 Analista ──PDF/txt──►  API FastAPI ──► PostgreSQL
 (arrastrar o botón)     tipo, tamaño y     documento + contenido original,
                         nombre válidos     estado: pending
                                │
                                │ 3. hecho el commit del registro,
                                │    la API encola la tarea
                                ▼
 (4) PROCESAR EN SEGUNDO PLANO — Worker Celery
     lee el contenido de la base ─► extrae el texto (pypdf o texto plano)
     ─► corta en fragmentos por página (≈ 800 caracteres, 120 de solape)
     ─► convierte cada fragmento en un embedding (1024 números)
     ─► guarda los fragmentos + embeddings en PostgreSQL + pgvector
                                │
                                ▼
 (5) LISTO — estado: ready
     el listado del frontend se refresca solo (consulta cada 3 segundos
     mientras haya algo en proceso)
```

Cada paso, en palabras:

1. **Subir.** El frontend ofrece una zona de arrastre y el botón clásico de
   archivo. Acepta PDF y texto plano, hasta 20 MB.
2. **Validar y guardar.** La API comprueba tipo, tamaño y nombre **antes** de
   tocar negocio (errores claros: 415 tipo no permitido, 413 demasiado grande,
   422 sin nombre o archivo vacío). Documento y contenido original se guardan
   juntos en PostgreSQL, en la misma transacción: o queda el registro completo,
   o no queda nada.
3. **Encolar.** La tarea de procesamiento se despacha **después** de confirmar
   el registro en la base — si fuera antes, el worker buscaría una fila que aún
   no existe. El mensaje viaja ligero (solo el identificador del documento): el
   contenido ya está a salvo en la base, no en la cola.
4. **Procesar.** El worker — un proceso Celery independiente — extrae el texto,
   lo corta y lo convierte en **embeddings**. Un *embedding* es la representación
   numérica del significado de un texto: una lista de 1024 números con la
   propiedad de que dos textos de significado parecido quedan con números
   parecidos. Buscar "por significado" es comparar esos números.
   El corte (***chunking***) respeta los límites de página — así una cita puede
   nombrar una página concreta — y cada fragmento arranca repitiendo las últimas
   palabras del anterior (120 caracteres de solape) para no partir ideas a medias.
5. **Listo.** El documento pasa a `ready` y el listado del frontend se refresca
   solo.

Dos propiedades que sostienen el flujo:

- **Idempotencia.** Subir el mismo archivo dos veces no duplica nada: el sistema
  guarda una huella SHA-256 del contenido y la re-subida devuelve el documento ya
  registrado sin reprocesarlo.
- **Reintentos honestos.** Si el procesamiento falla por algo transitorio (red,
  base de datos), se reintenta hasta 3 veces con esperas crecientes (10, 20 y 40
  segundos) mientras el documento figura `processing` — que es la verdad. Los
  fallos definitivos (p. ej. un PDF cifrado) marcan `failed` con su motivo a la
  primera, sin reintentar en vano.

### El flujo de la consulta — de la pregunta a la cita

```
 (1) PREGUNTAR
 Analista ──pregunta──► POST /api/query ──► AgenticQueryService
                                                    │
 (2) BUSCAR — hasta 3 iteraciones ──────────────────┤
     embedding de la pregunta ─► pgvector devuelve  │
     los 5 fragmentos más parecidos por significado │
                                                    ▼
 (3) DECIDIR con el umbral 0.45
     mejor similitud ≥ 0.45 ──► continúa
     mejor similitud <  0.45 ──► RECHAZO explícito (no responde)
     sin resultados         ──► RECHAZO (motivo distinto)
                                                    │
 (4) GENERAR Y VERIFICAR
     el LLM redacta la respuesta citando un fragmento LITERAL;
     se comprueba que ese fragmento existe, tal cual, en un
     fragmento real de la base
     ✓ cuadra    ──► responder con la cita
     ✗ no cuadra ──► refinar la búsqueda y volver a (2)
                                                    │
                                                    ▼
 (5) RESPUESTA + CITA = documento, página, fragmento literal
     clic en la cita ──► abre el PDF original en pestaña nueva,
     exactamente en la página citada (#page=N)
```

Lo esencial:

- **El umbral 0.45** es la barra mínima de similitud entre la pregunta y el mejor
  fragmento encontrado. Se decidió sobre el comportamiento real del modelo de
  embeddings y se calibró con un set de evaluación de 21 preguntas. Si ni el
  mejor fragmento la alcanza, no hay respuesta: el sistema rechaza **sin
  entregar contexto débil** — nunca responde "con lo que haya".
- **El bucle agéntico.** El servicio de consulta es un agente acotado: planifica
  (¿conviene reformular o descomponer la pregunta?), recupera, genera y verifica;
  si la cita no cuadra o la evidencia no alcanza, refina y reintenta, con techo
  de 3 iteraciones. Un solo agente con un trabajo claro, no una multitud.
- **La verificación es la frontera dura.** El fragmento citado debe aparecer
  *verbatim* en un fragmento real de la base. Aunque el modelo se equivocara o
  alguien intentara manipularlo con el contenido de un documento, no puede emitir
  texto sin anclaje documental verificado.

### Los tres desenlaces

Toda consulta termina en uno de tres estados, y el frontend los muestra tal cual:
**respondida** (respuesta + citas navegables), **rechazada** (mensaje fijo que
explica que la evidencia no alcanza) o **aclaración** (el agente pide concretar
la pregunta cuando es ambigua).

## Las fases del proyecto

Siete etapas más un post-cierre. Cada fase se cerró con su verificación: la
suite de tests **y** una corrida real del stack con `make smoke` — el patrón del
proyecto es que los tests y las corridas reales se corrigen mutuamente.

| Fase | Qué cerró | Verificación |
| --- | --- | --- |
| 0 | Esqueleto: monorepo, servicios, esquema, CI | stack arriba + CI verde |
| 1 | Dominio puro: chunking, umbral, citas | tests unitarios del dominio |
| 2 | Ingesta asíncrona idempotente | smoke con ingesta real |
| 3 | Consulta agéntica, umbral calibrado | smoke 37/37 |
| 4 | Frontend con citas navegables | smoke 40/40 |
| 5 | Observabilidad + evaluación de recuperación | smoke creciendo 48 → 55 |
| 6 | Cierre: 3 pólizas, 21 preguntas, límites y registro de IA | smoke 55/55 |
| Post-cierre | Borrado de documentos (D30) | smoke 58/58 |

**Fase 0 — el esqueleto que sostiene todo.** Monorepo con el `docker-compose`
de siete servicios (PostgreSQL con pgvector, Redis, la API FastAPI, el worker
Celery, el frontend tras nginx, el collector de OpenTelemetry y Jaeger), los
modelos de datos con sus migraciones Alembic — la base ya nace con su columna
`vector(1024)` y su índice HNSW, el tipo de índice que acelera la búsqueda de
los vectores más parecidos sin afinar parámetros — y el CI de GitHub Actions,
que en cada push corre lint y tests del backend (Python 3.12) y el build del
frontend (Node 24). Aquí se fijó la decisión estructural del proyecto:
arquitectura hexagonal, con el negocio en el centro y los frameworks en la
periferia.

**Fase 1 — el dominio puro.** Las reglas del negocio en Python plano, sin
frameworks: el cortador de fragmentos por página, el umbral de rechazo, la
construcción y validación de citas literales, y los estados del documento con
sus transiciones permitidas (ningún salto arbitrario: cada cambio legal está
en una tabla que el dominio valida y aplica). El cortador fija aquí sus dos
reglas de oro — nunca cruzar páginas (la cita debe poder nombrar una) y nunca
partir palabras (la cita debe ser literal) — y el umbral se define como
decisión de todo-o-nada sobre el mejor resultado: si la evidencia más parecida
no convence, no hay respuesta. Al no depender de nada, esta capa es la más
fácil de probar y la más difícil de romper.

**Fase 2 — la ingesta asíncrona.** La subida deja de procesar en el momento y
pasa a un worker Celery: validaciones en la puerta (tipo, tamaño, nombre),
idempotencia por huella SHA-256, contenido guardado en la base y no en la cola,
despacho de la tarea solo después del commit, y una política de reintentos que
distingue fallos transitorios de definitivos. El analista ve el estado avanzar
(`pending` → `processing` → `ready`/`failed`) con el motivo del fallo cuando lo
hay. El contrato HTTP queda fijado: `201` con la ubicación del documento nuevo,
`200` si el contenido ya estaba registrado, y el listado y el estado individual
con páginas, fragmentos y motivo de fallo.

**Fase 3 — la consulta agéntica.** El corazón del asistente: recuperación por
similitud en pgvector, el bucle planificar → recuperar → generar → verificar →
refinar con techo de 3 iteraciones, el rechazo explícito del sistema y la
calibración del umbral en 0.45 verificada de punta a punta con el proveedor real.
Fue la primera fase cerrada con el smoke completo en verde (37/37), con los dos
casos polos resueltos como debían: la pregunta en dominio respondida con cita
literal verificada contra la base, y la fuera de dominio rechazada sin pestañear.

**Fase 4 — el frontend.** React + Vite deliberadamente sobrio: zona de subida
(arrastrar o botón), listado con refresco automático cada 3 segundos mientras
algo se procesa, consulta con los tres desenlaces bien diferenciados y citas
navegables — el clic abre el PDF original en pestaña nueva, en la página exacta,
servido por la propia API con cache de una hora (el contenido es inmutable:
mismo hash, mismos bytes). Se evitó a propósito apilar TypeScript, Tailwind o
un visor PDF embebido: tres vistas no justifican esa deuda.

**Fase 5 — observabilidad y evaluación (la más larga).** Cinco piezas: la
telemetría OpenTelemetry en los dos procesos (API y worker) con propagación de
contexto — una sola traza cruza HTTP → Celery → llamadas al modelo; Jaeger
siempre encendido para ver esa traza con interfaz gráfica; **spans de dominio**
`rag.consulta` y `rag.iteracion` que cuentan qué decidió el agente (scores
frente al umbral, iteraciones, desenlace); el aislamiento del contenido de los
documentos dentro del prompt (los fragmentos viajan marcados como datos, nunca
como instrucciones — defensa frente a inyección); y el set de evaluación con
precision@k, recall@k y hit@5 más el barrido de umbral. La primera corrida real
destapó tres bugs que ningún test había visto — documentados en DECISIONS.md
(D24) — y esa es exactamente la lección de la fase.

**Fase 6 — el cierre.** El set de evaluación completo con las **tres pólizas**
del entregable (hogar, auto y vida) y 21 preguntas: 19 en dominio, cada una con
su página y fragmento de evidencia, y 2 fuera de dominio. La evaluación mide
precision@k (de los k fragmentos devueltos, cuántos eran la evidencia),
recall@k (de la evidencia que existía, cuánta apareció en los top-k) y hit@5
(si la evidencia literal está entre los cinco primeros), y además barre el
umbral de 0.30 a 0.60 mostrando qué aceptaría y qué dejaría pasar en cada
punto. Las tres pólizas comparten vocabulario de oficio a propósito — franquicias, exclusiones, plazos —
para que el distractor sea real, como en la vida de un buscador semántico. Se
cierra también la documentación: límites conocidos en este README y el registro
de uso de IA en DECISIONS.md.

**Post-cierre (D30) — el borrado.** `DELETE /api/documents/{id}`: un solo borrado
y la base derriba fragmentos, embeddings y contenido original (cascada). Se
rehúsa borrar en plena ingesta (409) porque la tarea en vuelo moriría contra una
fila que ya no existe. Botón «Eliminar» en el frontend con confirmación, y el
smoke ahora recoge su propia sonda de telemetría: ejecutarlo mil veces no deja
residuo en la lista de documentos.

## El mapa del código

Un recorrido por capa. La regla que ordena todo: **las flechas apuntan hacia
adentro** — el dominio y los servicios no importan nada de los adapters; los
adapters implementan los contratos (*puertos*) que el negocio define.

### backend/app/domain — las reglas, sin frameworks

- `document.py` — los estados del documento (`pending`, `processing`, `ready`,
  `failed`) y la tabla de transiciones permitidas. Ni la base ni la API deciden
  si un cambio de estado es legal: lo decide aquí.
- `chunker.py` — corta el texto página por página (un fragmento nunca cruza
  página), en palabras enteras hasta ~800 caracteres, con 120 de solape. Una
  palabra más larga que el máximo viaja entera: partirla rompería la literalidad
  de la cita.
- `retrieval.py` — la decisión de responder o rechazar: todo-o-nada sobre el
  mejor resultado frente al umbral 0.45, sin entregar contexto débil.
- `citations.py` — construye cada cita (documento, página, fragmento literal) y
  define en código qué significa "literal" (`quote_is_verbatim`), con recorte a
  220 caracteres por palabra completa cuando el fragmento es largo.

### backend/app/ports — los contratos

Cinco interfaces (`llm`, `embeddings`, `extraction`, `retrieval`,
`repository`): lo que el negocio necesita de fuera, escrito en sus propios
términos. Gracias a ellas, el núcleo no conoce ni OpenAI ni SQLAlchemy — y los
tests pueden inyectar dobles sin red ni claves.

### backend/app/services — los casos de uso

- `ingestion.py` — orquesta la ingesta: extraer, cortar, embedizar, persistir,
  y marcar estados con la política de fallos.
- `query.py` — `AgenticQueryService`, el bucle de consulta con sus tres
  desenlaces. Además de responder, emite los spans de dominio `rag.consulta` y
  `rag.iteracion` (la trazabilidad de qué decidió y por qué).

### backend/app/adapters — las implementaciones

- `http/documents.py` — `POST /api/documents` (subir: `201` nuevo con
  `Location`, `200` re-subida sin reprocesar), `GET /api/documents` (listado),
  `GET /api/documents/{id}` (estado con páginas, fragmentos y motivo de fallo),
  `GET /api/documents/{id}/content` (el PDF original `inline` y cacheable — es
  la cita navegable) y `DELETE /api/documents/{id}` (204/404/409 según estado).
- `http/query.py` — `POST /api/query`: la pregunta (y opcionalmente el documento
  donde acotarla) con sus tres desenlaces.
- `db/models.py` — las tres tablas y sus relaciones: `documents` (con la huella
  SHA-256 única del contenido), `document_contents` (el archivo original, 1:1)
  y `chunks` (texto, página y embedding `vector(1024)`); borrar un documento
  derriba las otras dos por cascada.
- `db/repository.py`, `db/retrieval.py` y `db/session.py` — el repositorio que
  implementa el puerto de persistencia, el buscador pgvector (que convierte
  distancia en similitud antes de que el dominio lo vea) y la unidad de trabajo
  transaccional.
- `llm/chat.py` y `llm/embeddings.py` — el proveedor real: Cloudflare Workers AI
  (llama-3.3-70b para redactar, bge-m3 para los embeddings) a través de su
  endpoint compatible con OpenAI — mismo código que el preset OpenAI, distinta
  URL. `llm/fake_llm.py` y `llm/fake_embeddings.py` — los dobles de los tests:
  deterministas y sin red.
- `extraction/extractors.py` — extracción de texto: pypdf para PDF, lectura
  directa para texto plano.
- `tasks/` — la aplicación Celery y la tarea de ingesta con su política de
  reintentos (`decide_retry`: fatal a la primera, transitoria hasta 3).
- `factory.py` — la composición: lee la configuración y conecta cada puerto con
  su adapter. Cambiar de proveedor es una variable de entorno, no una
  refactorización.

### backend/app/evaluation — la medición

`dataset.py` (las 3 pólizas y los 21 casos etiquetados con página y fragmento
de evidencia), `metrics.py` (precision@k, recall@k sobre páginas, hit@k) y
`runner.py` — lo que corre `make eval`: la tabla por caso, las medias y el
barrido de umbral 0.30–0.60 con el del stack marcado.

### backend/app/telemetry — la observabilidad

`setup.py` monta la instrumentación de cada proceso (la API y el worker se
identifican como servicios distintos), propaga el contexto de la petición HTTP
hasta la tarea de Celery, y se apaga por completo con `OTEL_ENABLED=false` —
los tests no notan su existencia.

### backend/app/config.py y main.py — la puerta de entrada

`config.py` declara la configuración tipada con validación al arrancar (un
nombre de proveedor mal escrito falla con el listado de valores válidos) y la
precedencia variable de entorno > `.env` > valor por defecto. `main.py` monta la
aplicación: `/health` para los healthchecks y el negocio bajo `/api`.

### backend/alembic — el esquema versionado

Dos migraciones: la inicial (documentos, fragmentos, columna `vector(1024)` con
índice HNSW) y la del contenido original. Se aplican solas al arrancar la API.

### backend/tests — 216 tests

La suite corre sin red ni claves: `conftest.py` fija los proveedores simulados
y `fakes.py` aporta los dobles. Hay tests por capa — chunker, citas, umbral,
ingesta, tarea Celery, API de documentos y consulta, telemetría, evaluación —
y además *pins* que congelan lecciones aprendidas: el guard `__main__` de todo
módulo ejecutable debe ser la última sentencia (la lección del bug que tumbó
la evaluación), el trío PDF ↔ huella SHA-256 ↔ set de etiquetas, la cláusula
gemela que no cuenta como evidencia de otra póliza, y la cascada del borrado.

### frontend/src — tres vistas, cero grasa

`api.js` (todo el cliente HTTP: listar, subir, eliminar, consultar, URL del
contenido), `App.jsx` (el shell con pestañas que comparte la lista de
documentos), `DocumentsView.jsx` (subida por arrastre, tabla con estados y
botón «Eliminar»), `QueryView.jsx` (la pregunta y sus tres desenlaces),
`Citation.jsx` (resuelve el documento de la cita y abre el PDF en la página
exacta) y `styles.css` (el tema sobrio de aseguradora).

### infra y scripts — el sistema alrededor del código

`infra/otel-collector-config.yaml` (el collector recibe trazas de ambos
procesos y las reenvía a consola y a Jaeger) y en `scripts/`: `smoke_stack.sh`
— las 18 secciones / 58 comprobaciones contra el stack levantado, de los
healthchecks de cada servicio a la migración, pgvector, Celery, la ingesta
completa, la consulta agéntica, el frontend, las trazas y la evaluación —,
`data/` (las tres pólizas del set) y `verifica_telemetria_local.py` (verifica
la telemetría sin Docker).

### La raíz del repo

`docker-compose.yml` (los siete servicios; API y worker comparten la red del
host — el motivo, una máquina sin salida IPv4, está comentado en el propio
archivo), `Makefile` (`up`, `down`, `logs`, `test`, `lint`, `smoke`, `eval`),
`.env.example` (la plantilla: solo faltan las dos credenciales de Cloudflare) y
`.github/workflows/ci.yml` (backend: lint + tests en Python 3.12; frontend:
`npm ci` + build en Node 24).

## Cómo ejecutarlo

Requisito: Docker.

```bash
cp .env.example .env   # completa CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN
make up                # = docker compose up --build (levanta los 7 servicios)
```

- **Frontend:** http://localhost:5173
- **API:** http://localhost:8000 — documentación interactiva en `/docs`
- **Jaeger:** http://localhost:16686 — busca la operación `rag.consulta`

¿Sin claves a mano? En `.env` pon `LLM_PROVIDER=fake` y
`EMBEDDING_PROVIDER=fake`: todo corre con el proveedor simulado — es el modo con
el que corren los tests.

Para verificar:

```bash
make smoke   # 18 secciones / 58 comprobaciones contra el stack real
make eval    # solo la evaluación de recuperación (P@k, R@k, hit@5 y barrido)
make test    # la suite de 216 tests, sin Docker
```

Los targets del `Makefile`: `up` (construye y levanta), `down` (tumba y borra
volúmenes), `logs`, `test` y `lint` (backend), `smoke` y `eval`.

Si el stack se desplegó desde Portainer con otro nombre de proyecto:
`PROJECT_NAME=<nombre> bash scripts/smoke_stack.sh`.

Desarrollo sin Docker:

```bash
cd backend && pip install -r requirements.txt && pytest -q   # la suite fija el modo fake sola
cd frontend && npm install && npm run dev
```

Si algo falla al levantar (build del frontend, red que bloquea PyPI, máquinas
sin salida IPv4), los tres casos reales y su diagnóstico están documentados en
[DECISIONS.md](DECISIONS.md) — la sección «Uso de IA generativa» y D24 narran
cada uno con su causa raíz.

## Límites conocidos

- **Sin autenticación ni multiusuario.** Cualquiera que alcance el stack ve
  todos los documentos. Recorte consciente del MVP.
- **No hay reindexado.** Cambiar el modelo de embeddings o el corte de
  fragmentos exige re-subir los documentos a mano. (Subir dos veces lo mismo no
  duplica nada, y el borrado individual sí existe desde D30.)
- **El umbral 0.45 está calibrado con 21 casos.** Es evidencia, no certeza; el
  barrido de `make eval` es la herramienta para revisarlo si el corpus real
  crece.
- **Precision y recall se reportan, no son puerta de calidad.** Fijarles suelos
  sin corridas reales acumuladas sería inventar números.
- **La suite corre con proveedores simulados; el smoke, con el real.** La
  calidad del modelo real no se mide en CI.
- **En la evaluación, cada pregunta vive en una sola póliza.** La búsqueda se
  acota al documento del caso para que los números sean reproducibles; medir
  recuperación global multi-documento queda para cuando exista un corpus real.

## Para saber más

[DECISIONS.md](DECISIONS.md) registra las 30 decisiones de diseño del proyecto,
cada una con su contexto, la decisión tomada y las alternativas que se
descartaron — más el registro de uso de IA generativa: qué se corrigió de lo
generado y por qué.
