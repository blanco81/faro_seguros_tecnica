#!/bin/bash
# ============================================================
# Faro Seguros RAG — smoke test del stack completo (Fases 0-2)
#
# Verifica el sistema REAL corriendo. Los 141 tests unitarios ya
# los valida el CI en cada push; esto prueba lo que el CI no
# puede: el stack arrancado de verdad (migración contra
# PostgreSQL, pgvector + HNSW, Celery via Redis, la cadena
# .env -> compose -> contenedor, el dominio de la Fase 1 vivo
# dentro de la imagen y — Fase 2, secciones 10-14 — la ingesta
# asíncrona COMPLETA: subida por la API, tarea Celery, embeddings
# del proveedor configurado (real o fake, D14) y chunks en la base.
#
# Re-ejecutable por diseño (D18): la idempotencia de subida (D03)
# hace que re-correr el smoke sea un no-evento (200, mismo id).
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

# --- Fase 2 (paso 5): materiales de la ingesta end-to-end ------------------
# PDF de ejemplo commiteado (2 páginas, cubierto por los tests de extracción).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLIZA_EJEMPLO="$SCRIPT_DIR/data/poliza-ejemplo.pdf"
# Espacio temporal para cuerpos y cabeceras de las llamadas HTTP del smoke.
SMOKE_TMP="$(mktemp -d)"
trap 'rm -rf "$SMOKE_TMP"' EXIT

# Parseo de JSON con solo POSIX (grep/cut): el smoke no depende de jq.
# jstr <clave> <fichero>  ->  valor de "clave":"valor" (strings)
# jnum <clave> <fichero>  ->  valor de "clave":123 (números; null no matchea)
jstr() { grep -o "\"$1\":\"[^\"]*\"" "$2" 2>/dev/null | head -1 | cut -d'"' -f4; }
jnum() { grep -o "\"$1\":[0-9]*" "$2" 2>/dev/null | head -1 | cut -d':' -f2; }

# Estado compartido entre las secciones 10-14 de la Fase 2.
doc_id=""
estado_final=""
chunks_api=""

echo "=================================================="
echo " Smoke test del stack — Faro Seguros RAG (Fases 0-2)"
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
if [ "$tablas" = "alembic_version,chunks,document_contents,documents" ]; then
  ok "tablas: $tablas"
else
  bad "tablas encontradas: '$tablas' (esperaba alembic_version,chunks,document_contents,documents)"
fi
rev="$(sql "SELECT version_num FROM alembic_version")"
if [ "$rev" = "0002" ]; then
  ok "alembic head: $rev (document_contents, D15)"
else
  bad "alembic head: '$rev' (esperaba 0002: ¿migró el stack desplegado?)"
fi
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
    print(f"    AVISO (el worker de la Fase 2 exige estas credenciales — si su healthcheck está verde, algo no cuadra): {e}")
' </dev/null || echo "    (no se pudo leer la configuración del contenedor)"

# ==========================================================================
# Fase 2 (paso 5): la ingesta asíncrona de verdad, por la puerta principal
# ==========================================================================
# La cadena completa: POST /api/documents (multipart) → registro + contenido
# en la base (D15/D17) → tarea Celery vía Redis → extracción → chunking →
# embeddings del proveedor CONFIGURADO (Cloudflare real o fake, D14) → chunks
# en pgvector → estado ready observable por GET. El smoke es un cliente más:
# no sabe nada de las piezas internas, solo del contrato HTTP y de la base.

echo ""
echo "10) Subida de la póliza de ejemplo (POST /api/documents, D17)"
if [ ! -f "$POLIZA_EJEMPLO" ]; then
  bad "falta el PDF de ejemplo: $POLIZA_EJEMPLO"
else
  codigo="$(curl -sS -o "$SMOKE_TMP/alta.json" -D "$SMOKE_TMP/alta.headers" \
             -w '%{http_code}' --max-time 30 \
             -F "file=@$POLIZA_EJEMPLO;type=application/pdf" \
             "$API_URL/api/documents" 2>/dev/null || echo 000)"
  if [ "$codigo" = "201" ] || [ "$codigo" = "200" ]; then
    ok "subida aceptada: HTTP $codigo (201 nuevo / 200 ya registrado, D03)"
  else
    bad "subida rechazada: HTTP $codigo — $(head -c 300 "$SMOKE_TMP/alta.json" 2>/dev/null)"
  fi
  doc_id="$(jstr id "$SMOKE_TMP/alta.json")"
  location="$(grep -i '^location:' "$SMOKE_TMP/alta.headers" 2>/dev/null | tr -d '\r' | head -1)"
  if [ -n "$doc_id" ] && echo "$location" | grep -q "$doc_id"; then
    ok "id del documento: $doc_id (Location correcto)"
  else
    bad "respuesta sin id o Location incorrecto (id='$doc_id', Location='$location')"
  fi
fi

