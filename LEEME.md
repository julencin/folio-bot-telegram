# Bot de Telegram de Folio

Le cuentas al bot lo que gastas, como te salga, y el apunte llega a Folio. Puede ser:

- **un texto**: «15 € en un bar»;
- **una nota de voz** 🎙️;
- **la foto del ticket** 📷, que puede llevar texto al lado («a medias», «esto es de la casa»);
- **las cuentas de la semana de una tirada**, en texto o en audio: «el lunes 40 de gasolina, el
  martes 12 en el Mercadona y ayer 60 de cena a medias». Salen **tres apuntes separados**, cada
  uno con su fecha. El bot los enseña juntos y puedes mandar los tres o revisarlos uno a uno.

## La Cartera

Lo mismo sirve para tus inversiones. El bot distingue solo si lo que le cuentas es un gasto o
una operación de la Cartera: compra, venta, dividendo, interés, comisión, traspaso o split.

| Cómo se lo dices | Qué saca |
|---|---|
| «He metido 200 € en Bitcoin a 58.000» | compra · Bitcoin · 200 € · cantidad = 200 / 58.000 |
| «He metido 200 € en Bitcoin a 58.000, comisión 1,50» | igual, y la cantidad sale de 198,50 € |
| «Compré 3 acciones de Apple a 180 dólares en Trade Republic» | compra · 3 × 180 $ · bróker Trade Republic |
| «Me han pagado 12 € de dividendo de Coca-Cola» | dividendo · 12 € |
| **Captura de la orden** (del bróker o del exchange) | lo lee todo: tipo, activo, cantidad, precio, comisión, total, fecha, divisa |
| Nota de voz con cualquiera de las anteriores | igual que el texto |

- **Lo más fiable es la captura** de la pantalla de «orden ejecutada»: trae todo y no hay que
  acordarse de nada.
- Por texto o por voz, **basta con el importe y el precio**. La cantidad la calcula el bot, no
  GPT, para que no haya errores de cuentas. Si das la cantidad y el precio, calcula el importe.
- **La comisión, si la dices.** Si no, cero.
- **Reconoce tus activos.** Folio le pasa la lista de los que ya tienes: nombre, ISIN, divisa y
  bróker, pero no cantidades ni importes. Así «BTC» o «el bitcoin» caen en tu activo «Bitcoin»,
  con su bróker. Si es un activo que no tenías, lo marca como _(nuevo)_ para que lo mires.
- En Telegram sale con su icono (📈 compra, 📉 venta, 💶 dividendo…) y un botón **🏷️ Tipo**
  para cambiarlo si no ha acertado.
- En Folio aparece en la misma bandeja del Historial, con «→ Cartera». Al aceptarlo va al libro
  de la Cartera, y de ahí al Resumen como inversión, igual que si lo metieras a mano. «Revisar»
  abre el formulario de la Cartera ya relleno.

Un mismo mensaje puede traer de las dos cosas: «30 € de gasolina y he metido 100 € en el ETF».

Todo pasa por OpenAI, con tu misma clave. El audio lo transcribe `gpt-4o-mini-transcribe` y la
foto la lee directamente el modelo, que ve imágenes. No interviene nadie más.

```
Tú:    ayer 37 de gasolina con la VISA
Foli:  🟠 −37,00 € · Gasolina
       Transporte › Gasolina · VISA · ayer
       [✅ A Folio] [🏷️ Categoría] [± Signo] [✖ Descartar]
```

Si pulsas ✅, el apunte se guarda en la bandeja de la carpeta de Drive. Folio lo recoge (al
abrirlo, y cada minuto mientras está abierto). Foli te avisa y lo ves arriba del
**Historial**, en «Llegado por Telegram». Ahí lo aceptas, lo revisas o lo descartas. Hasta que
no lo aceptas, no cuenta en nada.

## Cómo funciona

```
Telegram ──► Raspberry (este bot) ──► OpenAI (entiende la frase)
                  │
                  ▼
     Drive: Folio compartido/bandeja/<perfil>/entrantes/*.json
                  ▲                         │
    contexto.json │ (categorías, cuentas,   ▼
      memoria)    └────────── Folio en el ordenador (Historial › Llegado por Telegram)
```

- **El bot y Folio no hablan directamente.** El ordenador puede estar apagado; lo que apuntas
  espera en Drive.
- **El bot conoce tus categorías.** Folio deja en Drive `contexto.json`, con tu árbol de
  categorías, tus cuentas, la cuenta que más usas y una *memoria*. La memoria dice qué concepto
  suele ir en qué categoría, aprendido de tu histórico: sin importes ni fechas. Se actualiza
  solo cuando cambias algo.
- **GPT solo puede elegir tus categorías.** La respuesta usa salida estructurada con un esquema
  cerrado: la categoría, la subcategoría y la cuenta tienen que salir de tu lista. Aun así, el bot
  comprueba que la subcategoría cuelga de la categoría elegida.
- **Qué se manda a OpenAI:** lo que mandas al bot (el texto, la nota de voz o la foto), tus
  categorías y cuentas, y la memoria de conceptos. No se mandan saldos, ni importes del
  histórico, ni nada más.
- **Cada perfil tiene su bandeja.** `FOLIO_USUARIOS` dice qué usuario de Telegram es qué perfil.
  A cualquier otro, el bot solo le dice su id.

