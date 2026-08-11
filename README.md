# Bot de alertas BTC (H4) por Telegram — gratis

Envía una alerta de **compra** o **venta** a tu Telegram cada vez que se cierra
una vela de 4 horas de BTC/USDT y se produce un cruce de EMA9/EMA21 (filtrado
por RSI 14 para evitar señales en zonas de sobrecompra/sobreventa extrema).

Corre gratis en **GitHub Actions**, sin necesidad de tener tu PC prendida ni
pagar hosting.

## Archivos

- `bot_btc_h4.py` — el bot (Python, solo depende de `requests`)
- `requirements.txt`
- `state.json` — guarda la última alerta enviada para no repetirla
- `.github/workflows/alertas_btc.yml` — corre el bot cada 4h automáticamente

## Paso 1 — Crear el bot de Telegram

1. Abrí Telegram y buscá **@BotFather**.
2. Enviale `/newbot` y seguí los pasos (nombre y username del bot).
3. Te va a dar un **token** con este formato: `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`. Guardalo.
4. Buscá **@userinfobot**, iniciá el chat y te va a devolver tu **chat_id** (un número).
   - Si querés que el bot te escriba a un canal o grupo en vez de a vos, agregá el bot ahí como admin y usá el chat_id del grupo/canal (podés obtenerlo con @getidsbot).
5. Muy importante: iniciá una conversación con tu bot (mandale cualquier mensaje, ej. `/start`) para habilitar que te pueda escribir.

## Paso 2 — Subir el proyecto a GitHub

1. Creá un repositorio nuevo en GitHub (puede ser privado).
2. Subí estos archivos manteniendo la estructura de carpetas (`.github/workflows/alertas_btc.yml` tiene que quedar en esa ruta exacta).

## Paso 3 — Configurar los secrets

En el repo: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` = el token del Paso 1
- `TELEGRAM_CHAT_ID` = tu chat_id del Paso 1

## Paso 4 — Activar el workflow

1. Andá a la pestaña **Actions** del repo.
2. Si GitHub pregunta, habilitá los workflows.
3. Corré manualmente el workflow "Alertas BTC H4" una vez (botón *Run workflow*) para probar que llega el mensaje a Telegram (revisá los logs si no llega).
4. A partir de ahí corre solo cada 4 horas (00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC), sin que tengas que hacer nada.

## Probarlo en tu propia PC (opcional)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="tu_token"
export TELEGRAM_CHAT_ID="tu_chat_id"
python bot_btc_h4.py          # corre una vez
python bot_btc_h4.py --loop   # corre en loop, revisando cada 5 min
```

## Cómo funciona la señal

- **Compra 🟢**: EMA9 cruza hacia arriba a la EMA21 en la vela H4 recién cerrada, y RSI(14) < 70.
- **Venta 🔴**: EMA9 cruza hacia abajo a la EMA21, y RSI(14) > 30.
- Si no hay cruce fresco, no se envía nada (no satura tu Telegram).
- El estado en `state.json` evita mandar la misma alerta dos veces.

## Notas importantes

- Los datos vienen del endpoint público de Binance (`api.binance.com`), sin necesidad de API key ni cuenta.
- Esto es una herramienta de apoyo técnico, **no es asesoramiento financiero**. El cruce de medias con RSI es una estrategia simple y puede dar falsas señales, especialmente en mercados muy volátiles o laterales. Te recomiendo validarla vos mismo (backtesting) antes de operar en base a ella.
- Si más adelante querés otra estrategia (MACD, Bollinger, soportes/resistencias, múltiples timeframes, etc.) o que la alerta te llegue por WhatsApp/Discord en vez de Telegram, avisame y lo ajusto.
