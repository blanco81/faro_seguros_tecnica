import { useCallback, useEffect, useState } from "react";
import "./styles.css";
import { listarDocumentos } from "./api";
import DocumentsView from "./components/DocumentsView.jsx";
import QueryView from "./components/QueryView.jsx";

// Shell de la aplicación (Fase 4).
//
// Mantiene la lista de documentos en el estado del shell — la comparten
// las dos vistas:
//   - DocumentsView la muestra y la refresca tras cada subida.
//   - QueryView la usa para el <select> de "acotar a un documento" y
//     para mapear filename -> id en las citas navegables (la cita del
//     agente trae el filename, no el id; el endpoint /content recibe id).
//
// El polling de estado (pending -> ready) vive aquí: un useEffect que
// llama a listarDocumentos cada 3s mientras haya al menos un documento
// en pending/processing. En cuanto todos están listos/failed, para.

const PENDING = new Set(["pending", "processing"]);

export default function App() {
  const [tab, setTab] = useState("documentos");
  const [documentos, setDocumentos] = useState([]);
  const [errorCarga, setErrorCarga] = useState(null);
  const [ultimoRefresco, setUltimoRefresco] = useState(null);

  const refrescar = useCallback(async () => {
    try {
      const docs = await listarDocumentos();
      setDocumentos(docs);
      setErrorCarga(null);
      setUltimoRefresco(new Date());
    } catch (e) {
      // La primera carga fallida es crítica; las siguientes son warning
      // (puede ser un refresco intermitente mientras se reinicia la API).
      if (documentos.length === 0) setErrorCarga(e.message);
    }
  }, [documentos.length]);

  // Carga inicial.
  useEffect(() => {
    refrescar();
  }, [refrescar]);

  // Polling: solo mientras haya algo en cola. 3s (D20 — ajuste simple,
  // sin WebSocket para esta MVP).
  useEffect(() => {
    const hayPendientes = documentos.some((d) => PENDING.has(d.status));
    if (!hayPendientes) return;
    const id = setInterval(refrescar, 3000);
    return () => clearInterval(id);
  }, [documentos, refrescar]);

  return (
    <main className="app">
      <header className="app-header">
        <h1>Faro Seguros · Asistente RAG</h1>
        <div className="sub">
          Asistente de siniestros con citas verificables ·{" "}
          {documentos.length} documento(s) cargado(s)
          {ultimoRefresco && (
            <> · actualizado a las {ultimoRefresco.toLocaleTimeString("es")}</>
          )}
        </div>
      </header>

      {errorCarga && (
        <div className="error-msg">
          No se pudo conectar con la API: {errorCarga}. Verifica que el
          stack esté levantado (<code>make up</code>).
        </div>
      )}

      <nav className="tabs" role="tablist">
        <button
          className={`tab ${tab === "documentos" ? "activa" : ""}`}
          onClick={() => setTab("documentos")}
          role="tab"
        >
          Documentos
        </button>
        <button
          className={`tab ${tab === "consulta" ? "activa" : ""}`}
          onClick={() => setTab("consulta")}
          role="tab"
        >
          Consulta
        </button>
      </nav>

      {tab === "documentos" ? (
        <DocumentsView documentos={documentos} onSubida={refrescar} />
      ) : (
        <QueryView documentos={documentos} />
      )}
    </main>
  );
}
