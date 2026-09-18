"""
De lo que mandas al bot a apuntes de Folio. Todo pasa por OpenAI y nada más:

  · Texto: «15 € en un bar» o las cuentas de la semana de una tirada («el lunes 40 de
    gasolina, el martes 12 en el Mercadona y ayer una cena de 60 a medias»).
  · Nota de voz: se transcribe con `gpt-4o-mini-transcribe` y sigue como el texto.
  · Foto del ticket: GPT la lee directamente (el modelo ve imágenes) y saca el total, el
    sitio y la fecha.

De un mismo mensaje pueden salir **varios apuntes**: cada gasto o ingreso distinto que
cuentes es uno. Un ticket, en cambio, es uno solo (su total), aunque tenga muchas líneas.

Se pide a GPT una respuesta con **salida estructurada**: un JSON que tiene que cumplir un
esquema cerrado, en el que la categoría, la subcategoría y la cuenta solo pueden ser una
de las tuyas (van como `enum`). Así no se puede inventar una categoría que Folio luego
rechace. `validar` comprueba además que la subcategoría cuelga de la categoría elegida.

A OpenAI le llega lo que mandas (texto, audio o foto), tus categorías y cuentas, y la
memoria de conceptos. Nada de importes del histórico ni de saldos.

Para que salga barato, lo fijo (instrucciones, categorías, memoria) va primero y siempre
igual: OpenAI lo cachea y lo cobra a una cuarta parte. Lo que cambia va al final.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from . import precios

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MODELO_VOZ = "gpt-4o-mini-transcribe"

INSTRUCCIONES = """Eres el asistente de Folio, una app de finanzas personales en castellano.
Conviertes lo que la persona te manda por Telegram (texto, una nota de voz ya transcrita o la
foto de un ticket) en movimientos de su cuenta.

Cuántos movimientos:
- Uno por cada gasto o ingreso DISTINTO que cuente. Puede contar varios seguidos, por ejemplo
  las cuentas de la semana: «el lunes 40 de gasolina, el martes 12 en el Mercadona y ayer 60 de
  cena» son TRES. No los juntes, no los repitas y no te inventes ninguno.
- Una foto de un ticket es UN movimiento: el TOTAL pagado, con el nombre del comercio como
  concepto y la fecha del ticket. No separes las líneas del ticket. Si el texto que la acompaña
  dice otra cosa (otra categoría, «a medias»…), manda el texto.
- Si no hay ningún gasto ni ingreso (un saludo, una pregunta, algo sin importe), lista vacía y
  explica en «duda», en una frase amable y corta, qué necesitas.

Cada movimiento:
- importe: siempre positivo, en euros («15,50» → 15.5; «cinco euros» → 5).
- sentido: «sale» si gasta o paga; «entra» si recibe dinero (nómina, bizum recibido, devolución, venta).
- concepto: corto y reconocible, como saldría en el banco. Si dice el sitio, el sitio («Bar Txoko»,
  «Mercadona»); si no, lo genérico («Bar», «Gasolina»). Sin importes ni fechas en el concepto.
- categoria / categoria2 / categoria3: SOLO de su árbol de categorías. categoria2 debe colgar de
  categoria, y categoria3 de categoria2. Si no hay subcategoría que encaje, cadena vacía.
  Usa la memoria: si el concepto se parece a uno de la memoria, usa su clasificación.
- cuenta: la que diga («con la VISA», «en efectivo») si está en su lista; si no dice nada, la habitual.
- fecha: AAAA-MM-DD. Hoy si no dice nada. Entiende «ayer», «anteayer», «el lunes», «el viernes
  pasado», «el 3» usando el calendario de los últimos días que se te da. Nunca una fecha futura.
- compartido: true si dice «a medias», «entre los dos», «lo pagamos juntos» o similar.
- confianza: «alta» si lo tienes claro, «media» si has tenido que suponer la categoría o la
  fecha, «baja» si dudas mucho (un ticket que no se lee bien, por ejemplo).

