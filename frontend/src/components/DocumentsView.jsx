import { useRef, useState } from "react";
import { eliminarDocumento, subirDocumento } from "../api.js";

// Vista de Documentos (Fase 4).
//
// Dos bloques:
//   1. Zona drag&drop para subir PDFs (D08 valida tipo/tamaño en backend).
//   2. Tabla con el estado de cada documento — polling del shell refresca
//      cada 3s mientras haya algo en pending/processing.
//
// El estado de subida local (enviando / error) NO se comparte con la
// consulta: la subida dispara onSubida() que hace refrescar al shell,
// y el shell propaga la lista nueva por props. El borrado (D30) reusa
// el mismo callback: es el "lista, refresca" del shell.

const MIME_PERMITIDOS = ["application/pdf", "text/plain"];

function formatearTamano(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function formatearFecha(iso) {
  try {
    // "4 nov 2026, 0:04": día, mes abreviado y año explícitos — el formato
    // corto por defecto ("4/11/26") es ambiguo entre locales dd/mm y mm/dd.
    return new Date(iso).toLocaleString("es", {
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

const ETIQUETA_ESTADO = {
  pending: "En cola",
  processing: "Procesando",
  ready: "Listo",
  failed: "Falló",
};

// Estados con la tarea de ingesta en vuelo: el backend rechaza el borrado
// con 409 (D30) — el botón lo anticipa deshabilitándose (espejo del API).
const EN_INGESTA = new Set(["pending", "processing"]);

export default function DocumentsView({ documentos, onSubida }) {
  const [arrastrando, setArrastrando] = useState(false);
  const [subiendo, setSubiendo] = useState(false);
  const [errorSubida, setErrorSubida] = useState(null);
  const [ultimoSubido, setUltimoSubido] = useState(null);
  const [borrando, setBorrando] = useState(null); // id en vuelo (D30)
  const [errorBorrado, setErrorBorrado] = useState(null);
  const inputRef = useRef(null);

  async function manejarArchivo(file) {
    if (!file) return;
    setSubiendo(true);
    setErrorSubida(null);
    try {
      const doc = await subirDocumento(file);
      setUltimoSubido(doc);
      await onSubida();
    } catch (e) {
      setErrorSubida(e.message);
    } finally {
      setSubiendo(false);
    }
  }

  async function manejarBorrado(doc) {
    // Confirmación explícita: el borrado arrastra chunks, embeddings y el
    // archivo original (FK CASCADE, D30) — y liberar el hash permite
    // re-subirlo después, pero lo borrado no vuelve.
    const seguro = window.confirm(
      `¿Eliminar "${doc.filename}"?\n` +
        `Se borran también sus ${doc.chunk_count} chunk(s) con embeddings ` +
        `y el archivo original. No se puede deshacer.`
    );
    if (!seguro) return;
    setBorrando(doc.id);
    setErrorBorrado(null);
    try {
      await eliminarDocumento(doc.id);
      await onSubida(); // refresca la lista (mismo canal que la subida)
    } catch (e) {
      setErrorBorrado(e.message);
    } finally {
      setBorrando(null);
    }
  }

  function onDrop(e) {
    e.preventDefault();
    setArrastrando(false);
    const file = e.dataTransfer.files?.[0];
    if (file) manejarArchivo(file);
  }

  function onDragOver(e) {
    e.preventDefault();
    if (!arrastrando) setArrastrando(true);
  }

  function onDragLeave(e) {
    e.preventDefault();
    setArrastrando(false);
  }

  function onInputChange(e) {
    const file = e.target.files?.[0];
    if (file) manejarArchivo(file);
    // reset para permitir re-subir el mismo archivo
    e.target.value = "";
  }

  return (
    <>
      <section className="panel">
        <h2>Subir póliza o anexo</h2>
        <label
          className={`dropzone ${arrastrando ? "dragging" : ""}`}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onClick={() => inputRef.current?.click()}
        >
          <div className="icono">📄</div>
          <div>
            <strong>Arrastra aquí</strong> un PDF o haz clic para seleccionarlo
          </div>
          <div style={{ marginTop: "0.4rem", fontSize: "0.75rem" }}>
            PDF o texto plano · máximo 20 MB
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={MIME_PERMITIDOS.join(",")}
            onChange={onInputChange}
          />
        </label>

        {subiendo && <div className="cargando">Subiendo…</div>}
        {errorSubida && (
          <div className="error-msg">No se pudo subir: {errorSubida}</div>
        )}
        {ultimoSubido && (
          <div className="footer-status">
            Última subida:{" "}
            <strong>{ultimoSubido.filename}</strong> —{" "}
            {ETIQUETA_ESTADO[ultimoSubido.status]}
            {ultimoSubido.status === "failed" && ultimoSubido.failure_reason && (
              <> ({ultimoSubido.failure_reason})</>
            )}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Documentos cargados</h2>
        {errorBorrado && (
          <div className="error-msg">No se pudo eliminar: {errorBorrado}</div>
        )}
        {documentos.length === 0 ? (
          <div className="vacio">
            Todavía no hay documentos. Sube uno arriba para empezar.
          </div>
        ) : (
          <table className="tabla-docs">
            <thead>
              <tr>
                <th>Archivo</th>
                <th>Estado</th>
                <th>Págs.</th>
                <th>Chunks</th>
                <th>Tamaño</th>
                <th>Subido</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {documentos.map((d) => {
                const en_ingesta = EN_INGESTA.has(d.status);
                return (
                  <tr key={d.id}>
                    <td className="filename">{d.filename}</td>
                    <td>
                      <span className={`badge ${d.status}`}>
                        {ETIQUETA_ESTADO[d.status]}
                      </span>
                      {d.status === "failed" && d.failure_reason && (
                        <div
                          style={{
                            fontSize: "0.7rem",
                            color: "var(--st-failed)",
                            marginTop: "0.2rem",
                          }}
                        >
                          {d.failure_reason}
                        </div>
                      )}
                    </td>
                    <td>{d.page_count ?? "—"}</td>
                    <td>{d.chunk_count}</td>
                    <td>{formatearTamano(d.size_bytes)}</td>
                    <td>{formatearFecha(d.created_at)}</td>
                    <td>
                      <button
                        className="btn-borrar"
                        onClick={() => manejarBorrado(d)}
                        disabled={borrando === d.id || en_ingesta}
                        title={
                          en_ingesta
                            ? "Espera a que termine la ingesta para borrarlo (HTTP 409)"
                            : "Elimina el documento con sus chunks y embeddings (D30)"
                        }
                      >
                        {borrando === d.id ? "Borrando…" : "Eliminar"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
