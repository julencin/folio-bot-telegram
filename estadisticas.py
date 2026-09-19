"""
Lo que lleva gastado y recibido el bot: por día, por mes y en total, en euros.

Se guarda en `coste.json`, al lado del bot (fuera de git). Cada mensaje que pasa por
OpenAI suma: cuánto ha costado, de qué tipo era (texto, voz o foto), de quién, y cuántos
apuntes ha sacado. `/stats` lo enseña.

El `coste.json` de antes solo guardaba meses ({"2026-09": {"mensajes", "dolares"}}); al
leerlo se pasa solo al formato nuevo, sin perder lo que había.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import precios

TIPOS = ("texto", "voz", "foto")


def _vacio() -> dict[str, Any]:
    return {"formato": 2, "desde": None, "total": _fila(), "meses": {}, "dias": {}, "perfiles": {}}


def _fila() -> dict[str, Any]:
    return {"mensajes": 0, "dolares": 0.0, "apuntes": 0, "tipos": {t: 0 for t in TIPOS}}


def leer(ruta: Path) -> dict[str, Any]:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _vacio()
    if datos.get("formato") == 2:
        return datos
    # Formato viejo: {"AAAA-MM": {"mensajes", "dolares"}}. Se conserva como meses y total.
    nuevo = _vacio()
    for mes, fila in sorted(datos.items()):
        if not isinstance(fila, dict):
            continue
        m = {**_fila(), "mensajes": int(fila.get("mensajes") or 0), "dolares": float(fila.get("dolares") or 0)}
        nuevo["meses"][mes] = m
        nuevo["total"]["mensajes"] += m["mensajes"]
        nuevo["total"]["dolares"] += m["dolares"]
    return nuevo


def apuntar(datos: dict[str, Any], dolares: float, tipo: str = "texto", perfil: str = "",
            apuntes: int = 0, cuando: datetime | None = None) -> dict[str, Any]:
    cuando = cuando or datetime.now()
    tipo = tipo if tipo in TIPOS else "texto"
    dia, mes = cuando.strftime("%Y-%m-%d"), cuando.strftime("%Y-%m")
    datos["desde"] = datos.get("desde") or dia
    for fila in (
        datos["total"],
        datos["meses"].setdefault(mes, _fila()),
        datos["dias"].setdefault(dia, _fila()),
        datos["perfiles"].setdefault(perfil or "?", _fila()),
    ):
        fila.setdefault("tipos", {t: 0 for t in TIPOS})
        fila.setdefault("apuntes", 0)
        fila["mensajes"] += 1
        fila["dolares"] = round(fila["dolares"] + max(0.0, dolares), 8)
        fila["apuntes"] += apuntes
        fila["tipos"][tipo] = fila["tipos"].get(tipo, 0) + 1
    # Los días se guardan solo los últimos 400: los meses y el total ya lo cuentan todo.
    for viejo in sorted(datos["dias"])[:-400]:
        datos["dias"].pop(viejo)
    return datos


def guardar(ruta: Path, datos: dict[str, Any]) -> None:
    temporal = ruta.with_name(ruta.name + ".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    temporal.replace(ruta)


def euros(dolares: float) -> str:
    """En euros, con los decimales que hagan falta para que no salga «0,00 €»."""
    valor = dolares * precios.EUROS_POR_DOLAR
    if valor <= 0:
        return "0 €"
    decimales = 2 if valor >= 1 else 3 if valor >= 0.1 else 4
    return f"{valor:,.{decimales}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def _linea(nombre: str, fila: dict[str, Any] | None) -> str:
    fila = fila or _fila()
    m, a = fila["mensajes"], fila.get("apuntes", 0)
    return (f"*{nombre}:* {euros(fila['dolares'])} · {m} {'mensaje' if m == 1 else 'mensajes'}"
            f" · {a} {'apunte' if a == 1 else 'apuntes'}")


def texto(datos: dict[str, Any], modelo: str, nombres: dict[str, str] | None = None,
          cuando: datetime | None = None) -> str:
    cuando = cuando or datetime.now()
    total = datos["total"]
    tipos = total.get("tipos") or {}
    mensajes = max(1, total["mensajes"])
    #  Los de antes de /stats solo se guardaban por mes: sin día, tipo, persona ni apuntes.
    sin_detalle = total["mensajes"] - sum(tipos.get(t, 0) for t in TIPOS)
    lineas = [
        "📊 *Lo que lleva el bot*",
        "",
        _linea("Hoy", datos["dias"].get(cuando.strftime("%Y-%m-%d"))),
        _linea("Este mes", datos["meses"].get(cuando.strftime("%Y-%m"))),
        _linea("Desde siempre", total),
        "",
        f"Por mensaje, de media: {euros(total['dolares'] / mensajes)}",
        f"Recibidos: {tipos.get('texto', 0)} textos · {tipos.get('voz', 0)} notas de voz · {tipos.get('foto', 0)} fotos",
    ]
    if sin_detalle > 0:
        lineas.append(f"_Y {sin_detalle} de antes de /stats: cuentan en el mes y en el total, pero no se "
                      "guardó ni el día, ni el tipo, ni los apuntes._")
    perfiles = {p: f for p, f in (datos.get("perfiles") or {}).items() if p != "?"}
    if len(perfiles) > 1:
        lineas.append("")
        lineas += [_linea((nombres or {}).get(p, p.capitalize()), f) for p, f in sorted(perfiles.items())]
    revisado = datetime.fromisoformat(precios.REVISADO).strftime("%d/%m/%Y")
    cambio = f"{precios.EUROS_POR_DOLAR}".replace(".", ",")
    lineas += ["", f"_Modelo {modelo}. Precios oficiales de OpenAI del {revisado}, a {cambio} € el dólar._"]
    #  «Desde» es el primer día con datos de verdad, no el que se supuso al convertir.
    primero = min(datos["dias"]) if datos.get("dias") else None
    if primero:
        lineas.append(f"_Con detalle desde el {datetime.fromisoformat(primero).strftime('%d/%m/%Y')}._")
    return "\n".join(lineas)
