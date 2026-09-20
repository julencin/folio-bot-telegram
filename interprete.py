"""
De lo que mandas al bot a apuntes de Folio. Todo pasa por OpenAI y nada más:

  · Texto: «15 € en un bar» o las cuentas de la semana de una tirada («el lunes 40 de
    gasolina, el martes 12 en el Mercadona y ayer una cena de 60 a medias»).
  · Nota de voz: se transcribe con `gpt-4o-mini-transcribe` y sigue como el texto.
  · Foto del ticket: GPT la lee directamente (el modelo ve imágenes) y saca el total, el
    sitio y la fecha.

De un mismo mensaje pueden salir **varios apuntes**: cada gasto o ingreso distinto que
cuentes es uno. Un ticket, en cambio, es uno solo (su total), aunque tenga muchas líneas.

Y hay dos clases de apunte: los **gastos** (van al Historial) y las **operaciones de
cartera** (compras, ventas, dividendos… de la Cartera). «He metido 200 € en Bitcoin a
58.000» o la captura de una orden del bróker son de cartera; GPT decide cuál es cuál y un
mismo mensaje puede traer de las dos. Si le das el importe y el precio, la cantidad se
calcula aquí (no se le pide a GPT que divida).

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
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from . import precios

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def cuenta_a_medias() -> str:
    """
    La cuenta de lo que se paga a medias: siempre la misma (la común de la casa). Se
    cambia con FOLIO_CUENTA_A_MEDIAS en el .env; vacío, no se fuerza ninguna.
    """
    return os.environ.get("FOLIO_CUENTA_A_MEDIAS", "Revolut").strip()
MODELO_VOZ = "gpt-4o-mini-transcribe"

INSTRUCCIONES = """Eres el asistente de Folio, una app de finanzas personales en castellano.
Conviertes lo que la persona te manda por Telegram (texto, una nota de voz ya transcrita, la
foto de un ticket o una captura de pantalla) en movimientos de su cuenta.

Cuántos movimientos:
- Uno por cada gasto o ingreso DISTINTO que cuente. Puede contar varios seguidos, por ejemplo
  las cuentas de la semana: «el lunes 40 de gasolina, el martes 12 en el Mercadona y ayer 60 de
  cena» son TRES. No los juntes, no los repitas y no te inventes ninguno.
- **El ticket de una compra es UN movimiento**: el TOTAL pagado, con el nombre del comercio como
  concepto y la fecha del ticket. No separes en varios las líneas de un mismo ticket.
- **Una captura de una LISTA de movimientos es UNO POR CADA LÍNEA.** Es lo que se ve en la app del
  banco, en una app de gastos o en el extracto: varias filas, cada una con su fecha, su sitio y su
  importe, a veces agrupadas por días («17 sept», «13 sept»…). Haz un movimiento por cada fila que
  se lea entera, con SU fecha (la del grupo al que pertenece) y SU importe. Aunque sean de días o
  de meses distintos, van todas: no te quedes solo con la primera ni con las de un día.
  Si una fila está cortada por el borde y no se lee el importe, sáltala y dilo en «duda».
- Si el texto que acompaña a la foto dice otra cosa (otra categoría, «a medias»…), manda el texto.
- Si no hay ningún gasto ni ingreso (un saludo, una pregunta, algo sin importe), listas vacías y
  explica en «duda», en una frase amable y corta, qué necesitas.

Inversiones: van en «operaciones», NUNCA en «apuntes».
- Si compra, vende, cobra un dividendo o intereses, paga una comisión suelta, traspasa o hay un
  split de un activo (acciones, ETF, fondos, criptomonedas…), es una operación de su Cartera,
  no un gasto. Ejemplos: «he metido 200 € en Bitcoin», «compré 3 acciones de Apple a 180 $»,
  «vendí el ETF», «me han pagado 12 € de dividendo de Coca-Cola», «el broker me cobró 2 € de custodia».
