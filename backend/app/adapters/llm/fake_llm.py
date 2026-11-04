"""LLM simulado (D14): respuestas deterministas sin red ni claves.

El enunciado exige consulta con proveedor simulado. A diferencia de un
eco tonto, este fake produce respuestas ÚTILES para el bucle agéntico:
lee el contexto que el propio servicio le envía y extrae de ahí la cita.
Así el ciclo completo (planificar → recuperar → generar → verificar)
corre de punta a punta en la suite y en el stack con ``LLM_PROVIDER=fake``.

El mecanismo es explícito y frágil A PROPÓSITO: el fake se ancla a las
marcas ``TAREA:`` que el servicio pone al inicio de cada prompt. Si el
servicio cambia sus marcas, el fake se desincroniza y los tests lo
detectan al instante — mejor eso que un fake "inteligente" que tape
cambios de contrato.

La suite de unitarios no usa este fake para los casos de fallo (parafraseo,
rechazo, techo): usa el ``ScriptedLLM`` de ``tests/fakes.py``, que devuelve
respuestas encoladas y permite programar el desenlace de cada iteración.
"""

import json
import re

__all__ = ["FakeLLMProvider"]

#: Un fragmento del contexto, aislado como dato (D08):
#: '<fragmento numero="3" documento="X" pagina="2">\n<texto>\n</fragmento>'
_FRAGMENTO = re.compile(
    r'<fragmento numero="\d+" documento="[^"]*" pagina="\d+">\n(.+?)\n</fragmento>',
    re.DOTALL,
)
#: La línea de pregunta del prompt: "PREGUNTA: ¿...?"
_PREGUNTA = re.compile(r"PREGUNTA:\s*(.+)")


class FakeLLMProvider:
    """LLMProvider determinista anclado a las marcas ``TAREA:`` del servicio."""

    def chat(self, prompt: str, *, system: str | None = None) -> str:
        texto = f"{system or ''}\n{prompt}"

        if "TAREA: PLANIFICAR" in texto:
            # Sin descomposición: la pregunta tal cual (una sola sub-pregunta).
            return json.dumps({"subquestions": [self._pregunta(texto)]})

        if "TAREA: REFINAR" in texto:
            # Sin reformulación: reintentar con la pregunta original.
            return json.dumps({"query": self._pregunta(texto)})

        # TAREA: RESPONDER — cita la primera oración del primer fragmento.
        match = _FRAGMENTO.search(prompt)
        if match is None:
            return json.dumps({"answer": "", "quote": ""})
        fragmento = " ".join(match.group(1).split())
        oracion = fragmento.split(". ")[0].strip(".")
        cita = oracion[:220]
        return json.dumps({"answer": f"Según el documento: {cita}.", "quote": cita})

    @staticmethod
    def _pregunta(texto: str) -> str:
        match = _PREGUNTA.search(texto)
        return match.group(1).strip() if match else ""