if [ -n "$doc_id" ]; then
  echo ""
  echo "11) Ingesta asíncrona hasta el desenlace (worker + proveedor)"
  estado=""
  motivo=""
  espera=0
  i=0
  while [ "$i" -lt 50 ]; do
    i=$((i + 1))
    rm -f "$SMOKE_TMP/detalle.json"
    curl -fsS --max-time 5 -o "$SMOKE_TMP/detalle.json" \
      "$API_URL/api/documents/$doc_id" 2>/dev/null || true
    estado="$(jstr status "$SMOKE_TMP/detalle.json")"
    if [ "$estado" = "ready" ] || [ "$estado" = "failed" ]; then break; fi
    sleep 2
    espera=$((espera + 2))
  done

  if [ "$estado" = "ready" ]; then
    ok "estado final: ready (en ~${espera}s, $i consulta(s))"
    estado_final="ready"
  elif [ "$estado" = "failed" ]; then
    motivo="$(jstr failure_reason "$SMOKE_TMP/detalle.json")"
    bad "la ingesta terminó en failed: $motivo"
  else
    bad "la ingesta no terminó en ~100 s (último estado: '${estado:-sin respuesta}')"
  fi

  if [ "$estado_final" = "ready" ]; then
    paginas="$(jnum page_count "$SMOKE_TMP/detalle.json")"
    if [ "$paginas" = "2" ]; then
      ok "page_count: $paginas (el PDF de ejemplo tiene 2 páginas)"
    else
      bad "page_count: '$paginas' (esperaba 2 — ¿cambió el PDF de ejemplo?)"
    fi
    chunks_api="$(jnum chunk_count "$SMOKE_TMP/detalle.json")"
    if [ -n "$chunks_api" ] && [ "$chunks_api" -ge 1 ]; then
      ok "chunk_count: $chunks_api"
    else
      bad "chunk_count: '$chunks_api' (esperaba >= 1)"
    fi
  fi
else
  echo ""
  echo "11) Ingesta asíncrona: SALTADA (sin document_id)"
  bad "no hay documento que procesar (ver 10)"
fi

if [ "$estado_final" = "ready" ]; then
  echo ""
  echo "12) Chunks persistidos en pgvector (D02/D03)"
  chunks_db="$(sql "SELECT count(*) FROM chunks WHERE document_id = '$doc_id'")"
  if [ -n "$chunks_db" ] && [ "$chunks_db" = "$chunks_api" ]; then
    ok "chunks en la base: $chunks_db (coincide con la API)"
  else
    bad "chunks en la base: '$chunks_db' vs API '$chunks_api'"
  fi
  dims_ok="$(sql "SELECT count(*) FROM chunks WHERE document_id = '$doc_id' AND vector_dims(embedding) = 1024")"
  if [ -n "$dims_ok" ] && [ "$dims_ok" = "$chunks_db" ]; then
    ok "todos los embeddings con 1024 dimensiones (bge-m3, D10)"
  else
    bad "embeddings con dimensión incorrecta: '$dims_ok' de '$chunks_db'"
  fi
else
  echo ""
  echo "12) Chunks en pgvector: SALTADOS (la ingesta no terminó en ready)"
fi

if [ "$estado_final" = "ready" ]; then
  echo ""
  echo "13) Re-subida del mismo archivo: idempotencia (D03)"
  codigo2="$(curl -sS -o "$SMOKE_TMP/resubida.json" -w '%{http_code}' --max-time 30 \
               -F "file=@$POLIZA_EJEMPLO;type=application/pdf" \
               "$API_URL/api/documents" 2>/dev/null || echo 000)"
  doc_id2="$(jstr id "$SMOKE_TMP/resubida.json")"
  estado2="$(jstr status "$SMOKE_TMP/resubida.json")"
  if [ "$codigo2" = "200" ] && [ "$doc_id2" = "$doc_id" ] && [ "$estado2" = "ready" ]; then
    ok "200 con el mismo id y sigue ready (sin tarea nueva ni reproceso)"
  else
    bad "re-subida: HTTP $codigo2, id '$doc_id2', estado '$estado2' (esperaba 200 y '$doc_id')"
  fi
  listado="$(curl -fsS --max-time 10 "$API_URL/api/documents" 2>/dev/null || true)"
  if echo "$listado" | grep -q "\"id\":\"$doc_id\""; then
    ok "GET /api/documents lo lista para el seguimiento"
  else
    bad "el documento no aparece en GET /api/documents"
  fi
else
  echo ""
  echo "13) Idempotencia de re-subida: SALTADA (sin ingesta ready)"
fi

# El 413 (tamaño) no está aquí: fabricar un archivo de más de 20 MB en cada
# corrida para ver un código que ya cubre la suite unitaria es despilfarro (D18).
echo ""
echo "14) Validaciones de la subida (D08)"
cod="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
         -F "file=@$POLIZA_EJEMPLO;type=application/octet-stream" \
         "$API_URL/api/documents" 2>/dev/null || echo 000)"
if [ "$cod" = "415" ]; then ok "tipo no permitido -> 415"; else bad "tipo no permitido -> '$cod' (esperaba 415)"; fi

: > "$SMOKE_TMP/vacio.txt"
cod="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
         -F "file=@$SMOKE_TMP/vacio.txt;type=text/plain" \
         "$API_URL/api/documents" 2>/dev/null || echo 000)"
if [ "$cod" = "422" ]; then ok "archivo vacío -> 422"; else bad "archivo vacío -> '$cod' (esperaba 422)"; fi

cod="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
         -F "file=@$POLIZA_EJEMPLO;type=application/pdf;filename=" \
         "$API_URL/api/documents" 2>/dev/null || echo 000)"
if [ "$cod" = "422" ]; then ok "sin nombre de archivo -> 422"; else bad "sin nombre de archivo -> '$cod' (esperaba 422)"; fi

echo ""
echo "=================================================="
echo " RESULTADO: $PASS OK / $FAIL FALLOS"
if [ "$FAIL" -eq 0 ]; then
  echo " SMOKE: TODO VERDE — Fase 2 verificada end-to-end"
else
  echo " SMOKE: hay fallos — revisar la lista de arriba"
fi
echo "=================================================="
[ "$FAIL" -eq 0 ]
