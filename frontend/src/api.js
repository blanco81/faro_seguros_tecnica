// Cliente HTTP al backend (Fase 4).
//
// Todas las llamadas van por la misma ruta relativa /api/* — en desarrollo
// Vite las proxifica al backend (vite.config.js), en producción lo hace
// nginx (nginx.conf). Sin URL absoluta: el frontend no sabe ni necesita
// saber en qué puerto corre la API.

const BASE = "/api";

async function _json(resp) {
  if (!resp.ok) {
    // FastAPI devolverá {detail: "..."} en la mayoría de los errores;
    // si no, dejamos un mensaje genérico accionable.
    let detalle;
    try {
      detalle = (await resp.json()).detail ?? `HTTP ${resp.status}`;
    } catch {
      detalle = `HTTP ${resp.status} — ${resp.statusText}`;
    }
    throw new Error(detalle);
  }
  return resp.json();
}

export async function listarDocumentos() {
  // El más reciente primero (lo ordena el repositorio).
  const r = await fetch(`${BASE}/documents`);
  return _json(r);
}

export async function subirDocumento(file) {
  // multipart, campo "file" — el contrato del endpoint POST /api/documents.
  const form = new FormData();
  form.append("file", file);
  const r = await fetch(`${BASE}/documents`, { method: "POST", body: form });
  return _json(r);
}

export async function eliminarDocumento(documentId) {
  // DELETE /api/documents/{id} -> 204 SIN cuerpo (D30): no puede pasar por
  // _json porque no hay JSON que parsear. Los errores sí traen {detail}.
  const r = await fetch(`${BASE}/documents/${documentId}`, { method: "DELETE" });
  if (!r.ok) {
    let detalle;
    try {
      detalle = (await r.json()).detail ?? `HTTP ${r.status}`;
    } catch {
      detalle = `HTTP ${r.status} — ${r.statusText}`;
    }
    throw new Error(detalle);
  }
  return true;
}

export async function consultar(question, documentId = null) {
  // POST /api/query: {question, document_id?} -> QueryOut (D19).
  const body = { question };
  if (documentId) body.document_id = documentId;
  const r = await fetch(`${BASE}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return _json(r);
}

// URL del PDF original para abrir con #page=N en pestaña nueva (D12).
// No hace fetch: el navegador abre esta URL cuando el usuario hace clic.
export function urlContenido(documentId) {
  return `${BASE}/documents/${documentId}/content`;
}

// URL de descarga directa del PDF (no se usa en la cita navegable, pero
// queda expuesta por si el frontend la necesita para un visor embebido
// en una iteración futura).
export function urlContenidoPagina(documentId, page) {
  return `${urlContenido(documentId)}#page=${page}`;
}
