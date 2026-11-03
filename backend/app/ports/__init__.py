"""Puertos (interfaces propias) del núcleo.

El dominio y los servicios dependen de estas abstracciones; los adapters las
implementan. La Fase 2 añade TextExtractor y EmbeddingProvider; la Fase 3,
LLMProvider y VectorSearch. El adapter fake de cada puerto es lo permite
los tests sin red ni claves (decisión D01 de DECISIONS.md).
"""
