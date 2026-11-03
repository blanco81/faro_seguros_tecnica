# faro_seguros_tecnica
MVP en el que un analista sube documentos, el sistema los procesa en segundo plano (extracción de texto, chunking, embeddings, almacenamiento en PostgreSQL con pgvector) y luego hace preguntas en lenguaje natural. La respuesta debe llegar con citas verificables (documento, página y fragmento). Si la evidencia es insuficiente, sistema debe decirlo
