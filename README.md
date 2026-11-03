# Faro Seguros · Asistente RAG con citas

MVP para analistas de siniestros: suben pólizas y anexos (PDF o texto), el sistema
procesa en segundo plano (extracción → chunking → embeddings → PostgreSQL + pgvector)
y responde preguntas en lenguaje natural **con citas verificables** (documento, página
y fragmento). Si la evidencia recuperada no alcanza el umbral definido, **rechaza
responder** en lugar de inventar.

> Prueba técnica TopDevCrew · Ingeniero AI Full Stack Semi-Senior
> Entrega antes del **5 de noviembre de 2026, 23:22 UTC**

**Estado: Fase 2/6 completada** — Fase 0–1 (dominio puro, configuración D14) y
Fase 2 cerrada de punta a punta: puertos y adapters de extracción/embeddings,
repositorio y servicio de ingesta idempotente (D03), tarea Celery con contenido
en la base (D15), política de reintentos (D16), API HTTP de subida y seguimiento
con dispatch tras el commit (D17) y smoke end-to-end de la ingesta. 141 tests,
14 comprobaciones de smoke. Siguiente: Fase 3 (consulta RAG). Ver
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
| Jaeger (opcional) | `docker compose --profile ui up` → http://localhost:16686 |

## Verificar el stack: `make smoke`

Los tests unitarios (141) los corre el CI en cada push; el smoke test comprueba lo
que el CI no puede — **el stack arrancado de verdad**: servicios y healthchecks,
`/health`, frontend, migración Alembic (tablas y enum de estados), pgvector con
`vector(1024)` e índice HNSW, worker Celery vía Redis, el dominio de la Fase 1
ejecutándose dentro del contenedor, la cadena `.env → compose → proceso` (D14) y,
desde la Fase 2, la **ingesta asíncrona completa**: subida por la API,
procesamiento en el worker, chunks persistidos en pgvector e idempotencia de
re-subida.

```bash
make up      # levanta el stack
make smoke   # 14 comprobaciones, PASS/FAIL por cada una
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
- [ ] **Fase 3** — Consulta RAG: recuperación pgvector, puerto LLM (fake/OpenAI), rechazo
- [ ] **Fase 4** — Frontend: subir y ver estado, preguntar, citas navegables
- [ ] **Fase 5** — Observabilidad end-to-end (HTTP → Celery → LLM) + seguridad
- [ ] **Fase 6** — README completo, DECISIONS.md final, documentos y preguntas de evaluación

Decisiones de diseño, alternativas descartadas y uso de IA generativa:
[DECISIONS.md](DECISIONS.md).
