import { useState } from "react";
import { consultar } from "../api.js";
import Citation from "./Citation.jsx";

// Vista de Consulta (Fase 4, D19).
//
// Pregunta -> POST /api/query -> QueryOut con tres desenlaces posibles:
//   - refused=false con answer + citations: la respuesta con su cita.
//   - refused=true con message + reason: el dominio se negó (D13).
//   - clarification con texto: el planificador pidió aclaración.
//
// El <select> de documentos es opcional — acota la consulta a una póliza
// concreta (el retriever filtra por document_id). Por defecto "Todos"
// busca en todos los documentos listos.

export default function QueryView({ documentos }) {
  const [question, setQuestion] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [resultado, setResultado] = useState(null);
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState(null);

  // Solo tiene sentido acotar a documentos ya ingested (ready).
  const listos = documentos.filter((d) => d.status === "ready");

  async function onSubmit(e) {
    e.preventDefault();
    const q = question.trim();
    if (q.length < 3) {
      setError("Escribe una pregunta más larga (mínimo 3 caracteres).");
      return;
    }
    setEnviando(true);
    setError(null);
    setResultado(null);
    try {
      const r = await consultar(q, documentId || null);
      setResultado(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setEnviando(false);
    }
  }

  return (
    <>
      <section className="panel">
        <h2>Preguntar al asistente</h2>
        {listos.length === 0 && (
          <div className="vacio" style={{ padding: "0.75rem" }}>
            No hay documentos listos para consultar todavía. Sube y espera
            a que la ingesta termine en la pestaña Documentos.
          </div>
        )}
        <form onSubmit={onSubmit}>
          <textarea
            className="consulta-input"
            placeholder="Ej.: ¿Cuál es la franquicia aplicable a la cobertura de daños por agua?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            maxLength={500}
            disabled={enviando}
          />
          <div className="fila-form">
            <select
              className="select-doc"
              value={documentId}
              onChange={(e) => setDocumentId(e.target.value)}
              disabled={enviando}
            >
              <option value="">Todos los documentos</option>
              {listos.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.filename}
                </option>
              ))}
            </select>
            <button type="submit" className="btn" disabled={enviando}>
              {enviando ? "Preguntando…" : "Preguntar"}
            </button>
            {question.length > 0 && (
              <span className="footer-status">
                {question.length}/500
              </span>
            )}
          </div>
        </form>

        {error && <div className="error-msg">{error}</div>}
      </section>

      {resultado && <PanelRespuesta resultado={resultado} documentos={documentos} />}
    </>
  );
}

function PanelRespuesta({ resultado, documentos }) {
  // 503 no llega aquí — el cliente HTTP lanza y se captura como error
  // arriba. Aquí solo entran los tres desenlaces del dominio.
  if (resultado.clarification) {
    return (
      <section className="panel">
        <h2>Aclaración</h2>
        <div className="respuesta clarification">
          <div className="label">El planificador necesita más contexto</div>
          <div className="texto">{resultado.clarification}</div>
        </div>
      </section>
    );
  }

  if (resultado.refused) {
    return (
      <section className="panel">
        <h2>No puedo responder</h2>
        <div className="respuesta refused">
          <div className="label">Rechazo ({resultado.reason})</div>
          <div className="texto">
            {resultado.message ?? "La evidencia recuperada no alcanza."}
          </div>
        </div>
        <div className="iteraciones">
          {resultado.iterations} iteración(es) del agente.
        </div>
      </section>
    );
  }

  // refused=false con answer
  return (
    <section className="panel">
      <h2>Respuesta</h2>
      <div className="respuesta">
        <div className="label">Respuesta del asistente</div>
        <div className="texto">{resultado.answer}</div>
      </div>

      {resultado.citations?.length > 0 && (
        <>
          <div className="label" style={{ marginTop: "1rem" }}>
            Cita verificada (D12)
          </div>
          {resultado.citations.map((c, i) => (
            <Citation key={i} cita={c} documentos={documentos} />
          ))}
        </>
      )}

      <div className="iteraciones">
        {resultado.iterations} iteración(es) del agente.
      </div>
    </section>
  );
}
