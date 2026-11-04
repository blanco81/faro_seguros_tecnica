#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""arregla_makefile.py - Repara un Makefile cuyas lineas de receta quedaron
indentadas con espacios en lugar de TAB.

Sintoma exacto que corrige (make aborta en la PRIMERA linea mala, pero
normalmente hay varias):

    Makefile:5: *** separador em falta (queria usar TAB em vez de 8 espacos?).  Parar.
    Makefile:5: *** missing separator (did you mean TAB instead of 8 spaces?).  Stop.

Causa: el Makefile fue reescrito por un script/instalador y los TAB de las
recetas quedaron aplanados a espacios. GNU make EXIGE que cada linea de
receta (el cuerpo de una regla "objetivo:") empiece por TAB; las
asignaciones (VAR := ...) y las directivas (ifeq/endif/include...) no
necesitan TAB.

Que hace este script:

  1. Copia de seguridad  ->  Makefile.bak.<AAAAMMDD>-<HHMMSS>
  2. Convierte a TAB el sangrado de las lineas de RECETA indentadas con
     espacios (solo dentro de reglas; no toca asignaciones, directivas,
     lineas que ya empiezan por TAB ni el contenido de ninguna linea).
  3. Muestra un diff unificado de los cambios.
  4. Se valida a si mismo ejecutando `make -n` (en seco: no ejecuta nada).
  5. Es idempotente: correrlo de nuevo no cambia nada.

Nota anti-corrupcion: este archivo NO contiene ningun caracter TAB (los
TAB se generan con chr(9)), asi que sobrevive cualquier copy/paste.

Uso (desde la raiz del proyecto, junto al Makefile):

    python3 arregla_makefile.py
    python3 arregla_makefile.py Makefile
    python3 arregla_makefile.py --dry-run      # solo mostrar el diff
    python3 arregla_makefile.py --sin-backup   # no crear copia .bak
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import re
import shutil
import subprocess
import sys

TAB = chr(9)  # TAB generado en runtime, nunca literal en este archivo

# Directivas de GNU make: indentadas o no, nunca se convierten en receta.
_DIRECTIVAS = frozenset((
    "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif",
    "include", "-include", "sinclude", "export", "unexport",
    "define", "endef", "override", "undefine", "vpath", "load",
))


def _es_directiva(sin_sangrado: str) -> bool:
    partes = sin_sangrado.split(None, 1)
    return bool(partes) and partes[0] in _DIRECTIVAS


def _es_regla(sin_sangrado: str) -> bool:
    """True si la linea abre una regla 'objetivo(s): [prerequisitos]'."""
    if not sin_sangrado or sin_sangrado.startswith("#"):
        return False
    cuerpo = sin_sangrado.split("#", 1)[0]
    if ":" not in cuerpo:
        return False
    if "=" in cuerpo.split(":", 1)[0]:
        return False  # asignacion: "VAR = x" / "VAR := x"
    for i, ch in enumerate(cuerpo):
        if ch == ":" and cuerpo[i + 1:i + 2] != "=":
            return True  # ':' que no forma parte de ':='
    return False


def reparar(texto: str) -> tuple[str, int]:
    """Devuelve (texto_reparado, numero_de_lineas_de_receta_corregidas).

    Solo cambia el sangrado inicial (espacios -> TAB) de las lineas de
    receta; el resto del contenido de cada linea queda byte a byte igual.
    """
    out: list[str] = []
    corregidas = 0
    en_regla = False  # True tras ver un "objetivo:" en columna 0
    for cruda in texto.splitlines(keepends=True):
        if cruda.endswith("\r\n"):
            cuerpo, fin = cruda[:-2], "\r\n"
        elif cruda.endswith("\n"):
            cuerpo, fin = cruda[:-1], "\n"
        else:
            cuerpo, fin = cruda, ""
        if not cuerpo.strip():            # linea en blanco: no altera contexto
            out.append(cruda)
            continue
        if cuerpo.startswith(TAB):        # ya es receta correcta
            out.append(cruda)
            continue
        if cuerpo.startswith(" "):        # sangrada con espacios...
            s = cuerpo.lstrip(" ")
            if en_regla and not _es_directiva(s):
                out.append(TAB + s + fin)     # ...era receta: a TAB
                corregidas += 1
            else:
                out.append(cruda)          # directiva o continuacion legitima
            continue
        # Linea en columna 0: actualiza el contexto.
        if _es_regla(cuerpo):
            en_regla = True
        elif cuerpo.startswith("#"):
            pass  # comentario: make lo ignora, no cierra la regla
        elif not _es_directiva(cuerpo):
            en_regla = False  # asignacion u otra cosa: cierra la regla
        out.append(cruda)
    return "".join(out), corregidas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Repara lineas de receta con espacios en un Makefile.")
    ap.add_argument("archivo", nargs="?", default="Makefile",
                    help="ruta del Makefile (por defecto: ./Makefile)")
    ap.add_argument("--dry-run", action="store_true",
                    help="solo mostrar el diff; no escribir nada")
    ap.add_argument("--sin-backup", action="store_true",
                    help="no crear Makefile.bak.*")
    args = ap.parse_args(argv)

    try:
        with open(args.archivo, "r", encoding="utf-8", newline="") as fh:
            original = fh.read()
    except OSError as exc:
        print("[ERROR] no pude leer %s: %s" % (args.archivo, exc),
              file=sys.stderr)
        return 2

    reparado, n = reparar(original)

    if n == 0:
        print("[OK] %s no tiene recetas con espacios: nada que reparar."
              % args.archivo)
    else:
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), reparado.splitlines(),
            fromfile=args.archivo + " (roto)",
            tofile=args.archivo + " (reparado)",
            lineterm=""))
        print(diff)
        print("")
        print("[i] Se corrigieron %d linea(s) de receta (espacios -> TAB)." % n)

    if args.dry_run:
        print("[i] --dry-run: no se escribio nada.")
    elif n:
        if not args.sin_backup:
            sello = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = "%s.bak.%s" % (args.archivo, sello)
            shutil.copy2(args.archivo, backup)
            print("[i] Copia de seguridad: %s" % backup)
        with open(args.archivo, "w", encoding="utf-8", newline="") as fh:
            fh.write(reparado)
        print("[i] Escrito: %s" % args.archivo)

    # Validacion en seco: `make -n` solo parsea e imprime, no ejecuta nada.
    try:
        r = subprocess.run(["make", "-n", "-f", args.archivo],
                           capture_output=True, text=True)
    except FileNotFoundError:
        print("[i] `make` no esta disponible aqui; valida luego con `make -n`.")
        return 0
    if r.returncode == 0:
        print("[OK] `make -n` parsea el Makefile sin errores. Listo.")
        return 0
    print("[ERROR] `make -n` todavia falla despues de la reparacion:")
    print(r.stderr.strip())
    print("[i] Pega ese error junto con `sed -n '1,15p' %s` para revisarlo."
          % args.archivo)
    return 1


if __name__ == "__main__":
    sys.exit(main())
