#!/bin/bash
# ============================================================
# Faro Seguros RAG — smoke test del stack completo (Fases 0+1)
#
# Verifica el sistema REAL corriendo. Los 56 tests unitarios ya
# los valida el CI en cada push; esto prueba lo que el CI no
# puede: el stack arrancado de verdad (migración contra
# PostgreSQL, pgvector + HNSW, Celery via Redis, la cadena
# .env -> compose -> contenedor y el dominio de la Fase 1
# vivo dentro de la imagen).
#
# Se corre EN LA MÁQUINA CON DOCKER, con el stack levantado:
#
#   make up      # o docker compose up --build -d
#   make smoke   # o: bash scripts/smoke_stack.sh
#
# Variantes:
#   PROJECT_NAME=rag bash scripts/smoke_stack.sh     # deployado via Portainer
#   API_URL=http://host:8000 bash scripts/smoke_stack.sh
# ============================================================
set -u

API_URL="${API_URL:-http://localhost:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:5173}"

# --- ¿cómo hablamos con compose? -----------------------------
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: esta máquina no tiene 'docker compose' ni 'docker-compose'."
  echo "       El smoke test se corre donde corre el stack: make up && make smoke."
  exit 2
fi

# Proyecto: el de este directorio por defecto; si no hay nada
# levantado aquí y existe bajo el nombre 'rag' (Portainer), usarlo.
PFLAG="${PROJECT_NAME:+-p $PROJECT_NAME}"
compose() { $COMPOSE $PFLAG "$@"; }

if [ -z "${PROJECT_NAME:-}" ] && [ -z "$(compose ps -q api 2>/dev/null)" ]; then
  if [ -n "$($COMPOSE -p rag ps -q api 2>/dev/null)" ]; then
    PFLAG="-p rag"
    echo "(stack detectado bajo el proyecto 'rag' de Portainer)"
  fi
fi

if [ -z "$(compose ps -q api 2>/dev/null)" ]; then
  echo "ERROR: no encuentro el servicio 'api' del stack."
  echo "       ¿Está levantado? (make up)   ¿Otro nombre de proyecto?"
  echo "       PROJECT_NAME=<nombre> bash $0"
  exit 2
fi

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  [ OK ] %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }

# SQL contra el db del stack (psql ya viene en la imagen pgvector).
# </dev/null: psql no lee stdin; así ningún exec queda esperándolo.
sql() { compose exec -T db psql -U faro -d faro -tAc "$1" </dev/null 2>/dev/null; }

echo "=================================================="
echo " Smoke test del stack — Faro Seguros RAG (Fases 0+1)"
echo "=================================================="

echo ""
echo "1) Servicios corriendo"
for svc in db redis api worker frontend otel-collector; do
  cid="$(compose ps -q "$svc" 2>/dev/null)"
  if [ -z "$cid" ]; then bad "$svc: no existe"; continue; fi
  estado="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
  if [ "$estado" = "running" ]; then ok "$svc: running"; else bad "$svc: $estado"; fi
done

echo ""
echo "2) Healthchecks (db, redis, api, worker)"
for svc in db redis api worker; do
  cid="$(compose ps -q "$svc" 2>/dev/null)"
  h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin healthcheck{{end}}' "$cid" 2>/dev/null)"
  if [ "$h" = "healthy" ]; then ok "$svc: healthy"; else bad "$svc: $h"; fi
done

echo ""
echo "3) Endpoint /health de la API"
cuerpo="$(curl -fsS --max-time 5 "$API_URL/health" 2>/dev/null)"
if [ "$cuerpo" = '{"status":"ok"}' ]; then
  ok "$API_URL/health -> $cuerpo"
else
  bad "respuesta inesperada de $API_URL/health: '$cuerpo'"
fi

echo ""
echo "4) Frontend servido por nginx"
codigo="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$FRONTEND_URL/" 2>/dev/null)"
codigo="${codigo:-000}"
if [ "$codigo" = "200" ]; then ok "$FRONTEND_URL -> 200"; else bad "$FRONTEND_URL -> $codigo"; fi

echo ""
echo "5) Migración Alembic aplicada (D05)"
tablas="$(sql "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname='public'")"
if [ "$tablas" = "alembic_version,chunks,documents" ]; then
  ok "tablas: $tablas"
else
  bad "tablas encontradas: '$tablas' (esperaba alembic_version,chunks,documents)"
fi
rev="$(sql "SELECT version_num FROM alembic_version")"
if [ -n "$rev" ]; then ok "alembic head: $rev"; else bad "alembic_version vacía"; fi
enum="$(sql "SELECT enum_range(NULL::document_status)")"
if [ "$enum" = "{pending,processing,ready,failed}" ]; then
  ok "enum document_status: $enum"
