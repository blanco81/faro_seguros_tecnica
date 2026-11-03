# Faro Seguros · Asistente RAG con citas

MVP para analistas de siniestros: suben pólizas y anexos (PDF o texto), el sistema
procesa en segundo plano (extracción → chunking → embeddings → PostgreSQL + pgvector)
y responde preguntas en lenguaje natural **con citas verificables** (documento, página
y fragmento). Si la evidencia recuperada no alcanza el umbral definido, **rechaza
responder** en lugar de inventar.

> Prueba técnica TopDevCrew · Ingeniero AI Full Stack Semi-Senior
> Entrega antes del **5 de noviembre de 2026, 23:22 UTC**

**Estado: Fase 0/6 completada** — esqueleto del monorepo, infraestructura Docker,
esquema de datos con Alembic y CI. Ver [cronograma](#cronograma).

## Stack

FastAPI · Celery + Redis · PostgreSQL 16 + pgvector (índice HNSW) · Alembic ·
React + Vite · OpenTelemetry · Docker Compose

## Levantar todo con un comando

Requisito: Docker.

```bash
cp .env.example .env   # opcional: por defecto proveedores "fake" (sin claves ni red)
docker compose up --build
```

| Servicio | URL / puerto |
| --- | --- |
| Frontend (React) | http://localhost:5173 |
| API (FastAPI) | http://localhost:8000 — docs interactivas en `/docs` |
| Worker (Celery) | logs en la consola de compose |
| Collector OTel | 4317 (gRPC) / 4318 (HTTP) — trazas en consola |
| Jaeger (opcional) | `docker compose --profile ui up` → http://localhost:16686 |

## Desarrollo sin Docker

```bash
# Backend — los tests no usan red ni claves (proveedores fake)
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
- [ ] **Fase 1** — Dominio puro: chunking, umbral de rechazo, citas + tests unitarios
- [ ] **Fase 2** — Ingesta asíncrona: puertos y adapters, Celery idempotente, retries
- [ ] **Fase 3** — Consulta RAG: recuperación pgvector, puerto LLM (fake/OpenAI), rechazo
- [ ] **Fase 4** — Frontend: subir y ver estado, preguntar, citas navegables
- [ ] **Fase 5** — Observabilidad end-to-end (HTTP → Celery → LLM) + seguridad
- [ ] **Fase 6** — README completo, DECISIONS.md final, documentos y preguntas de evaluación

Decisiones de diseño, alternativas descartadas y uso de IA generativa:
[DECISIONS.md](DECISIONS.md).
