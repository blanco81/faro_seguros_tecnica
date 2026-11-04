"""El set de evaluación: preguntas etiquetadas contra TRES pólizas (D28/D29).

La deuda quedó escrita en D20: «Calibración con set formal etiquetado
(precision@k, recall@k) y curva ROC: queda para la Fase 5». D28 cerró la
métrica contra una póliza; D29 completa el entregable que pedían el plan
y el enunciado: **documentos y preguntas de ejemplo** — tres pólizas
(hogar, auto, vida) con 19 preguntas en dominio (página relevante + snippet
VERBATIM de la respuesta) y 2 fuera de dominio que no deben recuperar nada
de la base (el umbral D13 las tiene que dejar fuera).

Anclas de integridad (las tres verificadas automáticamente):

- ``DOCUMENTOS_EVAL``: cada póliza del set está anclada a ESE archivo
  exacto por ``sha256``. El runner localiza cada documento en la base por
  ``content_hash`` (único, D03), no por filename — un nombre puede
  repetirse con contenido distinto, un hash no.
- La suite extrae y trocea los PDF commiteados con el MISMO pipeline de
  producción (extracción local + chunker D11) y verifica que cada snippet
  existe verbatim en una página declarada relevante DE SU DOCUMENTO
  (tests/test_evaluation).
- El runner repite esa verificación en cada corrida contra los chunks
  REALES de la base: las etiquetas se validan solas contra la realidad.

Etiquetado a nivel de PÁGINA + snippet: la página es la granularidad de la
cita navegable (D12/D21) y estable frente al chunking (D11); el snippet es
la granularidad de la cita literal y hace al hit@k verificable sin juicio
humano — nadie tiene que opinar si un chunk es "parecido": o contiene el
texto o no lo contiene.

Los snippets se copiaron del texto extraído de los PDF (verificación
arriba); las preguntas se redactaron como las escribiría un analista de
siniestros, NO copiando la cláusula (una copia inflaría artificialmente
los scores de similitud y la evaluación no mediría recuperación, sino
memorización).

Las tres pólizas comparten léxico de oficio (franquicia, exclusiones,
plazos en días…): por eso la búsqueda en dominio va ACOTADA al documento
del caso (D28) — mide embedizar + rankear sin el ruido de las otras dos —
y las preguntas de las cláusulas gemelas (p. ej. la franquicia de lunas
del auto y la de daños por agua del hogar) son la prueba real de que el
acote funciona: sin él, los distractores serían casi clones.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DOCUMENTOS_EVAL",
    "EVAL_SET",
    "EvalCase",
    "EvalDocumento",
    "validate_eval_set",
]


@dataclass(frozen=True)
class EvalDocumento:
    """Una póliza del set, anclada al archivo exacto que la etiqueta.

    ``clave`` es el identificador corto que llevan los casos (``hogar``,
    ``auto``, ``vida``); ``filename`` es el que sube el smoke y el que
    muestra la API; ``sha256`` es el ANCLA (el ``content_hash`` de la
    subida, D03).
    """

    clave: str
    filename: str
    sha256: str
    paginas: int


#: Las tres pólizas del entregable (Fase 6). Los sha256 son los de los
#: PDF commiteados en scripts/data/ — la suite pinea el par PDF ↔ hash;
#: si se sustituye un PDF, su hash, sus etiquetas y este dict se revisan
#: juntos.
DOCUMENTOS_EVAL: tuple[EvalDocumento, ...] = (
    EvalDocumento(
        clave="hogar",
        filename="poliza-ejemplo.pdf",
        sha256="2243537308fca9fe351d47522a53ed0da8a23728e1730fe91ecd9f09df0a350e",
        paginas=2,
    ),
    EvalDocumento(
        clave="auto",
        filename="poliza-auto.pdf",
        sha256="1717996366b7f20cb51b064756914459b4296ad3e4d885e12da666336d5d0fa7",
        paginas=2,
    ),
    EvalDocumento(
        clave="vida",
        filename="poliza-vida.pdf",
        sha256="e403898dc163b82b79bf4057878c9932a76e3e79a8c1df74365619065a673d7d",
        paginas=2,
    ),
)


@dataclass(frozen=True)
class EvalCase:
    """Un caso etiquetado: la pregunta y su verdad conocida.

    - En dominio (``fuera_de_dominio=False``): ``documento`` es la clave
      de la póliza donde vive la respuesta, ``paginas_relevantes`` no
      vacío y ``snippets`` no vacío — la evidencia que la recuperación
      debe alcanzar.
    - Fuera de dominio: sin documento ni páginas ni snippets. Nada de lo
      que haya en la base es evidencia para la pregunta (D13: debe
      rechazarse, no responderse "con lo que haya").
    """

    id: str
    pregunta: str
    documento: str = ""
    paginas_relevantes: frozenset[int] = frozenset()
    snippets: tuple[str, ...] = ()
    fuera_de_dominio: bool = False


#: El set. Hogar: 10 casos (uno multi-página: límite Y franquicia de daños
#: por agua, en cláusulas de páginas distintas — recall@k con
#: |relevantes| > 1 de verdad). Auto y vida: las preguntas del plan
#: (lunas, asistencia, RC, renovación, exclusiones, capital, pago).
#: Fuera de dominio: la del smoke 15B y una de otro oficio, para que el
#: 0-aceptados no dependa de un único ejemplo.
EVAL_SET: tuple[EvalCase, ...] = (
    # --- hogar (poliza-ejemplo.pdf) ------------------------------------
    EvalCase(
        id="suma_asegurada",
        pregunta="¿Cuál es la suma asegurada de la póliza?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("Suma asegurada: 180.000 euros",),
    ),
    EvalCase(
        id="plazo_comunicacion",
        pregunta="¿En cuántos días debo comunicar el siniestro a la aseguradora?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("siete días naturales desde que el asegurado tenga conocimiento",),
    ),
    EvalCase(
        id="limite_danos_agua",
        pregunta="¿Hasta qué importe máximo cubre la póliza los daños por agua?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("máximo de 30.000 euros por siniestro",),
    ),
    EvalCase(
        id="alcance_danos_agua",
        pregunta="¿Qué cosas quedan cubiertas si hay una inundación en el hogar?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=(
            ("daños a la estructura del inmueble, al mobiliario y a los "
             "electrodomésticos"),
        ),
    ),
    EvalCase(
        id="asegurado_vigencia",
        pregunta="¿Quién es la persona asegurada y hasta cuándo tiene vigencia la póliza?",
        documento="hogar",
        paginas_relevantes=frozenset({1}),
        snippets=("Asegurado: María Fernández Rubio",),
    ),
    EvalCase(
        id="exclusiones",
        pregunta="¿Qué daños no están cubiertos por la póliza?",
        documento="hogar",
        paginas_relevantes=frozenset({2}),
        snippets=("falta de mantenimiento comprobada de las instalaciones de agua",),
    ),
    EvalCase(
        id="franquicia_danos_agua",
        pregunta="¿Cuánto es la franquicia para los daños por agua?",
        documento="hogar",
        paginas_relevantes=frozenset({2}),
        snippets=("franquicia de 300 euros por siniestro",),
    ),
    EvalCase(
        id="documentacion_siniestro",
        pregunta="¿Qué documentos tengo que presentar cuando hay un siniestro?",
        documento="hogar",
        paginas_relevantes=frozenset({2}),
        snippets=("denuncia o parte del siniestro, la relación de bienes dañados",),
    ),
    EvalCase(
        id="plazo_pronunciamiento",
        pregunta="¿Cuánto tarda la aseguradora en pronunciarse sobre la cobertura?",
        documento="hogar",
        paginas_relevantes=frozenset({2}),
        snippets=("plazo de cuarenta días para pronunciarse",),
    ),
    EvalCase(
        id="danos_agua_limite_y_franquicia",
        pregunta="¿Qué límite y qué franquicia aplican a los daños por agua?",
        documento="hogar",
        paginas_relevantes=frozenset({1, 2}),
        snippets=(
            "máximo de 30.000 euros por siniestro",
            "franquicia de 300 euros por siniestro",
        ),
    ),
    # --- auto (poliza-auto.pdf) ----------------------------------------
    EvalCase(
        id="lunas_cristales",
        pregunta="¿Qué lunas del coche quedan cubiertas por la póliza?",
        documento="auto",
        paginas_relevantes=frozenset({1}),
        snippets=(
            ("la rotura y las grietas del parabrisas, de las lunas "
             "laterales, de la luna trasera y del techo solar del "
             "vehículo asegurado"),
        ),
    ),
    EvalCase(
        id="franquicia_lunas",
        pregunta="¿Tengo que pagar franquicia si se me rompe una luna del coche?",
        documento="auto",
        paginas_relevantes=frozenset({1}),
        snippets=("franquicia de 150 euros por siniestro",),
    ),
    EvalCase(
        id="asistencia_carretera",
        pregunta="¿Qué cubre la asistencia en carretera si el coche queda inmovilizado?",
        documento="auto",
        paginas_relevantes=frozenset({2}),
        snippets=("grúa hasta 100 kilómetros y vehículo de sustitución durante tres días",),
    ),
    EvalCase(
        id="rc_terceros",
        pregunta="¿Hasta qué importe está cubierta la responsabilidad civil con el coche?",
        documento="auto",
        paginas_relevantes=frozenset({2}),
        snippets=(
            ("hasta 300.000 euros por siniestro por los daños personales y "
             "materiales causados a terceros"),
        ),
    ),
    EvalCase(
        id="renovacion_auto",
        pregunta="¿Se renueva la póliza del coche automáticamente cada año?",
        documento="auto",
        paginas_relevantes=frozenset({2}),
        snippets=("se renueva de forma tácita por períodos anuales",),
    ),
    # --- vida (poliza-vida.pdf) ----------------------------------------
    EvalCase(
        id="capital_fallecimiento",
        pregunta="¿Cuánto cobran los beneficiarios si el asegurado fallece?",
        documento="vida",
        paginas_relevantes=frozenset({1}),
        snippets=("capital asegurado de 250.000 euros",),
    ),
    EvalCase(
        id="beneficiarios",
        pregunta="¿Quiénes son los beneficiarios si no se ha designado a nadie?",
        documento="vida",
        paginas_relevantes=frozenset({1}),
        snippets=("el cónyuge del asegurado y, en su defecto, los hijos por partes iguales",),
    ),
    EvalCase(
        id="exclusiones_vida",
        pregunta="¿Qué causas de fallecimiento no están cubiertas por el seguro de vida?",
        documento="vida",
        paginas_relevantes=frozenset({2}),
        snippets=(
            ("deportes de riesgo no declarados, de la autolesión del "
             "asegurado y de la participación en actos de guerra"),
        ),
    ),
    EvalCase(
        id="plazo_pago_vida",
        pregunta="¿En cuántos días paga la aseguradora el capital del seguro de vida?",
        documento="vida",
        paginas_relevantes=frozenset({2}),
        snippets=("plazo máximo de treinta días",),
    ),
    # --- fuera de dominio ------------------------------------------------
    EvalCase(
        id="receta_tortilla",
        pregunta="¿Cuál es la receta tradicional de la tortilla de patatas?",
        fuera_de_dominio=True,
    ),
    EvalCase(
        id="depreciacion_contable",
        pregunta="¿Cómo se calcula la depreciación de los activos fijos en contabilidad?",
        fuera_de_dominio=True,
    ),
)


def validate_eval_set(
    casos: Sequence[EvalCase],
    *,
    paginas_por_documento: Mapping[str, int],
) -> None:
    """Valida la FORMA del set (la correspondencia con los PDF la verifican
    la suite y el runner contra los chunks reales).

    Args:
        casos: el set a validar.
        paginas_por_documento: clave de ``EvalDocumento`` → páginas que
            tiene ESE documento en la realidad (del PDF commiteado en la
            suite, de los chunks de la base en el runner).

    Raises:
        ValueError: con el id del caso culpable en el mensaje — accionable
            para quien etiqueta.
    """
    if not casos:
        raise ValueError("set de evaluación vacío")
    ids = [caso.id for caso in casos]
    duplicados = {id_ for id_ in ids if ids.count(id_) > 1}
    if duplicados:
        raise ValueError(f"ids duplicados en el set: {sorted(duplicados)}")

    en_dominio = [caso for caso in casos if not caso.fuera_de_dominio]
    fuera = [caso for caso in casos if caso.fuera_de_dominio]
    if not en_dominio:
        raise ValueError("sin casos en dominio no hay métricas de recuperación")
    if not fuera:
        raise ValueError(
            "sin casos fuera de dominio no se mide la seguridad del umbral (D13)"
        )

    for caso in casos:
        if not caso.pregunta.strip():
            raise ValueError(f"pregunta vacía: {caso.id}")
        if caso.fuera_de_dominio:
            if caso.documento:
                raise ValueError(
                    f"caso fuera de dominio anclado a un documento: {caso.id} — "
                    "fuera de dominio significa que NADA de la base es evidencia"
                )
            if caso.paginas_relevantes or caso.snippets:
                raise ValueError(
                    f"caso fuera de dominio con etiquetas: {caso.id} — fuera de "
                    "dominio significa que NADA de la base es evidencia"
                )
        else:
            if not caso.documento:
                raise ValueError(
                    f"caso en dominio sin documento: {caso.id} — la evidencia "
                    "vive en UNA póliza del set"
                )
            if caso.documento not in paginas_por_documento:
                raise ValueError(
                    f"{caso.id}: documento desconocido '{caso.documento}' "
                    f"(conocidos: {sorted(paginas_por_documento)})"
                )
            if not caso.paginas_relevantes:
                raise ValueError(f"caso en dominio sin páginas relevantes: {caso.id}")
            if not caso.snippets:
                raise ValueError(f"caso en dominio sin snippets: {caso.id}")
            rango = frozenset(
                range(1, paginas_por_documento[caso.documento] + 1)
            )
            fuera_de_rango = caso.paginas_relevantes - rango
            if fuera_de_rango:
                raise ValueError(
                    f"{caso.id}: páginas relevantes fuera de '{caso.documento}' "
                    f"({sorted(fuera_de_rango)}; el documento tiene "
                    f"{paginas_por_documento[caso.documento]})"
                )
