// Cita navegable (Fase 4, D12).
//
// Una cita del agente es {document, page, quote} donde `document` es el
// filename (no el id). Para abrir el PDF con #page=N necesitamos el id,
// así que esta función busca en la lista de documentos del shell aquel
// cuyo filename coincide. Si hay varios con el mismo nombre (poco
// probable por idempotencia D03, pero posible), enlazamos el primero.
// Si no encontramos el filename (p. ej., el documento fue borrado),
// mostramos la cita sin link — el quote sigue siendo información útil.

import { urlContenidoPagina } from "../api.js";

export default function Citation({ cita, documentos }) {
  const { document: filename, page, quote } = cita;
  const doc = documentos.find((d) => d.filename === filename);
  const id = doc?.id;

  const meta = (
    <div className="meta">
      {id ? (
        <a
          className="pag-link"
          href={urlContenidoPagina(id, page)}
          target="_blank"
          rel="noopener noreferrer"
        >
          {filename}
        </a>
      ) : (
        <span>{filename}</span>
      )}
      <span className="separador">·</span>
      <span>página {page}</span>
    </div>
  );

  return (
    <div className="cita">
      {meta}
      <blockquote>“{quote}”</blockquote>
    </div>
  );
}
