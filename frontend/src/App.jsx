// Placeholder de Fase 0 — la interfaz real (subir/estado, preguntar, citas)
// llega en la Fase 4 del cronograma.
export default function App() {
  return (
    <main style={{ fontFamily: 'system-ui, sans-serif', maxWidth: 720, margin: '4rem auto', padding: '0 1rem' }}>
      <h1>Faro Seguros · Asistente RAG</h1>
      <p>
        MVP en construcción (Fase 0 de 6): infraestructura Docker, esquema de datos y CI.
        La interfaz de analistas se implementa en la Fase 4.
      </p>
      <p style={{ color: '#555' }}>
        Estado de la API: <a href="/api/health">/api/health</a> (cuando el backend esté levantado).
      </p>
    </main>
  )
}