duda: vacío si todo está claro; si no, una frase corta con lo que no sabes.
"""


@dataclass
class Resultado:
    apuntes: list[dict[str, Any]] = field(default_factory=list)
    duda: str = ""
    #  Alguno se ha tenido que suponer: se avisa para que lo mires.
    dudoso: bool = False
    coste_dolares: float | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    #  Lo que se ha entendido de una nota de voz, para enseñarlo.
    transcripcion: str = ""


# ── El esquema y el mensaje ────────────────────────────────────────────────


def _subcategorias(arbol: dict[str, dict[str, list[str]]]) -> tuple[list[str], list[str]]:
    segundas: set[str] = set()
    terceras: set[str] = set()
    for subs in arbol.values():
        for sub, finales in (subs or {}).items():
            segundas.add(sub)
            terceras.update(finales or [])
    return sorted(segundas), sorted(terceras)


def esquema(contexto: dict[str, Any]) -> dict[str, Any]:
    arbol = contexto.get("categorias") or {}
    segundas, terceras = _subcategorias(arbol)
    cuentas = list(dict.fromkeys(contexto.get("cuentas") or []))
    apunte = {
        "type": "object",
        "additionalProperties": False,
        "required": ["importe", "sentido", "concepto", "categoria", "categoria2", "categoria3",
                     "cuenta", "fecha", "compartido", "confianza"],
        "properties": {
            "importe": {"type": "number"},
            "sentido": {"type": "string", "enum": ["sale", "entra"]},
            "concepto": {"type": "string"},
            "categoria": {"type": "string", "enum": [*sorted(arbol), ""]},
            "categoria2": {"type": "string", "enum": [*segundas, ""]},
            "categoria3": {"type": "string", "enum": [*terceras, ""]},
            "cuenta": {"type": "string", "enum": [*cuentas, ""]},
            "fecha": {"type": "string"},
            "compartido": {"type": "boolean"},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["apuntes", "duda"],
        "properties": {"apuntes": {"type": "array", "items": apunte}, "duda": {"type": "string"}},
    }


def _arbol_en_texto(arbol: dict[str, dict[str, list[str]]]) -> str:
    lineas = []
    for cat in sorted(arbol):
        subs = arbol[cat] or {}
        if not subs:
            lineas.append(f"- {cat}")
            continue
        partes = [f"{sub} ({', '.join(finales)})" if finales else sub for sub, finales in sorted(subs.items())]
        lineas.append(f"- {cat}: {'; '.join(partes)}")
    return "\n".join(lineas)


def _memoria_en_texto(memoria: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {m['concepto']} → {' › '.join(x for x in (m['categoria'], m.get('categoria2'), m.get('categoria3')) if x)}"
        for m in memoria
    )


def calendario(hoy: date, dias: int = 10) -> str:
    """Los últimos días con su nombre: así «el martes» tiene una fecha sin cuentas raras."""
    return ", ".join(
        f"{DIAS[d.weekday()]} {d.isoformat()}" for d in (hoy - timedelta(days=i) for i in range(dias))
    )


def mensajes(contexto: dict[str, Any], frase: str, hoy: date, imagen: bytes | None = None,
             tipo_imagen: str = "image/jpeg") -> list[dict[str, Any]]:
    fijo = (
        f"{INSTRUCCIONES}\n"
        f"Árbol de categorías (categoría: subcategorías (subcategorías 2)):\n{_arbol_en_texto(contexto.get('categorias') or {})}\n\n"
        f"Cuentas: {', '.join(contexto.get('cuentas') or []) or '(ninguna)'}\n"
        f"Cuenta habitual: {contexto.get('cuenta_habitual') or '(ninguna)'}\n\n"
        f"Memoria (concepto → dónde suele ir):\n{_memoria_en_texto(contexto.get('memoria') or [])}\n"
    )
    variable = (
        f"Hoy es {DIAS[hoy.weekday()]} {hoy.isoformat()}.\n"
        f"Últimos días: {calendario(hoy)}.\n"
        + (f"Te manda la foto de un ticket. Texto que la acompaña: {frase.strip() or '(ninguno)'}"
           if imagen is not None else f"Mensaje: {frase.strip()}")
    )
    if imagen is None:
        contenido: Any = variable
    else:
        datos = base64.b64encode(imagen).decode("ascii")
        contenido = [
            {"type": "text", "text": variable},
            {"type": "image_url", "image_url": {"url": f"data:{tipo_imagen};base64,{datos}", "detail": "high"}},
        ]
    return [{"role": "system", "content": fijo}, {"role": "user", "content": contenido}]


# ── Lo que vuelve ──────────────────────────────────────────────────────────


def _uno(bruto: dict[str, Any], contexto: dict[str, Any], hoy: date, texto: str, origen: str) -> dict[str, Any] | None:
    try:
        importe = round(abs(float(bruto.get("importe") or 0)), 2)
    except (TypeError, ValueError):
        return None
    if not importe:
        return None

    arbol = contexto.get("categorias") or {}
    cat = str(bruto.get("categoria") or "")
    sub = str(bruto.get("categoria2") or "")
    sub2 = str(bruto.get("categoria3") or "")
    confianza = str(bruto.get("confianza") or "media")
    if cat not in arbol:
        cat, sub, sub2 = "", "", ""
        confianza = "baja"
    if sub and sub not in (arbol.get(cat) or {}):
        sub, sub2 = "", ""
    if sub2 and sub2 not in ((arbol.get(cat) or {}).get(sub) or []):
        sub2 = ""

    cuentas = contexto.get("cuentas") or []
    cuenta = str(bruto.get("cuenta") or "")
    if cuenta not in cuentas:
        cuenta = contexto.get("cuenta_habitual") or ""

    try:
        fecha = date.fromisoformat(str(bruto.get("fecha") or ""))
    except ValueError:
        fecha = hoy
    if fecha > hoy or fecha < hoy - timedelta(days=400):
        fecha = hoy

    signo = 1 if bruto.get("sentido") == "entra" else -1
    return {
        "id": uuid.uuid4().hex[:12],
        "fecha": fecha.isoformat(),
        "concepto": str(bruto.get("concepto") or "").strip()[:120] or (sub or cat or "Sin concepto"),
        "importe": signo * importe,
        "categoria": cat,
        "categoria2": sub,
        "categoria3": sub2,
        "cuenta": cuenta,
        "compartido": bool(bruto.get("compartido")),
        "nota": "",
        "texto": texto.strip()[:500],
        "origen": origen,
        "confianza": confianza,
        "creado": datetime.now().isoformat(timespec="seconds"),
    }


def validar(bruto: dict[str, Any], contexto: dict[str, Any], hoy: date, texto: str,
            origen: str = "telegram") -> Resultado:
    """De la respuesta de GPT a apuntes que Folio va a aceptar. Corrige lo que no cuadra."""
    apuntes = [a for a in (_uno(b, contexto, hoy, texto, origen) for b in bruto.get("apuntes") or []) if a]
    duda = str(bruto.get("duda") or "")
    if not apuntes and not duda:
        duda = "No veo ningún gasto ni ingreso ahí. ¿Cuánto ha sido y en qué?"
    return Resultado(apuntes=apuntes, duda=duda, dudoso=any(a["confianza"] != "alta" for a in apuntes))


def _extra_de_modelo(modelo: str) -> dict[str, Any]:
    if modelo.startswith(("gpt-5", "o")):
        # Los modelos que razonan no aceptan temperatura; y para esto basta con pensar poco.
        return {"reasoning_effort": "minimal" if modelo.startswith("gpt-5") and "." not in modelo else "low"}
    return {"temperature": 0}


def interpretar(cliente: Any, modelo: str, contexto: dict[str, Any], frase: str, hoy: date | None = None,
                imagen: bytes | None = None, tipo_imagen: str = "image/jpeg", origen: str = "telegram") -> Resultado:
    """
    Llama a OpenAI. `cliente` es un `openai.OpenAI()`; se pasa desde fuera para poder
    probar todo lo demás sin red.
    """
    hoy = hoy or date.today()
    respuesta = cliente.chat.completions.create(
        model=modelo,
        messages=mensajes(contexto, frase, hoy, imagen, tipo_imagen),
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "apuntes_folio", "strict": True, "schema": esquema(contexto)},
        },
        **_extra_de_modelo(modelo),
    )
    eleccion = respuesta.choices[0].message
    if getattr(eleccion, "refusal", None):
        return Resultado(duda="Eso no lo puedo apuntar.")
    texto = frase if imagen is None else (f"📷 {frase}".strip() if frase else "📷 ticket")
    resultado = validar(json.loads(eleccion.content or "{}"), contexto, hoy, texto, origen)
    uso = getattr(respuesta, "usage", None)
    if uso is not None:
        detalles = getattr(uso, "prompt_tokens_details", None)
        cacheada = int(getattr(detalles, "cached_tokens", 0) or 0) if detalles else 0
        resultado.tokens = {"entrada": uso.prompt_tokens, "cacheada": cacheada, "salida": uso.completion_tokens}
        resultado.coste_dolares = precios.coste_dolares(respuesta.model or modelo, uso.prompt_tokens, cacheada,
                                                        uso.completion_tokens)
    return resultado


def transcribir(cliente: Any, audio: bytes, nombre: str = "nota.ogg") -> str:
    """Una nota de voz a texto, con el modelo de transcripción de OpenAI y en castellano."""
    respuesta = cliente.audio.transcriptions.create(model=MODELO_VOZ, file=(nombre, audio), language="es")
    return str(getattr(respuesta, "text", "") or "").strip()
