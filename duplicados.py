"""
¿Esto ya lo tienes apuntado?

Folio publica en Drive las huellas de lo apuntado en los últimos 90 días (solo fecha,
importe y concepto). Antes de mandar nada, el bot compara: mismo importe, concepto
parecido y la fecha a tres días o menos. Si algo cuadra, lo dice; nunca lo impide, porque
un recibo igual cada mes es normal y dos cafés iguales el mismo día también pasan.

Comparar no cuesta nada: no pasa por OpenAI.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any

MARGEN_DIAS = 3


def llano(texto: Any) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def se_parecen(a: str, b: str) -> bool:
    a, b = llano(a), llano(b)
    if not a or not b:
        return a == b
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.82


def buscar(apunte: dict[str, Any], huellas: list[dict]) -> str | None:
    """Cómo se llama lo que ya tienes y cuándo fue, o None si no hay nada parecido."""
    try:
        fecha = date.fromisoformat(str(apunte.get("fecha") or ""))
    except ValueError:
        return None
    importe = round(float(apunte.get("importe") or 0.0), 2)
    if not importe:
        return None
    concepto = apunte.get("concepto") or apunte.get("activo") or ""
    for otro in huellas or []:
        if round(float(otro.get("i") or 0.0), 2) != importe:
            continue
        try:
            suya = date.fromisoformat(str(otro.get("f") or ""))
        except ValueError:
            continue
        if abs((suya - fecha).days) > MARGEN_DIAS:
            continue
        if not se_parecen(concepto, otro.get("c") or ""):
            continue
        return f"{otro.get('c') or 'sin concepto'} · {suya.strftime('%d/%m/%Y')}"
    return None


def marcar(apuntes: list[dict[str, Any]], huellas: list[dict]) -> int:
    """
    Pone `repetido` en los que ya tengas apuntado, y también en los que se repiten dentro
    del mismo mensaje (una captura con dos filas iguales). Devuelve cuántos ha marcado.
    """
    vistos = list(huellas or [])
    marcados = 0
    for apunte in apuntes:
        aviso = buscar(apunte, vistos)
        apunte["repetido"] = aviso or ""
        marcados += 1 if aviso else 0
        vistos.append({"f": apunte.get("fecha"), "i": apunte.get("importe"),
                       "c": apunte.get("concepto") or apunte.get("activo") or ""})
    return marcados
