"""
El bot de Telegram de Folio: le cuentas lo que gastas y el apunte llega a Folio.

Los mensajes van en HTML, no en Markdown, y todo lo que viene de fuera (un concepto, el
nombre de un activo, lo que dictaste) pasa por `escape`. Con Markdown, un concepto del
banco como «PAGO_TARJETA *4321» dejaba el formato a medias y Telegram rechazaba el
mensaje entero: no llegaba nada y parecía que el bot no contestaba.

    Tú (Telegram)      «ayer 37 de gasolina», una nota de voz o la foto del ticket
    Bot + OpenAI       −37,00 € · Gasolina · Transporte › Gasolina · IMAGIN · ayer
                       [✅ A Folio] [🏷️ Categoría] [± Signo] [✖]
    Al confirmar       deja el apunte en la bandeja de Drive
    Folio              lo recoge y te lo enseña en el Historial para aceptarlo

Si cuentas varias cosas de una vez («las cuentas de la semana»), salen varios apuntes:
el bot los enseña juntos y puedes mandarlos todos o revisarlos uno a uno.

Lo mismo con la Cartera: «he metido 200 € en Bitcoin a 58.000» o la captura de una orden
del bróker salen como operación de cartera (📈 compra, 📉 venta, 💶 dividendo…), y en
Folio van a la Cartera en vez de al Historial.

Solo contesta a los usuarios de FOLIO_USUARIOS (id de Telegram → perfil de Folio). A
cualquier otro le dice su id y nada más, para que puedas añadirlo si es de casa.

Arranque (en la Raspberry):  python -m integraciones.telegram.bot
Configuración: variables de entorno o un `.env` al lado de este fichero (ver .env.ejemplo).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from html import escape
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .bandeja import Bandeja
from .esperando import Esperando
from . import duplicados, estadisticas, interprete, precios

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("folio.bot")


# ── Configuración ──────────────────────────────────────────────────────────


def _cargar_env(ruta: Path) -> None:
    """Un `.env` sencillo (CLAVE=valor por línea). Lo que ya esté en el entorno manda."""
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def usuarios(texto: str) -> dict[int, str]:
    """«111=julen, 222=irati» → {111: "julen", 222: "irati"}."""
    salida: dict[int, str] = {}
    for trozo in texto.replace(";", ",").split(","):
        if "=" in trozo:
            ident, perfil = trozo.split("=", 1)
            if ident.strip().lstrip("-").isdigit() and perfil.strip():
                salida[int(ident.strip())] = perfil.strip().lower()
    return salida


# ── Cómo se enseña un apunte ───────────────────────────────────────────────


def _euros(valor: float) -> str:
    entero, dec = f"{abs(valor):,.2f}".split(".")
    return ("+" if valor > 0 else "−") + entero.replace(",", ".") + "," + dec + " €"


def _cuando(fecha_iso: str, hoy: date) -> str:
    fecha = date.fromisoformat(fecha_iso)
    if fecha == hoy:
        return "hoy"
    if fecha == hoy - timedelta(days=1):
        return "ayer"
    if hoy - fecha < timedelta(days=7):
        return f"el {interprete.DIAS[fecha.weekday()]}"
    return fecha.strftime("%d/%m/%Y")


ICONOS_CARTERA = {"compra": "📈", "venta": "📉", "dividendo": "💶", "interés": "💶", "comisión": "🧾",
                  "traspaso": "🔁", "split": "✂️"}


def _numero(valor: float, decimales: int = 8) -> str:
    """3 → «3»; 0.00344828 → «0,00344828»; 58000 → «58.000»."""
    texto = f"{valor:,.{decimales}f}".rstrip("0").rstrip(".")
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _dinero(valor: float, divisa: str = "EUR") -> str:
    simbolo = {"EUR": "€", "USD": "$", "GBP": "£"}.get(divisa.upper(), divisa.upper())
    entero, dec = f"{abs(valor):,.2f}".split(".")
    return f"{entero.replace(',', '.')},{dec} {simbolo}"


def resumen_cartera(op: dict[str, Any], hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    signo = interprete.SIGNO_CARTERA.get(op["tipo"], 0)
    cabeza = (f"{ICONOS_CARTERA.get(op['tipo'], '📈')} <b>{escape(op['tipo'].capitalize())}</b> · {escape(op['activo'])}"
              + (" <i>(nuevo)</i>" if op.get("nuevo") else ""))
    partes = []
    if op["importe"]:
        partes.append(("−" if signo < 0 else "+" if signo > 0 else "") + _dinero(op["importe"], "EUR" if op["divisa"] == "EUR" else op["divisa"]))
    if op["cantidad"]:
        partes.append(f"{_numero(op['cantidad'])} u." + (f" a {_dinero(op['precio'], op['divisa'])}" if op["precio"] else ""))
    if op["comision"]:
        partes.append(f"comisión {_dinero(op['comision'], op['divisa'])}")
    partes += [op["cuenta"] or "sin bróker", _cuando(op["fecha"], hoy)]
    aviso = f"\n⚠️ <i>Ya tienes una igual: {escape(op['repetido'])}</i>" if op.get("repetido") else ""
    return f"{cabeza}\n" + " · ".join(escape(p) for p in partes) + "\n<i>A la Cartera</i>" + aviso


def resumen(apunte: dict[str, Any], hoy: date | None = None) -> str:
    if apunte.get("clase") == "cartera":
        return resumen_cartera(apunte, hoy)
    hoy = hoy or date.today()
    ruta = " › ".join(x for x in (apunte["categoria"], apunte["categoria2"], apunte["categoria3"]) if x)
    partes = [ruta or "sin categoría", apunte["cuenta"] or "sin cuenta", _cuando(apunte["fecha"], hoy)]
    if apunte.get("compartido"):
        partes.append("a medias")
    aviso = f"\n⚠️ <i>Ya tienes uno igual: {escape(apunte['repetido'])}</i>" if apunte.get("repetido") else ""
    return (f"{'🟢' if apunte['importe'] > 0 else '🟠'} <b>{_euros(apunte['importe'])}</b> · {escape(apunte['concepto'])}\n"
            + " · ".join(escape(p) for p in partes) + aviso)


def resumen_varios(apuntes: list[dict[str, Any]], hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    lineas = [f"<b>{len(apuntes)} apuntes:</b>"]
    for n, a in enumerate(apuntes, 1):
        if a.get("clase") == "cartera":
            signo = interprete.SIGNO_CARTERA.get(a["tipo"], 0)
            dinero = (("−" if signo < 0 else "+" if signo > 0 else "") + _dinero(a["importe"], a["divisa"])) if a["importe"] else ""
            lineas.append(f"{n}. {ICONOS_CARTERA.get(a['tipo'], '📈')} {escape(a['tipo'])} · {escape(a['activo'])}"
                          + (f" · {dinero}" if dinero else "") + f" · {_cuando(a['fecha'], hoy)}"
                          + (" ⚠️" if a.get("repetido") else ""))
            continue
        ruta = " › ".join(x for x in (a["categoria"], a["categoria2"]) if x) or "sin categoría"
        lineas.append(f"{n}. {_euros(a['importe'])} · {escape(a['concepto'])} · {escape(ruta)} · {_cuando(a['fecha'], hoy)}"
                      + (" · a medias" if a.get("compartido") else "")
                      + (" ⚠️" if a.get("repetido") else ""))
    repes = [(n, a) for n, a in enumerate(apuntes, 1) if a.get("repetido")]
    if repes:
        lineas.append("")
        lineas += [f"⚠️ <i>El {n} ya lo tienes: {escape(a['repetido'])}</i>" for n, a in repes]
    gastos = [a for a in apuntes if a.get("clase") != "cartera"]
    if len(gastos) > 1:
        lineas.append(f"\nGastos e ingresos: <b>{_euros(sum(a['importe'] for a in gastos))}</b>")
    return "\n".join(lineas)


AYUDA = """<b>Lo que sé hacer</b>