## Cuánto cuesta

Precios oficiales de OpenAI a 18/09/2026, en $ por millón de tokens. Están en `precios.py`; si
cambian, se cambian ahí.

Un mensaje lleva unos 3.300 tokens de entrada (instrucciones, categorías y memoria) y unos 80 de
salida. La parte fija es siempre igual, así que a partir del segundo mensaje seguido OpenAI la
cobra como *cacheada*, a una cuarta parte.

| Modelo | Entrada | Cacheada | Salida | Por mensaje | 100 mensajes al mes |
|---|---|---|---|---|---|
| **gpt-4.1-mini** (por defecto) | 0,40 | 0,10 | 1,60 | 0,07–0,13 céntimos | 7–13 céntimos |
| gpt-4.1-nano | 0,10 | 0,025 | 0,40 | ~0,03 céntimos | ~3 céntimos |
| gpt-4o-mini | 0,15 | 0,075 | 0,60 | ~0,05 céntimos | ~5 céntimos |
| gpt-5-mini (razona un poco) | 0,25 | 0,025 | 2,00 | ~0,11 céntimos | ~11 céntimos |

Lo que cuesta de más:

| Si mandas… | Cuánto más | Por qué |
|---|---|---|
| Una nota de voz | ~0,03 céntimos por cada 10 s | `gpt-4o-mini-transcribe`, 0,003 $ el minuto |
| La foto de un ticket | ~0,1 céntimos | la imagen entra como ~2.500 tokens de entrada (detalle alto, para leer la letra pequeña) |
| Varias cuentas de golpe | casi nada | solo crece un poco la salida |

En resumen, **menos de 20 céntimos al mes** apuntando tres o cuatro cosas al día, aunque sean
audios o tickets. Lo que no es un apunte («hola») cuesta lo mismo, porque también pasa por GPT.

`/stats` te dice lo que lleva gastado el bot, en euros: **hoy, este mes y desde siempre**, con
cuántos mensajes ha recibido (textos, notas de voz y fotos), cuántos apuntes ha sacado y cuánto
lleva cada uno de vosotros. Con `FOLIO_MOSTRAR_COSTE=1`, cada respuesta dice lo que ha costado
(«Coste: 0,0017 €»: una fracción de céntimo).

## Instalación en la Raspberry

1. **Crear el bot.** En Telegram, habla con @BotFather, usa `/newbot` y guarda el token.
2. **Traer el código.** Está en este repositorio, en `integraciones/telegram/`. En la Raspberry
   solo hace falta esta carpeta, con su ruta (`integraciones/__init__.py` incluido):
   ```bash
   git clone <tu repo> folio && cd folio
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r integraciones/telegram/requirements.txt
   ```
3. **Llegar a Drive.** Con rclone (`rclone config` y un remoto de Google Drive, por ejemplo
   `gdrive`), elige una de las dos formas:
   - Montado: `rclone mount "gdrive:Folio compartido" ~/drive/Folio --vfs-cache-mode writes --daemon`
     y `FOLIO_BANDEJA=/home/pi/drive/Folio/bandeja`.
   - Sin montar: `FOLIO_RCLONE=gdrive:Folio compartido/bandeja`. El bot usa `rclone cat` y
     `rclone copyto` en cada mensaje.

   La carpeta es la misma que elegiste en Folio, en *Datos y mantenimiento › El otro ordenador*.
   Dentro, Folio crea sola `bandeja/<perfil>/`.
4. **Configurar.** Copia `.env.ejemplo` como `.env`, en la misma carpeta, y rellénalo. Tu id de
   Telegram te lo da el propio bot con `/start` (arráncalo una vez sin ponerlo).
5. **Arrancar.** `python -m integraciones.telegram.bot` desde la raíz del repo. Para que arranque
   solo con la Raspberry, usa `folio-bot.service`:
   ```bash
   sudo cp integraciones/telegram/folio-bot.service /etc/systemd/system/
   sudo systemctl enable --now folio-bot
   journalctl -u folio-bot -f      # ver qué hace
   ```

## Qué hay aquí

| Fichero | Qué hace |
|---|---|
| `bot.py` | Telegram: texto, voz, fotos, botones, `/start`, `/stats` |
| `estadisticas.py` | Lo gastado y recibido por día, mes, total y persona (`coste.json`) |
| `interprete.py` | Lo que mandas → gastos y operaciones de cartera: el prompt, el esquema estricto, la validación y la transcripción |
| `bandeja.py` | Leer `contexto.json` y dejar apuntes en Drive (carpeta o rclone) |
| `precios.py` | Los precios oficiales y el cálculo del coste por mensaje |
| `.env.ejemplo` | La configuración; el `.env` de verdad no se sube a git |

Las pruebas (`pruebas/test_bot_telegram.py`) no usan ni Telegram ni OpenAI. Hacen el recorrido
entero con un Folio de pruebas: el bot lee el contexto, deja un apunte, Folio lo recoge y, al
aceptarlo, aparece en el Historial.

## Para más adelante

- **`/mes`**: cuánto llevas este mes contra el presupuesto. Hace falta que Folio publique un
  resumen en la bandeja, igual que publica el contexto.
- **Aceptar solo** lo que llega con confianza alta, sin pasar por el Historial.
