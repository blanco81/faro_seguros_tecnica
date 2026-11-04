.PHONY: up down logs test lint smoke eval network

# La red intergraf_net es externa y compartida: se crea una sola vez.
network:
	docker network create intergraf_net 2>/dev/null || true

up: network
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f

test:
	cd backend && pytest -q

lint:
	cd backend && ruff check app tests

# Smoke test del stack LEVANTADO: 18 secciones / 58 comprobaciones del
# sistema real (servicios, healthchecks, migración, pgvector+HNSW, Celery,
# dominio, config viva, ingesta asíncrona end-to-end, consulta agéntica con
# cita verificada, frontend, trazas OTLP y evaluación de recuperación con
# el set etiquetado). Ver scripts/smoke_stack.sh
smoke:
	bash scripts/smoke_stack.sh

# Evaluación de recuperación contra el stack LEVANTADO (D28): corre el set
# etiquetado dentro del contenedor api — precision@k / recall@k, hit@5 y el
# barrido de umbral. Es la sección 18 del smoke, sola, para iterar sin
# repetir el smoke entero (misma máquina, mismo stack; Portainer: usa el
# smoke, que detecta el proyecto por sí solo).
eval:
	docker compose $(if $(PROJECT_NAME),-p $(PROJECT_NAME),) exec -T api python -m app.evaluation.runner