Mándame lo que gastas o lo que te entra, como te salga:
· «15 € en un bar» · «ayer 37 de gasolina con la VISA»
· varias cosas de una tirada: «el lunes 40 de gasolina, el martes 12 en el Mercadona»
· una 🎙️ nota de voz
· la 📷 foto de un ticket, o una captura de la lista de movimientos del banco (una por fila)
· tu cartera: «he metido 200 € en Bitcoin a 58.000», o la captura de la orden del bróker

Te enseño lo que he entendido y, con ✅, va a la bandeja de Folio. Lo aceptas allí.
¿Algo mal? Dímelo justo después, sin repetirlo todo: «ponle de concepto brocas», «era con la
VISA», «es a medias», «fue ayer». Cambio el apunte de arriba.

<b>Comandos</b>
/movimientos — los últimos gastos apuntados en Folio y por dónde ibas
/stats — lo que llevo gastado en OpenAI: hoy, este mes y siempre
/recargar — releer tus categorías y cuentas ahora mismo (si las acabas de cambiar)
/help — esto"""


def texto_ultimos(datos: dict[str, Any], hoy: date | None = None) -> str:
    """
    Los últimos movimientos apuntados en Folio: para saber por dónde ibas.

    Un día por bloque, con su total, y cada línea con su icono: 🟠 lo que sale, 🟢 lo que
    entra, 💼 lo de la Cartera y 🤝 lo que va a medias. La categoría, en gris debajo del
    concepto, que es lo que menos se mira.
    """
    hoy = hoy or date.today()
    movimientos = datos.get("movimientos") or []
    if not movimientos:
        return "En Folio no hay ningún movimiento todavía."
    ultimo = date.fromisoformat(movimientos[0]["fecha"])
    dias = (hoy - ultimo).days
    cuando = "hoy" if dias <= 0 else "ayer" if dias == 1 else f"hace {dias} días"
    lineas = [f"🗓️ <b>Lo último que tienes apuntado es del {ultimo.strftime('%d/%m/%Y')}</b> ({cuando})", ""]

    por_dia: dict[str, list[dict]] = {}
    for m in movimientos:
        por_dia.setdefault(m["fecha"], []).append(m)
    for fecha, delDia in por_dia.items():
        dia = date.fromisoformat(fecha)
        cabecera = ("Hoy" if dia == hoy else "Ayer" if dia == hoy - timedelta(days=1)
                    else f"{interprete.DIAS[dia.weekday()].capitalize()} "
                         + dia.strftime("%d/%m" if dia.year == hoy.year else "%d/%m/%Y"))
        total = sum(m["importe"] for m in delDia)
        lineas.append(f"<b>{cabecera}</b>  <i>{_euros(total)}</i>")
        for m in delDia:
            cartera = m.get("clase") == "cartera"
            icono = "💼" if cartera else "🟢" if m["importe"] > 0 else "🟠"
            donde = ("Cartera" if cartera
                     else " › ".join(x for x in (m.get("categoria"), m.get("categoria2")) if x) or "sin categoría")
            lineas.append(f"{icono} <b>{_euros(m['importe'])}</b> · {escape(m['concepto'] or 'sin concepto')}"
                          + (" 🤝" if m.get("compartido") else ""))
            lineas.append(f"      <i>{escape(donde)}</i>")
        lineas.append("")

    gastos = [m for m in movimientos if m.get("clase") != "cartera"]
    inversion = len(movimientos) - len(gastos)
    pie = f"Son los {len(movimientos)} últimos: {len(gastos)} del día a día"
    pie += f" y {inversion} de la Cartera." if inversion else "."
    lineas.append(pie)
    if datos.get("esperando"):
        n = datos["esperando"]
        lineas.append(f"📥 <i>Y {n} {'apunte espera' if n == 1 else 'apuntes esperan'} en la bandeja de Folio.</i>")
    return "\n".join(lineas).strip()


# ── Lo que lleva gastado el bot (para /stats) ──────────────────────────────

RUTA_STATS = AQUI / "coste.json"


#  Cuánto tiempo después un mensaje sin importe puede corregir el apunte anterior.
CORREGIR_MINUTOS = 30

#  Las fotos de los tickets esperan aquí, en la Raspberry, hasta que le das a ✅. Pasado
#  esto sin mandarse (el apunte ya ha caducado), la copia de la Raspberry se tira. Lo que
#  ya está en Drive no se toca nunca.
DIAS_FOTO_SIN_MANDAR = 15


def extension(imagen: bytes) -> str:
    """Por los primeros bytes: Telegram manda JPEG, pero una captura como fichero puede ser PNG."""
    if imagen.startswith(b"\x89PNG"):
        return ".png"
    if imagen[:4] == b"RIFF" and imagen[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def limpiar_fotos(carpeta: Path, dias: int = DIAS_FOTO_SIN_MANDAR) -> None:
    limite = time.time() - dias * 86400
    for foto in carpeta.glob("*.*") if carpeta.is_dir() else []:
        try:
            if foto.stat().st_mtime < limite:
                foto.unlink()
        except OSError:
            pass


def _donde(mensaje: Any) -> dict[str, Any]:
    """El chat y el número de un mensaje enviado, para poder editarlo después."""
    return {"chat": getattr(mensaje, "chat_id", None), "id": getattr(mensaje, "message_id", None)}


def _apuntar_coste(dolares: float | None, tipo: str = "texto", perfil: str = "", apuntes: int = 0) -> None:
    datos = estadisticas.leer(RUTA_STATS)
    estadisticas.apuntar(datos, dolares or 0.0, tipo, perfil, apuntes)
    estadisticas.guardar(RUTA_STATS, datos)


# ── El bot ─────────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover - necesita Telegram y OpenAI de verdad
    from openai import OpenAI
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ChatAction, ParseMode
    from telegram.ext import (
        Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
    )

    _cargar_env(AQUI / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ["TELEGRAM_TOKEN"]
    modelo = os.environ.get("FOLIO_MODELO", "gpt-4.1-mini")
    quienes = usuarios(os.environ.get("FOLIO_USUARIOS", ""))
    mostrar_coste = os.environ.get("FOLIO_MOSTRAR_COSTE", "0") == "1"
    bandeja = Bandeja(os.environ.get("FOLIO_BANDEJA"), os.environ.get("FOLIO_RCLONE"))
    cliente = OpenAI()
    #  Lo que espera a que pulses un botón. En disco: sobrevive a los reinicios del bot y
    #  dura 14 días (ver esperando.py).
    esperando = Esperando(AQUI / "esperando.json")   # un apunte suelto
    grupos = Esperando(AQUI / "grupos.json")         # varios de un mismo mensaje
    #  Lo ya mandado a Folio: si vuelves a pulsar ✅, se contesta «ya está» y no se repite.
    enviados = Esperando(AQUI / "enviados.json")
    #  Las fotos de los tickets hasta que se mandan (ver DIAS_FOTO_SIN_MANDAR).
    fotos = AQUI / "tickets"
    limpiar_fotos(fotos)
    #  Lo que se está mandando ahora mismo: un segundo toque no lo manda dos veces.
    en_curso: set[str] = set()

    def botones(ident: str) -> InlineKeyboardMarkup:
        pendiente = esperando.get(ident) or {}
        if (pendiente.get("apunte") or {}).get("clase") == "cartera":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ A Folio", callback_data=f"ok:{ident}"),
                 InlineKeyboardButton("🏷️ Tipo", callback_data=f"tipo:{ident}")],
                [InlineKeyboardButton("✖ Descartar", callback_data=f"no:{ident}")],
            ])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ A Folio", callback_data=f"ok:{ident}"),
             InlineKeyboardButton("🏷️ Categoría", callback_data=f"cat:{ident}")],
            [InlineKeyboardButton("± Signo", callback_data=f"signo:{ident}"),
             InlineKeyboardButton("✖ Descartar", callback_data=f"no:{ident}")],
        ])

    def botones_grupo(gid: str, n: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Los {n} a Folio", callback_data=f"gok:{gid}")],
            [InlineKeyboardButton("🔎 Uno a uno", callback_data=f"guno:{gid}"),
             InlineKeyboardButton("✖ Descartar", callback_data=f"gno:{gid}")],
        ])

    def permitido(update: Update) -> str | None:
        usuario = update.effective_user
        return quienes.get(usuario.id) if usuario is not None else None

    async def empezar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        usuario = update.effective_user
        if permitido(update) is None:
            await update.message.reply_text(
                f"Hola. Este bot es de una casa concreta. Tu id de Telegram es {usuario.id if usuario else '?'}: "
                "si es la tuya, añádelo a FOLIO_USUARIOS."
            )
            return
        await update.message.reply_text(
            "¡Hola! Soy Foli 🟧. Cuéntame lo que gastas o lo que te entra, como te salga:\n"
            "· «15 € en un bar»\n· «ayer 37 de gasolina con la VISA»\n"
            "· las cuentas de la semana de una tirada: «el lunes 40 de gasolina, el martes 12 en el Mercadona…»\n"
            "· una 🎙️ nota de voz\n· la 📷 foto del ticket\n"
            "· tu cartera: «he metido 200 € en Bitcoin a 58.000», o la captura de la orden del bróker\n\n"
            "Te enseño cómo lo he entendido y, si le das a ✅, llega a la bandeja de Folio.\n\n"
            "/help te cuenta todo lo que sé hacer."
        )

    async def ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if permitido(update) is None:
            return
        await update.message.reply_text(AYUDA, parse_mode=ParseMode.HTML)

    async def movimientos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Los últimos gastos apuntados en Folio, para no repetir ni dejarte días sueltos."""
        perfil = permitido(update)
        if perfil is None:
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            datos = await asyncio.to_thread(bandeja.ultimos, perfil)
        except FileNotFoundError as exc:
            await update.message.reply_text(str(exc))
            return
        except Exception:
            log.exception("No se han podido leer los últimos movimientos")
            await update.message.reply_text("No he podido mirarlo en Drive ahora mismo. Prueba en un rato.")
            return
        await update.message.reply_text(texto_ultimos(datos), parse_mode=ParseMode.HTML)

    async def recargar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Releer categorías y cuentas sin esperar a que caduque la copia (5 min)."""
        perfil = permitido(update)
        if perfil is None:
            return
        bandeja.olvidar(perfil)
        try:
            contexto = await asyncio.to_thread(bandeja.contexto, perfil)
        except FileNotFoundError as exc:
            await update.message.reply_text(str(exc))
            return
        cartera = (contexto.get("cartera") or {}).get("activos") or []
        await update.message.reply_text(
            f"Al día: {len(contexto.get('categorias') or {})} categorías, "
            f"{len(contexto.get('cuentas') or [])} cuentas y {len(cartera)} activos."
        )

    async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Cuánto lleva gastado el bot y cuántos mensajes ha recibido: hoy, este mes y siempre."""
        if permitido(update) is None:
            return
        await update.message.reply_text(estadisticas.texto(estadisticas.leer(RUTA_STATS), modelo),
                                        parse_mode=ParseMode.HTML)

    async def entender(update: Update, ctx: ContextTypes.DEFAULT_TYPE, frase: str, imagen: bytes | None = None,
                       extra_dolares: float = 0.0, transcripcion: str = "", tipo: str = "texto") -> None:
        """Lo común a texto, voz y foto: GPT, y enseñar lo entendido con sus botones."""
        perfil = permitido(update)
        if perfil is None:
            return
        # Lo último sin confirmar, por si este mensaje solo lo corrige («ponle de concepto…»).
        # Una foto siempre es un ticket nuevo: no corrige nada.
        previo = esperando.reciente(perfil, CORREGIR_MINUTOS * 60) if imagen is None else None
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            contexto = await asyncio.to_thread(bandeja.contexto, perfil)
        except FileNotFoundError as exc:
            await update.message.reply_text(str(exc))
            return
        try:
            resultado = await asyncio.to_thread(interprete.interpretar, cliente, modelo, contexto, frase, None, imagen,
                                                anterior=previo[1]["apunte"] if previo else None)
        except Exception:  # la red, la clave, el modelo…: se dice y ya
            log.exception("OpenAI ha fallado")
            await update.message.reply_text("No he podido entenderlo ahora mismo (OpenAI no responde). Prueba en un rato.")
            return
        # ¿Algo de esto ya lo tienes apuntado? Se avisa, no se impide (es local: no cuesta nada).
        try:
            huellas = (await asyncio.to_thread(bandeja.ultimos, perfil, 120)).get("huellas") or []
        except Exception:
            huellas = []
        duplicados.marcar(resultado.apuntes, huellas)
        if imagen is not None:
            gastos = [a for a in resultado.apuntes if a.get("clase") == "gasto"]
            if gastos:
                # Una foto, un fichero: si de ella salen varios apuntes, todos la llevan.
                nombre = f"{uuid.uuid4().hex[:16]}{extension(imagen)}"
                try:
                    fotos.mkdir(parents=True, exist_ok=True)
                    (fotos / nombre).write_bytes(imagen)
                    for a in gastos:
                        a["ticket"] = nombre
                except OSError:
                    log.warning("No he podido guardar la foto del ticket", exc_info=True)

        dolares = (resultado.coste_dolares or 0.0) + extra_dolares
        _apuntar_coste(dolares, tipo, perfil, len(resultado.apuntes))
        cabeza = f"🎙️ <i>«{escape(transcripcion)}»</i>\n\n" if transcripcion else ""
        pie = ""
        if resultado.duda:
            pie += f"\n\n<i>{escape(resultado.duda)}</i>"
        elif resultado.dudoso:
            pie += "\n\n<i>Alguna categoría o fecha la he supuesto: míralo antes de mandarlo.</i>"
        if mostrar_coste:
            pie += f"\n\n<code>Coste: {estadisticas.euros(dolares)}</code>"

        if not resultado.apuntes:
            await update.message.reply_text(cabeza + escape(resultado.duda or "No he visto ningún gasto."),
                                            parse_mode=ParseMode.HTML)
            return
        if resultado.corrige and previo is not None:
            # El mismo apunte, cambiado: conserva su id (sus botones siguen valiendo) y el
            # mensaje de antes se queda sin botones, para que no haya dos ✅ del mismo gasto.
            ident, guardado = previo
            antes = guardado["apunte"]
            if antes.get("ticket"):
                resultado.apuntes[0]["ticket"] = antes["ticket"]
            nuevo = {**resultado.apuntes[0], "id": ident,
                     "texto": " · ".join(x for x in ((antes.get("texto") or "").strip(), frase.strip()) if x)[:500]}
            esperando[ident] = {"perfil": perfil, "apunte": nuevo}
            viejo = guardado.get("mensaje") or {}
            if viejo.get("chat") is not None and viejo.get("id") is not None:
                try:
                    await ctx.bot.edit_message_text(chat_id=viejo["chat"], message_id=viejo["id"],
                                                    text=resumen(antes) + "\n\n↪️ <i>Corregido más abajo.</i>",
                                                    parse_mode=ParseMode.HTML)
                except Exception:
                    log.info("No se ha podido quitar los botones del mensaje anterior", exc_info=True)
            enviado = await update.message.reply_text(cabeza + "✏️ <b>Corregido</b>\n" + resumen(nuevo) + pie,
                                                      parse_mode=ParseMode.HTML, reply_markup=botones(ident))
            esperando[ident] = {"perfil": perfil, "apunte": nuevo, "mensaje": _donde(enviado)}
            return
        if len(resultado.apuntes) == 1:
            apunte = resultado.apuntes[0]
            esperando[apunte["id"]] = {"perfil": perfil, "apunte": apunte}
            enviado = await update.message.reply_text(cabeza + resumen(apunte) + pie, parse_mode=ParseMode.HTML,
                                                      reply_markup=botones(apunte["id"]))
            # Dónde está el mensaje: si luego llega una corrección, se le quitan los botones.
            esperando[apunte["id"]] = {"perfil": perfil, "apunte": apunte, "mensaje": _donde(enviado)}
            return
        gid = resultado.apuntes[0]["id"]
        grupos[gid] = {"perfil": perfil, "apuntes": resultado.apuntes}
        await update.message.reply_text(cabeza + resumen_varios(resultado.apuntes) + pie, parse_mode=ParseMode.HTML,
                                        reply_markup=botones_grupo(gid, len(resultado.apuntes)))

    async def texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message and update.message.text:
            await entender(update, ctx, update.message.text)

    async def voz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if permitido(update) is None or not update.message:
            return
        nota = update.message.voice or update.message.audio
        if nota is None:
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        fichero = await nota.get_file()
        audio = bytes(await fichero.download_as_bytearray())
        try:
            frase = await asyncio.to_thread(interprete.transcribir, cliente, audio, "nota.ogg")
        except Exception:
            log.exception("La transcripción ha fallado")
            await update.message.reply_text("No he podido escuchar la nota ahora mismo. Prueba otra vez o escríbemelo.")
            return
        if not frase:
            await update.message.reply_text("No he entendido nada en la nota. ¿Me la repites?")
            return
        await entender(update, ctx, frase, extra_dolares=precios.coste_voz(nota.duration or 0), transcripcion=frase,
                       tipo="voz")

    async def foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if permitido(update) is None or not update.message:
            return
        if update.message.photo:
            fichero = await update.message.photo[-1].get_file()  # la de más resolución
        elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
            fichero = await update.message.document.get_file()
        else:
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        imagen = bytes(await fichero.download_as_bytearray())
        await entender(update, ctx, update.message.caption or "", imagen=imagen, tipo="foto")

    def subir(perfil: str, apuntes: list[dict[str, Any]]) -> None:
        """Primero las fotos, luego los apuntes: Folio nunca ve un apunte sin su foto."""
        for nombre in dict.fromkeys(a["ticket"] for a in apuntes if a.get("ticket")):
            ruta = fotos / nombre
            if ruta.is_file():
                bandeja.dejar_ticket(perfil, nombre, ruta.read_bytes())
            else:
                # La copia de la Raspberry ya no está: el apunte va igual, sin foto.
                for a in apuntes:
                    if a.get("ticket") == nombre:
                        a.pop("ticket", None)
        bandeja.dejar_varios(perfil, apuntes)

    async def mandar(consulta: Any, ident: str, perfil: str, apuntes: list[dict[str, Any]], texto: str,
                     volver: InlineKeyboardMarkup) -> None:
        """
        Mandar a Folio, sin hacer esperar: se contesta al toque, se quitan los botones (no
        se puede pulsar dos veces) y se sube a Drive aparte. Al acabar, el mensaje dice cómo ha ido.
        """
        if enviados.get(ident):
            await consulta.answer("✅ Ya está en la bandeja de Folio.")
            return
        if ident in en_curso:
            await consulta.answer("⏳ Ya lo estoy mandando…")
            return
        en_curso.add(ident)
        try:
            await consulta.answer("⏳ Mandando a Folio…")
            await consulta.edit_message_text(texto + "\n\n⏳ <i>Mandando a Folio…</i>", parse_mode=ParseMode.HTML)
            try:
                await asyncio.to_thread(subir, perfil, apuntes)
            except Exception:
                log.exception("No se ha podido escribir en la bandeja")
                await consulta.edit_message_text(texto + "\n\n⚠️ No he podido dejarlo en Drive. ¿Está montado? Vuelve a pulsar.",
                                                 parse_mode=ParseMode.HTML, reply_markup=volver)
                return
            enviados[ident] = {"perfil": perfil}
            grupos.pop(ident, None)
            esperando.pop(ident, None)
            await consulta.edit_message_text(texto + "\n\n✅ En la bandeja de Folio. Lo aceptas en el Historial.",
                                             parse_mode=ParseMode.HTML)
        finally:
            en_curso.discard(ident)

    async def boton(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        consulta = update.callback_query
        if permitido(update) is None:
            await consulta.answer()
            return
        accion, _, resto = (consulta.data or "").partition(":")
        ident, _, extra = resto.partition(":")

        # ── Varios de golpe ──
        if accion in ("gok", "guno", "gno"):
            grupo = grupos.get(ident)
            if grupo is None:
                # Ya mandado, descartado o muy viejo: se dice con un aviso y el mensaje no se toca.
                await consulta.answer("✅ Ya está en la bandeja de Folio." if enviados.get(ident)
                                      else "Esto ya no está pendiente.")
                return
            if accion == "gok":
                await mandar(consulta, ident, grupo["perfil"], grupo["apuntes"], resumen_varios(grupo["apuntes"]),
                             botones_grupo(ident, len(grupo["apuntes"])))
                return
            await consulta.answer()
            if accion == "gno":
                grupos.pop(ident, None)
                await consulta.edit_message_text("Descartados. No llega nada a Folio.")
            else:  # uno a uno: cada apunte en su mensaje, con sus botones
                grupos.pop(ident, None)
                await consulta.edit_message_text(f"Te los paso uno a uno ({len(grupo['apuntes'])}):")
                for a in grupo["apuntes"]:
                    esperando[a["id"]] = {"perfil": grupo["perfil"], "apunte": a}
                    await ctx.bot.send_message(update.effective_chat.id, resumen(a), parse_mode=ParseMode.HTML,
                                               reply_markup=botones(a["id"]))
            return

        # ── Uno suelto ──
        pendiente = esperando.get(ident)
        if pendiente is None:
            await consulta.answer("✅ Ya está en la bandeja de Folio." if enviados.get(ident)
                                  else "Este apunte ya no está pendiente.")
            return
        apunte = pendiente["apunte"]
        if accion == "ok":
            await mandar(consulta, ident, pendiente["perfil"], [apunte], resumen(apunte), botones(ident))
            return
        await consulta.answer()
        arbol = (await asyncio.to_thread(bandeja.contexto, pendiente["perfil"])).get("categorias") or {}

        if accion == "no":
            esperando.pop(ident, None)
            await consulta.edit_message_text("Descartado. No llega nada a Folio.")
        elif accion == "tipo":
            tipos = interprete.TIPOS_CARTERA
            filas = [[InlineKeyboardButton(f"{ICONOS_CARTERA[t]} {t}", callback_data=f"t1:{ident}:{i}")
                      for i, t in list(enumerate(tipos))[j:j + 2]] for j in range(0, len(tipos), 2)]
            filas.append([InlineKeyboardButton("← Volver", callback_data=f"ver:{ident}")])
            await consulta.edit_message_text("¿Qué ha sido?", reply_markup=InlineKeyboardMarkup(filas))
        elif accion == "t1":
            tipos = interprete.TIPOS_CARTERA
            if extra.isdigit() and int(extra) < len(tipos):
                apunte["tipo"] = tipos[int(extra)]
                esperando.guardar()
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.HTML, reply_markup=botones(ident))
        elif accion == "signo":
            apunte["importe"] = -apunte["importe"]
            esperando.guardar()
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.HTML, reply_markup=botones(ident))
        elif accion == "cat":
            # Primero la categoría; si tiene subcategorías, después la subcategoría.
            nombres = sorted(arbol)
            filas = [[InlineKeyboardButton(n, callback_data=f"c1:{ident}:{i}") for i, n in list(enumerate(nombres))[j:j + 2]]
                     for j in range(0, len(nombres), 2)]
            filas.append([InlineKeyboardButton("← Volver", callback_data=f"ver:{ident}")])
            await consulta.edit_message_text("¿En qué categoría va?", reply_markup=InlineKeyboardMarkup(filas))
        elif accion == "c1":
            nombres = sorted(arbol)
            cat = nombres[int(extra)] if extra.isdigit() and int(extra) < len(nombres) else ""
            apunte.update(categoria=cat, categoria2="", categoria3="")
            esperando.guardar()
            subs = sorted(arbol.get(cat) or {})
            if not subs:
                await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.HTML, reply_markup=botones(ident))
                return
            filas = [[InlineKeyboardButton(n, callback_data=f"c2:{ident}:{i}") for i, n in list(enumerate(subs))[j:j + 2]]
                     for j in range(0, len(subs), 2)]
            filas.append([InlineKeyboardButton("Sin subcategoría", callback_data=f"ver:{ident}")])
            await consulta.edit_message_text(f"{cat} › ¿subcategoría?", reply_markup=InlineKeyboardMarkup(filas))
        elif accion == "c2":
            subs = sorted(arbol.get(apunte["categoria"]) or {})
            if extra.isdigit() and int(extra) < len(subs):
                apunte.update(categoria2=subs[int(extra)], categoria3="")
                esperando.guardar()
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.HTML, reply_markup=botones(ident))
        else:  # «ver»: vuelve al resumen
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.HTML, reply_markup=botones(ident))

    async def alFallar(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Si algo revienta, se dice; antes el mensaje se perdía y parecía que el bot no contestaba."""
        log.exception("Algo ha fallado atendiendo un mensaje", exc_info=ctx.error)
        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat:
            await ctx.bot.send_message(chat, "Uf, algo me ha fallado con eso. Mira el registro del bot o inténtalo otra vez.")

    # Varias cosas a la vez: un botón se atiende aunque haya un audio procesándose.
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", empezar))
    app.add_handler(CommandHandler(["help", "ayuda"], ayuda))
    app.add_handler(CommandHandler(["movimientos", "ultimos"], movimientos))
    app.add_handler(CommandHandler("recargar", recargar))
    app.add_handler(CommandHandler(["stats", "coste"], stats))
    app.add_handler(CallbackQueryHandler(boton))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voz))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, foto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))
    app.add_error_handler(alFallar)
    log.info("Folio bot en marcha con %s para %d usuarios", modelo, len(quienes))
    app.run_polling()


if __name__ == "__main__":
    main()