- Una captura de pantalla de un bróker o de un exchange: lee la operación entera (tipo, activo,
  cantidad, precio, comisión, total, divisa, fecha). Si la captura trae varias operaciones, una
  por cada una.
- tipo: compra, venta, dividendo, interés, comisión, traspaso o split.
- activo: si es uno de su lista de activos, escríbelo EXACTAMENTE como aparece en ella (aunque lo
  diga de otra forma: «BTC», «el bitcoin»). Si es nuevo, su nombre normal.
- isin: el de la lista si es uno suyo, o el que se vea en la captura; si no, vacío.
- importe: el dinero TOTAL que salió o entró de la cuenta; en una compra, con la comisión incluida.
  0 si no se sabe.
- cantidad: unidades o participaciones. 0 si no lo dice (se calcula con el importe y el precio).
- precio: por unidad, en la divisa del activo. 0 si no lo dice.
- comision: 0 si no hay o no lo dice.
- divisa: la del activo (la de la lista si es uno suyo); EUR si no se sabe.
- cuenta: el bróker o exchange; el de la lista si es uno de ellos; si no lo dice, el del activo
  en su lista, o vacío.
- fecha y confianza: como en los gastos.

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
- compartido: true si dice «a medias», «entre los dos», «lo pagamos juntos» o similar. Lo que es
  a medias va siempre a la cuenta común (se pone sola).
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


TIPOS_CARTERA = ["compra", "venta", "dividendo", "interés", "comisión", "traspaso", "split"]
#  Cómo mueve el dinero cada tipo (el mismo criterio que Folio): −1 sale, +1 entra.
SIGNO_CARTERA = {"compra": -1, "venta": 1, "dividendo": 1, "interés": 1, "comisión": -1, "traspaso": 0, "split": 0}


