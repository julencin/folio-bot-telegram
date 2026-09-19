"""
El bot de Telegram de Folio: le cuentas lo que gastas y el apunte llega a Folio.

    Tú (Telegram)      «ayer 37 de gasolina», una nota de voz o la foto del ticket
    Bot + OpenAI       −37,00 € · Gasolina · Transporte › Gasolina · IMAGIN · ayer
                       [✅ A Folio] [🏷️ Categoría] [± Signo] [✖]
    Al confirmar       deja el apunte en la bandeja de Drive
    Folio              lo recoge y te lo enseña en el Historial para aceptarlo

Si cuentas varias cosas de una vez («las cuentas de la semana»), salen varios apuntes:
el bot los enseña juntos y puedes mandarlos todos o revisarlos uno a uno.

Solo contesta a los usuarios de FOLIO_USUARIOS (id de Telegram → perfil de Folio). A
cualquier otro le dice su id y nada más, para que puedas añadirlo si es de casa.

Arranque (en la Raspberry):  python -m integraciones.telegram.bot
Configuración: variables de entorno o un `.env` al lado de este fichero (ver .env.ejemplo).
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .bandeja import Bandeja
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


def resumen(apunte: dict[str, Any], hoy: date | None = None) -> str:
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
        ruta = " › ".join(x for x in (a["categoria"], a["categoria2"]) if x) or "sin categoría"
        lineas.append(f"{n}. {_euros(a['importe'])} · {a['concepto']} · {ruta} · {_cuando(a['fecha'], hoy)}"
                      + (" · a medias" if a.get("compartido") else ""))
    lineas.append(f"\nEn total: *{_euros(sum(a['importe'] for a in apuntes))}*")
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
    #  Lo que espera a que pulses un botón, en memoria. Si el bot se reinicia, el botón
    #  dice que ha caducado y vuelves a mandarlo.
    esperando: dict[str, dict[str, Any]] = {}   # un apunte suelto
    grupos: dict[str, dict[str, Any]] = {}      # varios de un mismo mensaje

    def botones(ident: str) -> InlineKeyboardMarkup:
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
            "· una 🎙️ nota de voz\n· la 📷 foto del ticket\n\n"
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
            contexto = bandeja.contexto(perfil)
        except FileNotFoundError as exc:
            await update.message.reply_text(str(exc))
            return
        try:
            resultado = interprete.interpretar(cliente, modelo, contexto, frase, imagen=imagen)
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
            frase = interprete.transcribir(cliente, audio, "nota.ogg")
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

    async def boton(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        consulta = update.callback_query
        await consulta.answer()
        if permitido(update) is None:
            return
        accion, _, resto = (consulta.data or "").partition(":")
        ident, _, extra = resto.partition(":")

        # ── Varios de golpe ──
        if accion in ("gok", "guno", "gno"):
            grupo = grupos.get(ident)
            if grupo is None:
                await consulta.edit_message_text("Esto ha caducado (el bot se ha reiniciado). Mándamelo otra vez.")
                return
            if accion == "gno":
                grupos.pop(ident, None)
                await consulta.edit_message_text("Descartados. No llega nada a Folio.")
            elif accion == "gok":
                try:
                    for a in grupo["apuntes"]:
                        bandeja.dejar(grupo["perfil"], a)
                except Exception:
                    log.exception("No se ha podido escribir en la bandeja")
                    await consulta.edit_message_text("No he podido dejarlos en Drive. ¿Está montado? Vuelve a intentarlo.",
                                                     reply_markup=botones_grupo(ident, len(grupo["apuntes"])))
                    return
                grupos.pop(ident, None)
                await consulta.edit_message_text(resumen_varios(grupo["apuntes"]) + "\n\n✅ En la bandeja de Folio.",
                                                 parse_mode=ParseMode.MARKDOWN)
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
            await consulta.edit_message_text("Este apunte ha caducado (el bot se ha reiniciado). Mándamelo otra vez.")
            return
        apunte = pendiente["apunte"]
        arbol = bandeja.contexto(pendiente["perfil"]).get("categorias") or {}

        if accion == "ok":
            try:
                bandeja.dejar(pendiente["perfil"], apunte)
            except Exception:
                log.exception("No se ha podido escribir en la bandeja")
                await consulta.edit_message_text("No he podido dejarlo en Drive. ¿Está montado? Vuelve a intentarlo.",
                                                 reply_markup=botones(ident))
                return
            esperando.pop(ident, None)
            await consulta.edit_message_text(resumen(apunte) + "\n\n✅ En la bandeja de Folio. Lo aceptas en el Historial.",
                                             parse_mode=ParseMode.MARKDOWN)
        elif accion == "no":
            esperando.pop(ident, None)
            await consulta.edit_message_text("Descartado. No llega nada a Folio.")
        elif accion == "signo":
            apunte["importe"] = -apunte["importe"]
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
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))
        else:  # «ver»: vuelve al resumen
            await consulta.edit_message_text(resumen(apunte), parse_mode=ParseMode.MARKDOWN, reply_markup=botones(ident))

    app = Application.builder().token(token).build()
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
