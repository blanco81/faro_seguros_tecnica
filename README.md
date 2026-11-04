# Faro Seguros · Asistente RAG con citas

MVP para analistas de siniestros: suben pólizas y anexos (PDF o texto), el sistema
procesa en segundo plano (extracción → chunking → embeddings → PostgreSQL + pgvector)
y responde preguntas en lenguaje natural **con citas verificables** (documento, página
y fragmento). Si la evidencia recuperada no alcanza el umbral definido, **rechaza
responder** en lugar de inventar.

> Prueba técnica TopDevCrew · Ingeniero AI Full Stack Semi-Senior
> Entrega antes del **5 de noviembre de 2026, 23:22 UTC**

**Estado: entrega completa — Fases 0-6 cerradas.** Infraestructura y
 dominio puro (Fases 0-1), ingesta asíncrona idempotente (Fase 2),
consulta agéntica con rechazo honesto y umbral calibrado en 0.45 (Fase 3,
D20), frontend con citas navegables (Fase 4) y observabilidad end-to-end
(Fase 5): trazas OpenTelemetry HTTP → Celery → LLM con Jaeger always-on
(D23-D25), aislamiento del contexto no confiable en el prompt (D26) y
spans de dominio `rag.consulta`/`rag.iteracion` — la traza cuenta qué
decidió el agente (D27). La Fase 6 cierra la entrega: **las tres pólizas
del set de evaluación** (hogar, auto y vida; 19 preguntas en dominio + 2
fuera, D28/D29), README completo con límites conocidos y DECISIONS.md
final con el registro de uso de IA completado. Post-cierre (D30): borrado
de documentos (`DELETE /api/documents/{id}` con cascada, botón en el
frontend y smoke que recoge su propia sonda de telemetría). 216 tests
unitarios; smoke end-to-end de **18 secciones / 58 comprobaciones**. Ver
[cronograma](#cronograma).

## Stack

FastAPI · Celery + Redis · PostgreSQL 16 + pgvector (índice HNSW) · Alembic ·
React + Vite · OpenTelemetry · Docker Compose

## Levantar todo con un comando

Requisito: Docker.

```bash
cp .env.example .env   # y completa CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN
docker compose up --build
```

Por defecto el sistema arranca con el **proveedor real** (Cloudflare Workers
AI, decisión D10). ¿Sin claves a mano? En `.env` pon `LLM_PROVIDER=fake` y
`EMBEDDING_PROVIDER=fake`: todo corre con el proveedor simulado — es el modo
con el que corren los tests (D14).

| Servicio | URL / puerto |
| --- | --- |
| Frontend (React) | http://localhost:5173 |
| API (FastAPI) | http://localhost:8000 — docs interactivas en `/docs` |
| Worker (Celery) | logs en la consola de compose |
| Collector OTel | 4317 (gRPC) / 4318 (HTTP) — trazas en consola |
| Jaeger (always-on, D25) | http://localhost:16686 — busca la operación `rag.consulta` |

## Verificar el stack: `make smoke`

Los tests unitarios (216) los corre el CI en cada push; el smoke test comprueba lo
que el CI no puede — **el stack arrancado de verdad**: servicios y healthchecks,
`/health`, frontend (shell React con tabs + assets bundleados), migración
Alembic (tablas y enum de estados), pgvector con `vector(1024)` e índice HNSW,
worker Celery vía Redis, el dominio de la Fase 1 ejecutándose dentro del
contenedor, la cadena `.env → compose → proceso` (D14), la **ingesta asíncrona
completa** (subida por la API, procesamiento en el worker, chunks en pgvector,
idempotencia de re-subida), la **consulta agéntica** (respuesta con cita literal
verificada contra la base, rechazo explícito de preguntas fuera de dominio
D13/D19), el **frontend y la cita navegable** (`GET /api/documents/{id}/content`
sirve el PDF inline y cacheable), las **trazas OTLP** de punta a punta
(collector, Jaeger, propagación HTTP → Celery, spans de dominio `rag.*`) y la
**evaluación de recuperación** con el set etiquetado de **tres pólizas** (hogar,
auto y vida; sección 18: precision@k / recall@k, hit@5 y barrido de umbral,
D28/D29). Desde D30 la sección 17 además recoge su sonda de telemetría al
terminar (DELETE end-to-end con cascada verificada en pgvector): el smoke
re-ejecutable no deja residuo en la lista de documentos.

```bash
make up      # levanta el stack
make smoke   # 18 secciones / 58 comprobaciones, PASS/FAIL por cada una
make eval    # solo la evaluación de recuperación (D28/D29; sube antes las
             # tres pólizas de scripts/data/ o corre el smoke, que las sube)
```

Si el stack se deployó desde Portainer con otro nombre de proyecto:
`PROJECT_NAME=rag bash scripts/smoke_stack.sh`.

## Desarrollo sin Docker

```bash
# Backend — la suite fija el modo fake por sí misma (tests sin red ni claves, D14)
cd backend && pip install -r requirements.txt && pytest -q

# Frontend
cd frontend && npm install && npm run dev
```

## Solución de problemas

- **El build de `frontend` falla con `exit code: 127` (`vite: not found`).**
  El lockfile del repo compila verificado (`npm ci && npm run build`). Si falla
  en tu máquina, casi siempre es que `frontend/package.json` o
  `frontend/package-lock.json` divergieron de la versión del repo (por ejemplo,
  tras ejecutar `npm install` local con otra versión de npm). Compara con:

  ```bash
  md5sum frontend/package.json frontend/package-lock.json
  # esperado:
  # 7b9d93771d35f7373445cafba651f27e  frontend/package.json
  # 754d7fd9f908f21485c3620ad89a81df  frontend/package-lock.json
  ```

  Si difieren, restaura ambos archivos del repo y reconstruye sin caché:

  ```bash
  docker compose build --no-cache frontend && docker compose up -d
  ```

- **Ver el error real de un paso de build** (sin que la barra de progreso lo
  tape): `docker compose build --progress=plain --no-cache frontend 2>&1 | tee build.log`

- **El build falla en `pip install` con `Errno 101 Network is unreachable`.**
  El paso de build (que corre dentro de un contenedor) no alcanza pypi.org,
  aunque las imágenes se descarguen bien (eso lo hace el daemon en la red del
  host). Diagnóstico en 30 segundos:

  ```bash
  # 1) ¿El host llega a PyPI?
  curl -sI https://pypi.org/simple/ -o /dev/null -w '%{http_code}\n'

  # 2) ¿Un contenedor llega a PyPI? (misma ruta de red que usa el build)
  docker run --rm python:3.12-slim \
    python -c "import urllib.request as u; print(u.urlopen('https://pypi.org/simple/', timeout=10).status)"
  ```

  Según el resultado:
  - **Ambos funcionan** → fue una caída transitoria: reintenta
    `docker compose up --build` (las capas anteriores están cacheadas).
  - **Solo el contenedor falla** → típico tras conectar/desconectar una VPN o
    un cambio de firewall que rompe el NAT del bridge de Docker:
    `sudo systemctl restart docker` y reintentar. Si persiste, revisa qué IP
    resuelve el contenedor (`docker run --rm python:3.12-slim python -c
    "import socket; print(socket.gethostbyname('pypi.org'))"`); una IP tipo
    `0.0.0.0` indica DNS filtrado (sinkhole corporativo).
  - **El host tampoco llega** → la red bloquea PyPI. Dos salidas: configurar
    el proxy corporativo en `~/.docker/config.json` (sección `proxies`;
    BuildKit lo inyecta automáticamente en los builds), o apuntar a un espejo
    accesible con la variable `PIP_INDEX_URL` en `.env` (ver `.env.example`).

- **La ingesta termina en `failed` con "Reintentos agotados (4 intentos):
  Connection error." (máquinas con salida IPv6-only).** El worker no llega a
  `api.cloudflare.com`: la red bridge de Docker es IPv4-interna y, sin salida
  IPv4, el contenedor no resuelve el host (`[Errno -3] Temporary failure in
  name resolution`). El `docker-compose.yml` del repo trae al `worker` en
  `network_mode: host` (hereda la pila IPv6 del host) con `DATABASE_URL` y
  `REDIS_URL` apuntando a `localhost` (los puertos 5432/6379 ya están
  mapeados). En redes dual-stack es inofensivo. Si tu stack no levanta por
  "mutually exclusive network_mode and networks", tienes un
  `docker-compose.override.yml` reañadiendo `networks:` al worker: renómbralo
  a `.bak` y vuelve a intentar.

## Estructura (monorepo)

```
├── backend/               # FastAPI + Celery + Alembic
│   ├── app/
│   │   ├── domain/        # núcleo: estados, chunking, umbral, citas (sin frameworks)
│   │   ├── ports/         # interfaces propias: LLMProvider, EmbeddingProvider, ...
│   │   ├── services/      # casos de uso: ingesta y consulta
│   │   ├── adapters/      # implementaciones: db, http, llm, extraction, tasks
│   │   ├── evaluation/    # set etiquetado + precision@k/recall@k (D28)
│   │   └── telemetry/     # OpenTelemetry
│   ├── alembic/           # migraciones versionadas
│   └── tests/
├── frontend/              # React + Vite (nginx en producción)
├── infra/                 # configuración del collector OTel
├── .github/workflows/     # CI: lint + tests + build
└── docker-compose.yml
```

La regla de dependencias es hexagonal: `domain` y `services` no importan nada de
`adapters`; los adapters implementan los `ports` (ver DECISIONS.md).

## Límites conocidos

- **Sin autenticación ni multiusuario** (fuera de alcance consciente del MVP):
  cualquiera que alcance el stack ve todos los documentos.
- **No hay reindexado** (el borrado individual sí existe desde D30: botón
  «Eliminar» en el frontend o `DELETE /api/documents/{id}`, con cascada a
  chunks y contenido): subir el mismo archivo dos veces no duplica nada
  (D03), pero cambiar el modelo de embeddings o el chunking exige
  reingestar los documentos a mano.
- **El umbral 0.45 está calibrado con el set de evaluación** (D20/D28/D29): 21
  casos dan evidencia, no certeza — el barrido de `make eval` es la herramienta
  para revisarlo si el corpus real crece.
- **P@k/R@k se reportan, no son puerta de salida**: los suelos de calidad se
  fijarán con corridas reales acumuladas (mismo criterio que D20/D16).
- **La suite corre con proveedores fake** (D14) y el smoke con el proveedor
  real del stack: la calidad del LLM real no se mide en CI.
- **En la evaluación, cada pregunta vive en UNA póliza**: la búsqueda en
  dominio va acotada al documento del caso (reproducible, sin sondas
  acumuladas); medir recuperación global multi-documento queda para cuando
  exista un corpus real.

## Cronograma

- [x] **Fase 0** — Esqueleto monorepo, Docker Compose (6 servicios), modelos + Alembic, CI
- [x] **Fase 1** — Dominio puro: chunking, umbral de rechazo, citas + tests unitarios
- [x] **Fase 2** — Ingesta asíncrona: puertos y adapters, Celery idempotente, retries, API de subida/seguimiento
- [x] **Fase 3** — Consulta RAG agéntica: recuperación pgvector, bucle con verificación de citas (D19), rechazo explícito (D13) — **verificada con `make smoke` 37/37 OK, umbral calibrado en 0.45 (D20)**
- [x] **Fase 4** — Frontend React+Vite: subida drag&drop, listado con polling 3s, consulta con tres desenlaces, citas navegables (`#page=N`, D12) — **verificada con `make smoke` 40/40 OK**
- [ ] **Fase 5** — Observabilidad end-to-end (HTTP → Celery → LLM) + seguridad
  - [x] Pasos 1-3 (D23 + D24): telemetría real — `app/telemetry` instrumenta FastAPI +
    Celery + httpx en ambos procesos, propagación HTTP → Celery por traceparent,
    sección 17 del smoke. Bug del compose corregido (worker heredaba un
    endpoint OTLP inalcanzable desde `network_mode: host`) y tres bugs más
    destapados por la primera corrida real (D24): instrumentación en
    `worker_process_init` (cada hijo prefork, no el padre — el hilo del BSP
    no sobrevive al fork), sonda única en el smoke para forzar tarea fresca
    (D03 idempotencia) y nombres de span reales (`apply_async/` y `run/`)
  - [x] **Spans de dominio RAG** (D27): `AgenticQueryService` emite `rag.consulta`
    (raíz: pregunta, umbral, desenlace respondida/rechazada/aclaración, motivo,
    iteraciones, citas) y `rag.iteracion` por vuelta del bucle (scores de los k
    mejores, mejor score vs umbral, decisión de esa iteración, cita verificada);
    tracer inyectable, no-op con la telemetría apagada — la suite no cambia (D01);
    un rechazo NO es status ERROR: es un desenlace de negocio correcto (D13)
  - [x] **Set de evaluación + precision@k / recall@k** (D28, cierra la
    calibración de D20): 10 preguntas en dominio etiquetadas contra
    `poliza-ejemplo.pdf` (página relevante + snippet verbatim, ancla sha256 —
    una multi-página) y 2 fuera de dominio; el runner (`make eval`, smoke
    sección 18) mide P@1/P@5/R@5/hit@5 por caso y medias con búsqueda acotada
    al documento, el 0-aceptados fuera de dominio con búsqueda sobre toda la
    base (la seguridad del umbral D13) y el barrido de umbral 0.30–0.60 con el
    del stack marcado — la «curva» de D20. Las etiquetas se verifican en cada
    corrida contra los chunks reales de la base y la suite pinea el trío
    PDF ↔ sha256 ↔ set (210 tests tras D29 y el fix del guard del runner)
  - [x] **Jaeger always-on** (D25): el collector reenvía trazas a Jaeger por OTLP (`otlp/jaeger` en `infra/otel-collector-config.yaml`) y el servicio `jaeger` perdió el `profiles: ["ui"]` — arranca con `make up` y su UI en http://localhost:16686 muestra la cascada faro-api ↔ faro-worker; el smoke consulta su API REST (`/api/services`, `/api/traces`)
  - [x] **Aislamiento del contexto no confiable en el prompt** (D26, deuda D08): cada fragmento viaja en un bloque `<fragmento ...>`, el prompt declara la REGLA DE AISLAMIENTO (datos, no instrucciones) y la frontera dura sigue siendo la cita verbatim validada contra el chunk (D12/D19); `FakeLLMProvider` ancla su regex a los nuevos marcadores en lockstep
- [x] **Fase 6** — Cierre de la entrega: el set de evaluación completo con **las
  tres pólizas** del entregable (`poliza-ejemplo.pdf` hogar + `poliza-auto.pdf` +
  `poliza-vida.pdf`; 19 preguntas en dominio con página relevante y snippet
  verbatim — las cláusulas gemelas de las tres no se cruzan: cada caso se verifica
  contra SU póliza — y 2 fuera de dominio, D29), README con **límites conocidos**
  y DECISIONS.md final con el **registro de uso de IA** completado (qué se
  corrigió de lo generado y por qué)
- [x] **Post-cierre (D30)** — Borrado de documentos: `DELETE
  /api/documents/{id}` (204/404/409 según estado; un solo DELETE y la FK
  `ON DELETE CASCADE` tumba chunks y contenido) + botón «Eliminar» en el
  frontend (confirmación, deshabilitado durante la ingesta) + sección 17 del
  smoke que borra su propia sonda y verifica la cascada contra pgvector — el
  smoke re-ejecutable ya no acumula `sonda.txt` en la lista de documentos

Decisiones de diseño, alternativas descartadas y uso de IA generativa:
[DECISIONS.md](DECISIONS.md).