def _cartera(contexto: dict[str, Any]) -> dict[str, Any]:
    return contexto.get("cartera") or {"activos": [], "cuentas": [], "tipos": TIPOS_CARTERA}


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
    tipos = [t for t in _cartera(contexto).get("tipos") or TIPOS_CARTERA if t in SIGNO_CARTERA] or TIPOS_CARTERA
    operacion = {
        "type": "object",
        "additionalProperties": False,
        "required": ["tipo", "activo", "isin", "cantidad", "precio", "comision", "importe", "divisa",
                     "cuenta", "fecha", "confianza"],
        "properties": {
            "tipo": {"type": "string", "enum": tipos},
            "activo": {"type": "string"},
            "isin": {"type": "string"},
            "cantidad": {"type": "number"},
            "precio": {"type": "number"},
            "comision": {"type": "number"},
            "importe": {"type": "number"},
            "divisa": {"type": "string"},
            "cuenta": {"type": "string"},
            "fecha": {"type": "string"},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["apuntes", "operaciones", "duda"],
        "properties": {
            "apuntes": {"type": "array", "items": apunte},
            "operaciones": {"type": "array", "items": operacion},
            "duda": {"type": "string"},
        },
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


def _activos_en_texto(cartera: dict[str, Any]) -> str:
    filas = [
        "- " + " · ".join(x for x in (a.get("activo"), a.get("isin"), a.get("divisa"), a.get("cuenta")) if x)
        for a in cartera.get("activos") or []
    ]
    return "\n".join(filas) or "(todavía ninguno)"


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
        f"Memoria (concepto → dónde suele ir):\n{_memoria_en_texto(contexto.get('memoria') or [])}\n\n"
        f"Activos de su cartera (nombre · ISIN · divisa · bróker):\n{_activos_en_texto(_cartera(contexto))}\n"
        f"Brókers: {', '.join(_cartera(contexto).get('cuentas') or []) or '(ninguno)'}\n"
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


def _fecha(bruto: dict[str, Any], hoy: date) -> str:
    try:
        fecha = date.fromisoformat(str(bruto.get("fecha") or ""))
    except ValueError:
        fecha = hoy
    if fecha > hoy or fecha < hoy - timedelta(days=400):
        fecha = hoy
    return fecha.isoformat()


def _num(valor: Any) -> float:
    try:
        return abs(float(valor or 0))
    except (TypeError, ValueError):
        return 0.0


def _llano(texto: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def _operacion(bruto: dict[str, Any], contexto: dict[str, Any], hoy: date, texto: str, origen: str) -> dict[str, Any] | None:
    """Una operación de cartera, con lo que falte deducido de lo que sí hay."""
    tipo = str(bruto.get("tipo") or "")
    activo = str(bruto.get("activo") or "").strip()[:120]
    if tipo not in SIGNO_CARTERA or not activo:
        return None
    cartera = _cartera(contexto)
    conocido = next((a for a in cartera.get("activos") or []
                     if _llano(a.get("activo", "")) == _llano(activo)
                     or (bruto.get("isin") and _llano(a.get("isin", "")) == _llano(bruto["isin"]))), None)
    if conocido:
        activo = conocido["activo"]
    cantidad, precio = _num(bruto.get("cantidad")), _num(bruto.get("precio"))
    comision, importe = _num(bruto.get("comision")), _num(bruto.get("importe"))
    # Lo que falta, de lo que hay: «200 € a 58.000» es una cantidad; «3 a 180» es un importe.
    if not cantidad and importe and precio and tipo in ("compra", "venta"):
        neto = importe - comision if tipo == "compra" else importe + comision
        cantidad = max(0.0, neto) / precio
    if not importe and cantidad and precio:
        bruto_total = cantidad * precio
        importe = bruto_total + comision if tipo == "compra" else max(0.0, bruto_total - comision) if tipo == "venta" else bruto_total
    if not importe and not cantidad:
        return None
    confianza = str(bruto.get("confianza") or "media")
    if not conocido and confianza == "alta":
        confianza = "media"  # un activo nuevo: que lo mire
    cuentas = cartera.get("cuentas") or []
    cuenta = str(bruto.get("cuenta") or "").strip()
    cuenta = next((c for c in cuentas if _llano(c) == _llano(cuenta)), cuenta) or (conocido or {}).get("cuenta", "")
    return {
        "clase": "cartera",
        "id": uuid.uuid4().hex[:12],
        "fecha": _fecha(bruto, hoy),
        "tipo": tipo,
        "activo": activo,
        "isin": (str(bruto.get("isin") or "").strip() or (conocido or {}).get("isin", "")).upper()[:20],
        "cantidad": round(cantidad, 8),
        "precio": round(precio, 8),
        "comision": round(comision, 2),
        "importe": round(importe, 2),
        "divisa": (str(bruto.get("divisa") or "").strip() or (conocido or {}).get("divisa") or "EUR").upper()[:5],
        "cuenta": cuenta[:80],
        "nota": "",
        "texto": texto.strip()[:500],
        "origen": origen,
        "confianza": confianza,
        "nuevo": conocido is None,
        "creado": datetime.now().isoformat(timespec="seconds"),
    }


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
    # Lo que es a medias sale siempre de la cuenta común, diga lo que diga GPT. Con su
    # nombre tal cual esté en tu lista («REVOLUT», «Revolut»…), o como está puesto si no hay lista.
    comun = cuenta_a_medias()
    if bruto.get("compartido") and comun:
        cuenta = next((c for c in cuentas if c.strip().lower() == comun.lower()), comun if not cuentas else cuenta)

    try:
        fecha = date.fromisoformat(str(bruto.get("fecha") or ""))
    except ValueError:
        fecha = hoy
    if fecha > hoy or fecha < hoy - timedelta(days=400):
        fecha = hoy

    signo = 1 if bruto.get("sentido") == "entra" else -1
    return {
        "clase": "gasto",
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
    apuntes += [o for o in (_operacion(b, contexto, hoy, texto, origen) for b in bruto.get("operaciones") or []) if o]
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
    texto = frase if imagen is None else (f"📷 {frase}".strip() if frase else "📷 foto")
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
