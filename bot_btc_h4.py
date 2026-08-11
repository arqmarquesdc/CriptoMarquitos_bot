"""
Bot de alertas de compra/venta de Bitcoin (BTCUSDT) en timeframe H4.
Estrategia: cruce de EMA9/EMA21 filtrado por RSI(14).
Envía las alertas por Telegram. Diseñado para correr gratis y en tiempo real
vía GitHub Actions (cron cada 4 horas), pero también funciona en loop local.

Requisitos:
    pip install requests

Variables de entorno necesarias:
    TELEGRAM_BOT_TOKEN  -> token del bot (te lo da @BotFather)
    TELEGRAM_CHAT_ID    -> tu chat_id (te lo da @userinfobot)

Uso:
    python bot_btc_h4.py            # corre una vez y sale (ideal para cron/GitHub Actions)
    python bot_btc_h4.py --loop     # corre en loop continuo, revisando cada 5 minutos
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests

SYMBOL = "BTCUSDT"
INTERVAL = "4h"
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=100):
    """Trae velas históricas de Binance (endpoint público, no requiere API key)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6],
        })
    return candles


def only_closed_candles(candles):
    """Descarta la última vela si todavía está en formación (close_time futuro)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candles and candles[-1]["close_time"] > now_ms:
        return candles[:-1]
    return candles


def calculate_ema(values, period):
    ema = [None] * len(values)
    if len(values) < period:
        return ema
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    for i in range(period, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def calculate_rsi(values, period=RSI_PERIOD):
    rsi = [None] * len(values)
    if len(values) <= period:
        return rsi

    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi[i + 1] = 100 - (100 / (1 + rs))

    return rsi


def detect_signal(candles):
    """
    Devuelve ('BUY'|'SELL'|None, info_dict) evaluando la vela H4 recién cerrada
    contra la anterior, para detectar un cruce fresco de EMA9/EMA21 filtrado por RSI.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    rsi = calculate_rsi(closes, RSI_PERIOD)

    i = len(closes) - 1  # última vela cerrada
    prev = i - 1

    if None in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev], rsi[i]):
        return None, {}

    crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]

    info = {
        "close_time": candles[i]["close_time"],
        "price": closes[i],
        "ema_fast": round(ema_fast[i], 2),
        "ema_slow": round(ema_slow[i], 2),
        "rsi": round(rsi[i], 2),
    }

    if crossed_up and rsi[i] < 70:
        return "BUY", info
    if crossed_down and rsi[i] > 30:
        return "SELL", info
    return None, info


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_alerted_close_time": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[AVISO] Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. No se envía Telegram.")
        print(text)
        return
    url = TELEGRAM_API_URL.format(token=token)
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)
    if resp.status_code != 200:
        print(f"[ERROR] Telegram respondió {resp.status_code}: {resp.text}")
    else:
        print("[OK] Alerta enviada por Telegram.")


def format_message(signal, info):
    emoji = "🟢" if signal == "BUY" else "🔴"
    accion = "COMPRA" if signal == "BUY" else "VENTA"
    ts = datetime.fromtimestamp(info["close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{emoji} *Señal de {accion} - BTC/USDT (H4)*\n"
        f"Precio: ${info['price']:,.2f}\n"
        f"EMA9: {info['ema_fast']:,.2f} | EMA21: {info['ema_slow']:,.2f}\n"
        f"RSI(14): {info['rsi']}\n"
        f"Vela cerrada: {ts}\n\n"
        f"_Estrategia: cruce EMA9/EMA21 filtrado por RSI. No es consejo financiero._"
    )


def run_once():
    candles = only_closed_candles(get_klines())
    signal, info = detect_signal(candles)

    if not signal:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sin señal nueva. "
              f"RSI={info.get('rsi')} EMA9={info.get('ema_fast')} EMA21={info.get('ema_slow')}")
        return

    state = load_state()
    if state.get("last_alerted_close_time") == info["close_time"]:
        print("Señal ya alertada para esta vela, no se repite.")
        return

    message = format_message(signal, info)
    send_telegram_message(message)

    state["last_alerted_close_time"] = info["close_time"]
    state["last_signal"] = signal
    save_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Correr en loop continuo (revisa cada 5 min)")
    args = parser.parse_args()

    if args.loop:
        print("Bot corriendo en loop. Ctrl+C para detener.")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[ERROR] {e}")
            time.sleep(300)
    else:
        run_once()


if __name__ == "__main__":
    main()
