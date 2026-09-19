"""
El bot de Telegram de Folio: le cuentas lo que gastas y el apunte llega a Folio.

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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .bandeja import Bandeja
from .esperando import Esperando
from . import estadisticas, interprete, precios

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
    cabeza = (f"{ICONOS_CARTERA.get(op['tipo'], '📈')} *{op['tipo'].capitalize()}* · {op['activo']}"
              + (" _(nuevo)_" if op.get("nuevo") else ""))
    partes = []
    if op["importe"]:
        partes.append(("−" if signo < 0 else "+" if signo > 0 else "") + _dinero(op["importe"], "EUR" if op["divisa"] == "EUR" else op["divisa"]))
    if op["cantidad"]:
        partes.append(f"{_numero(op['cantidad'])} u." + (f" a {_dinero(op['precio'], op['divisa'])}" if op["precio"] else ""))
    if op["comision"]:
        partes.append(f"comisión {_dinero(op['comision'], op['divisa'])}")
    partes += [op["cuenta"] or "sin bróker", _cuando(op["fecha"], hoy)]
    return f"{cabeza}\n" + " · ".join(partes) + "\n_A la Cartera_"


def resumen(apunte: dict[str, Any], hoy: date | None = None) -> str:
    if apunte.get("clase") == "cartera":
        return resumen_cartera(apunte, hoy)
    hoy = hoy or date.today()
    ruta = " › ".join(x for x in (apunte["categoria"], apunte["categoria2"], apunte["categoria3"]) if x)
    partes = [ruta or "sin categoría", apunte["cuenta"] or "sin cuenta", _cuando(apunte["fecha"], hoy)]
    if apunte.get("compartido"):
        partes.append("a medias")
    return f"{'🟢' if apunte['importe'] > 0 else '🟠'} *{_euros(apunte['importe'])}* · {apunte['concepto']}\n" + " · ".join(partes)


def resumen_varios(apuntes: list[dict[str, Any]], hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    lineas = [f"*{len(apuntes)} apuntes:*"]
    for n, a in enumerate(apuntes, 1):
        if a.get("clase") == "cartera":
            signo = interprete.SIGNO_CARTERA.get(a["tipo"], 0)
            dinero = (("−" if signo < 0 else "+" if signo > 0 else "") + _dinero(a["importe"], a["divisa"])) if a["importe"] else ""
            lineas.append(f"{n}. {ICONOS_CARTERA.get(a['tipo'], '📈')} {a['tipo']} · {a['activo']}"
                          + (f" · {dinero}" if dinero else "") + f" · {_cuando(a['fecha'], hoy)}")
            continue
        ruta = " › ".join(x for x in (a["categoria"], a["categoria2"]) if x) or "sin categoría"
        lineas.append(f"{n}. {_euros(a['importe'])} · {a['concepto']} · {ruta} · {_cuando(a['fecha'], hoy)}"
                      + (" · a medias" if a.get("compartido") else ""))
    gastos = [a for a in apuntes if a.get("clase") != "cartera"]
    if len(gastos) > 1:
        lineas.append(f"\nGastos e ingresos: *{_euros(sum(a['importe'] for a in gastos))}*")
    return "\n".join(lineas)


# ── Lo que lleva gastado el bot (para /stats) ──────────────────────────────

RUTA_STATS = AQUI / "coste.json"


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
            "/stats te dice cuánto llevo gastado en OpenAI."
        )

    async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Cuánto lleva gastado el bot y cuántos mensajes ha recibido: hoy, este mes y siempre."""
        if permitido(update) is None:
            return
        await update.message.reply_text(estadisticas.texto(estadisticas.leer(RUTA_STATS), modelo),
                                        parse_mode=ParseMode.MARKDOWN)

    async def entender(update: Update, ctx: ContextTypes.DEFAULT_TYPE, frase: str, imagen: bytes | None = None,
                       extra_dolares: float = 0.0, transcripcion: str = "", tipo: str = "texto") -> None:
        """Lo común a texto, voz y foto: GPT, y enseñar lo entendido con sus botones."""
        perfil = permitido(update)
        if perfil is None:
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            contexto = await asyncio.to_thread(bandeja.contexto, perfil)
        except FileNotFoundError as exc:
            await update.message.reply_text(str(exc))
            return
        try:
            resultado = await asyncio.to_thread(interprete.interpretar, cliente, modelo, contexto, frase, None, imagen)
        except Exception:  # la red, la clave, el modelo…: se dice y ya
            log.exception("OpenAI ha fallado")
            await update.message.reply_text("No he podido entenderlo ahora mismo (OpenAI no responde). Prueba en un rato.")
            return
        dolares = (resultado.coste_dolares or 0.0) + extra_dolares
        _apuntar_coste(dolares, tipo, perfil, len(resultado.apuntes))
        cabeza = f"🎙️ _«{transcripcion}»_\n\n" if transcripcion else ""
        pie = ""
        if resultado.duda:
            pie += f"\n\n_{resultado.duda}_"
        elif resultado.dudoso:
            pie += "\n\n_Alguna categoría o fecha la he supuesto: míralo antes de mandarlo._"
        if mostrar_coste:
            pie += f"\n\n`Coste: {estadisticas.euros(dolares)}`"

        if not resultado.apuntes:
            await update.message.reply_text(cabeza + (resultado.duda or "No he visto ningún gasto."),
                                            parse_mode=ParseMode.MARKDOWN)
            return
        if len(resultado.apuntes) == 1:
            apunte = resultado.apuntes[0]
            esperando[apunte["id"]] = {"perfil": perfil, "apunte": apunte}
            await update.message.reply_text(cabeza + resumen(apunte) + pie, parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=botones(apunte["id"]))
            return
        gid = resultado.apuntes[0]["id"]
        grupos[gid] = {"perfil": perfil, "apuntes": resultado.apuntes}
        await update.message.reply_text(cabeza + resumen_varios(resultado.apuntes) + pie, parse_mode=ParseMode.MARKDOWN,
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
            await consulta.edit_message_text(texto + "\n\n⏳ _Mandando a Folio…_", parse_mode=ParseMode.MARKDOWN)
            try:
                await asyncio.to_thread(bandeja.dejar_varios, perfil, apuntes)
            except Exception:
                log.exception("No se ha podido escribir en la bandeja")
                await consulta.edit_message_text(texto + "\n\n⚠️ No he podido dejarlo en Drive. ¿Está montado? Vuelve a pulsar.",
                                                 parse_mode=ParseMode.MARKDOWN, reply_markup=volver)
                return
            enviados[ident] = {"perfil": perfil}
            grupos.pop(ident, None)
            esperando.pop(ident, None)
            await consulta.edit_message_text(texto + "\n\n✅ En la bandeja de Folio. Lo aceptas en el Historial.",
                                             parse_mode=ParseMode.MARKDOWN)
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
                    await ctx.bot.send_message(update.effective_chat.id, resumen(a), parse_mode=ParseMode.MARKDOWN,
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
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))
        elif accion == "signo":
            apunte["importe"] = -apunte["importe"]
            esperando.guardar()
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))
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
                await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))
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
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))
        else:  # «ver»: vuelve al resumen
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))

    # Varias cosas a la vez: un botón se atiende aunque haya un audio procesándose.
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "ayuda"], empezar))
    app.add_handler(CommandHandler(["stats", "coste"], stats))
    app.add_handler(CallbackQueryHandler(boton))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voz))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, foto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))
    log.info("Folio bot en marcha con %s para %d usuarios", modelo, len(quienes))
    app.run_polling()


if __name__ == "__main__":
    main()
