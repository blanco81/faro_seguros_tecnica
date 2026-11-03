# Faro Seguros · Asistente RAG con citas

MVP para analistas de siniestros: suben pólizas y anexos (PDF o texto), el sistema
procesa en segundo plano (extracción → chunking → embeddings → PostgreSQL + pgvector)
y responde preguntas en lenguaje natural **con citas verificables** (documento, página
y fragmento). Si la evidencia recuperada no alcanza el umbral definido, **rechaza
responder** en lugar de inventar.

> Prueba técnica TopDevCrew · Ingeniero AI Full Stack Semi-Senior
> Entrega antes del **5 de noviembre de 2026, 23:22 UTC**

**Estado: Fase 4/6 verificada** — Fase 0–3 (infra, dominio, ingesta asíncrona,
consulta agéntica) y Fase 4 cerrada: **frontend React+Vite** con subida
drag&drop, listado con polling 3s, consulta al agente con tres desenlaces
(respuesta / rechazo / aclaración) y **citas navegables** — cada cita abre el
PDF original en pestaña nueva con `#page=N` (D12). Endpoint nuevo
`GET /api/documents/{id}/content` sirve el PDF `inline` y cacheable (D03).
172 tests unitarios, 16 secciones de smoke. Siguiente: Fase 5 (observabilidad).
Ver [cronograma](#cronograma).

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
| Jaeger (opcional) | `docker compose --profile ui up` → http://localhost:16686 |

## Verificar el stack: `make smoke`

Los tests unitarios (172) los corre el CI en cada push; el smoke test comprueba lo
que el CI no puede — **el stack arrancado de verdad**: servicios y healthchecks,
`/health`, frontend (shell React con tabs + assets bundleados), migración
Alembic (tablas y enum de estados), pgvector con `vector(1024)` e índice HNSW,
worker Celery vía Redis, el dominio de la Fase 1 ejecutándose dentro del
contenedor, la cadena `.env → compose → proceso` (D14), la **ingesta asíncrona
completa** (subida por la API, procesamiento en el worker, chunks en pgvector,
idempotencia de re-subida), la **consulta agéntica** (respuesta con cita literal
verificada contra la base, rechazo explícito de preguntas fuera de dominio
D13/D19) y, desde la Fase 4, el **frontend y la cita navegable**
(`GET /api/documents/{id}/content` sirve el PDF inline y cacheable).

```bash
make up      # levanta el stack
make smoke   # 16 secciones / ~40 comprobaciones individuales, PASS/FAIL por cada una
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

## Cronograma

- [x] **Fase 0** — Esqueleto monorepo, Docker Compose (6 servicios), modelos + Alembic, CI
- [x] **Fase 1** — Dominio puro: chunking, umbral de rechazo, citas + tests unitarios
- [x] **Fase 2** — Ingesta asíncrona: puertos y adapters, Celery idempotente, retries, API de subida/seguimiento
- [x] **Fase 3** — Consulta RAG agéntica: recuperación pgvector, bucle con verificación de citas (D19), rechazo explícito (D13) — **verificada con `make smoke` 37/37 OK, umbral calibrado en 0.45 (D20)**
- [x] **Fase 4** — Frontend React+Vite: subida drag&drop, listado con polling 3s, consulta con tres desenlaces, citas navegables (`#page=N`, D12) — **verificada con `make smoke` (sección 16)**
- [ ] **Fase 5** — Observabilidad end-to-end (HTTP → Celery → LLM) + seguridad
- [ ] **Fase 6** — README completo, DECISIONS.md final, documentos y preguntas de evaluación

Decisiones de diseño, alternativas descartadas y uso de IA generativa:
[DECISIONS.md](DECISIONS.md).
