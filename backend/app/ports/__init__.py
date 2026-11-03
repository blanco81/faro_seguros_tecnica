"""Puertos (interfaces propias) del núcleo.

El dominio y los servicios dependen de estas abstracciones; los adapters las
implementan. Fase 2: TextExtractor y EmbeddingProvider (con sus adapters
pypdf/texto-plano y OpenAI-compatible/fake). La Fase 3 añade LLMProvider y
VectorSearch. El adapter fake de cada puerto es lo que permite los tests
sin red ni claves (decisión D01 de DECISIONS.md).
"""