else
  bad "enum document_status: '$enum'"
fi

echo ""
echo "6) pgvector: extensión, vector(1024) e índice HNSW (D02)"
ext="$(sql "SELECT extversion FROM pg_extension WHERE extname='vector'")"
if [ -n "$ext" ]; then ok "extensión vector: $ext"; else bad "extensión vector NO instalada"; fi
tipo="$(sql "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a WHERE a.attrelid='chunks'::regclass AND a.attname='embedding'")"
if [ "$tipo" = "vector(1024)" ]; then ok "chunks.embedding: $tipo"; else bad "chunks.embedding: '$tipo'"; fi
idx="$(sql "SELECT indexdef FROM pg_indexes WHERE indexname='ix_chunks_embedding_hnsw'")"
if echo "$idx" | grep -q "hnsw" && echo "$idx" | grep -q "vector_cosine_ops"; then
  ok "índice HNSW (coseno) presente"
else
  bad "índice HNSW no encontrado: '$idx'"
fi

echo ""
echo "7) Worker Celery responde vía Redis"
# Salida capturada con stderr VISIBLE (2>&1): si algo falla, se ve por qué.
# timeout -k 5 45: si a los 45s no terminó, SIGTERM y 5s después SIGKILL.
salida7="$(timeout -k 5 45 $COMPOSE $PFLAG exec -T worker celery -A app.adapters.tasks.celery_app inspect ping </dev/null 2>&1 || true)"
if echo "$salida7" | grep -q "pong"; then
  ok "worker respondió pong ($(echo "$salida7" | grep -o "celery@[^:]*" | head -1))"
else
  bad "sin respuesta del worker — salida completa: $salida7"
fi

echo ""
echo "8) Dominio de la Fase 1 dentro del contenedor (D11-D13)"
# El código viaja como ARGUMENTO (python -c), no por stdin: bajo `make`, 
# alimentar un exec por heredoc puede quedarse esperando EOF indefinidamente.
demo="$(compose exec -T api python -c '
from app.domain.chunker import PageText, chunk_document
from app.domain.citations import build_citation, quote_is_verbatim
from app.domain.retrieval import RetrievedChunk, decide_retrieval

texto = ("La poliza cubre danos por inundacion en el hogar asegurado "
         "segun los limites y condiciones descritos en la clausula 4.1 ")
chunks = chunk_document([PageText(1, texto * 6)], 120, 30)
cita = build_citation("demo.pdf", chunks[0].page_number, chunks[0].content)
assert quote_is_verbatim(cita, chunks[0].content), "la cita no es literal"
debil = decide_retrieval([RetrievedChunk("demo.pdf", 1, chunks[0].content, 0.30)], threshold=0.45)
fuerte = decide_retrieval([RetrievedChunk("demo.pdf", 1, chunks[0].content, 0.80)], threshold=0.45)
assert not debil.answered and debil.reason == "below_threshold", "no rechazo con score bajo"
assert fuerte.answered and len(fuerte.chunks) == 1, "no respondio con score alto"
print(f"DOMINIO-OK: {len(chunks)} chunks, cita literal, umbral rechaza 0.30 y acepta 0.80")
' </dev/null 2>&1 || true)"
if echo "$demo" | grep -q "DOMINIO-OK"; then ok "$demo"; else bad "la demo del dominio falló: $demo"; fi

echo ""
echo "9) Configuración viva dentro del contenedor (D14: .env -> compose -> proceso)"
compose exec -T api python -c '
from app.config import get_settings

s = get_settings()
print(f"    proveedor llm/embeddings: {s.llm_provider} / {s.embedding_provider}")
print(f"    modelos: {s.llm_model} | {s.embedding_model}")
print(f"    umbral de rechazo: {s.rejection_threshold} | chunk: {s.chunk_size}+{s.chunk_overlap}")
try:
    s.validate_provider_credentials()
    print("    credenciales del proveedor: completas")
except ValueError as e:
    print(f"    AVISO (la Fase 2 las exigirá al arrancar): {e}")
' </dev/null || echo "    (no se pudo leer la configuración del contenedor)"

echo ""
echo "=================================================="
echo " RESULTADO: $PASS OK / $FAIL FALLOS"
if [ "$FAIL" -eq 0 ]; then
  echo " SMOKE: TODO VERDE — base sólida para la Fase 2"
else
  echo " SMOKE: hay fallos — revisar la lista de arriba"
fi
echo "=================================================="
[ "$FAIL" -eq 0 ]
