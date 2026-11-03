.PHONY: up down logs test lint network

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
