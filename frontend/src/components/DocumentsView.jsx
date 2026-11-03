import { useRef, useState } from "react";
import { subirDocumento } from "../api.js";

// Vista de Documentos (Fase 4).
//
// Dos bloques:
//   1. Zona drag&drop para subir PDFs (D08 valida tipo/tamaño en backend).
//   2. Tabla con el estado de cada documento — polling del shell refresca
//      cada 3s mientras haya algo en pending/processing.
//
// El estado de subida local (enviando / error) NO se comparte con la
// consulta: la subida dispara onSubida() que hace refrescar al shell,
// y el shell propaga la lista nueva por props.

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

export default function DocumentsView({ documentos, onSubida }) {
  const [arrastrando, setArrastrando] = useState(false);
  const [subiendo, setSubiendo] = useState(false);
  const [errorSubida, setErrorSubida] = useState(null);
  const [ultimoSubido, setUltimoSubido] = useState(null);
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
              </tr>
            </thead>
            <tbody>
              {documentos.map((d) => (
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
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
