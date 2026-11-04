"""Evaluación de recuperación: set etiquetado + precision@k / recall@k
(D28; tres pólizas en D29).

El paquete que cierra la deuda de calibración de D20 («set formal
etiquetado, precision@k, recall@k y curva ROC: queda para la Fase 5»):

- ``metrics``: las métricas puras (P@k, R@k, hit@k) sobre la lista
  rankeada del ``RetrievalPort``.
- ``dataset``: el set etiquetado contra las TRES pólizas de
  ``scripts/data/`` (hogar, auto y vida, cada una anclada por sha256) —
  19 preguntas en dominio con página relevante y snippet verbatim,
  2 fuera de dominio.
- ``runner``: lo corre contra el stack real
  (``python -m app.evaluation.runner`` / ``make eval`` / smoke sección 18)
  con el reporte por caso, las medias y el barrido de umbral.

Importar el paquete NO arrastra al runner (y con él SQLAlchemy): el
``__init__`` exporta solo las piezas reutilizables — métricas y set — y
el runner se importa explícito donde se usa. Así ``python -m`` no
re-ejecuta un módulo ya importado (y las piezas puras siguen puras).

No es una perilla de runtime del sistema: es la herramienta de evidencia
para calibrar (D20) y detectar regresiones de recuperación.
"""

from app.evaluation.dataset import (
    DOCUMENTOS_EVAL,
    EVAL_SET,
    EvalCase,
    EvalDocumento,
    validate_eval_set,
)
from app.evaluation.metrics import (
    evidence_hit_at_k,
    normaliza_texto,
    precision_at_k,
    recall_at_k,
)

__all__ = [
    "DOCUMENTOS_EVAL",
    "EVAL_SET",
    "EvalCase",
    "EvalDocumento",
    "evidence_hit_at_k",
    "normaliza_texto",
    "precision_at_k",
    "recall_at_k",
    "validate_eval_set",
]
