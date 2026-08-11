"""
Bot de alertas de compra/venta de Bitcoin (BTC/USD) en H4 y M15.
Estrategia: cruce de EMA9/EMA21 filtrado por RSI(14), evaluada de forma
independiente en cada temporalidad (cada una alerta por su cuenta, etiquetada
con su timeframe).
Envía las alertas por Telegram. Diseñado para correr gratis y en tiempo real
vía GitHub Actions (cron cada 15 minutos), pero también funciona en loop local.

Requisitos:
    pip install requests

Variables de entorno necesarias:
    TELEGRAM_BOT_TOKEN  -> token del bot (te lo da @BotFather)
    TELEGRAM_CHAT_ID    -> tu chat_id (te lo da @userinfobot)

Uso:
    python bot_btc_h4.py            # corre una vez (revisa H4 y M15) y sale (ideal para cron/GitHub Actions)
    python bot_btc_h4.py --loop     # corre en loop continuo, revisando cada 1 minuto
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
SWING_LOOKBACK = 10       # velas hacia atrás para buscar el swing low/high estructural
ATR_STOP_MIN_MULT = 0.5   # el SL nunca queda a menos de 0.5 ATR (evita stops pegados al precio)
ATR_STOP_MAX_MULT = 3.0   # ni a más de 3 ATR (evita stops absurdamente amplios)
ATR_STOP_BUFFER_MULT = 0.1  # colchón extra más allá del swing, para no quedar exacto sobre el nivel
RISK_REWARD_RATIO = 2.0   # take profit = 2x la distancia del stop loss
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Temporalidades que evalúa el bot. Cada una corre la misma estrategia
# (EMA9/EMA21 + RSI) de forma independiente y alerta por separado.
TIMEFRAMES = [
    {"label": "H4", "kraken_interval": 240},
    {"label": "M15", "kraken_interval": 15},
]

# Nota: la API de Binance (api.binance.com) devuelve error 451 (bloqueo legal
# por región) para las IPs de los runners de GitHub Actions, así que usamos
# la API pública de Kraken, que no tiene esa restricción.
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_klines(pair="XBTUSD", interval_minutes=240, limit=100):
    """Trae velas históricas de BTC/USD desde Kraken (endpoint público, sin API key)."""
    params = {"pair": pair, "interval": interval_minutes}
    resp = requests.get(KRAKEN_OHLC_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key][-limit:]

    interval_ms = interval_minutes * 60 * 1000
    candles = []
    for row in raw:
        open_time = int(row[0]) * 1000
        candles.append({
            "open_time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
            "close_time": open_time + interval_ms,
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


def calculate_atr(candles, period=ATR_PERIOD):
    """ATR (Average True Range) de Wilder, para dimensionar el stop según la volatilidad real."""
    n = len(candles)
    tr = [None] * n
    for i in range(1, n):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    atr = [None] * n
    if n <= period:
        return atr

    atr[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def swing_low(candles, end_idx, lookback=SWING_LOOKBACK):
    start = max(0, end_idx - lookback + 1)
    return min(c["low"] for c in candles[start:end_idx + 1])


def swing_high(candles, end_idx, lookback=SWING_LOOKBACK):
    start = max(0, end_idx - lookback + 1)
    return max(c["high"] for c in candles[start:end_idx + 1])


def calculate_trade_levels(candles, i, entry, atr_val, direction):
    """
    Stop loss 'lógico': más allá del swing low/high reciente (estructura de mercado,
    no un número arbitrario), con un colchón y límites basados en ATR para que no
    quede ni pegado al precio ni absurdamente lejos. Take profit a partir de un
    ratio riesgo:beneficio fijo, como haría un trader disciplinado.
    """
    if direction == "BUY":
        structural = swing_low(candles, i)
        raw_sl = structural - ATR_STOP_BUFFER_MULT * atr_val
        dist = entry - raw_sl
        dist = min(max(dist, ATR_STOP_MIN_MULT * atr_val), ATR_STOP_MAX_MULT * atr_val)
        sl = entry - dist
        tp = entry + RISK_REWARD_RATIO * dist
    else:  # SELL
        structural = swing_high(candles, i)
        raw_sl = structural + ATR_STOP_BUFFER_MULT * atr_val
        dist = raw_sl - entry
        dist = min(max(dist, ATR_STOP_MIN_MULT * atr_val), ATR_STOP_MAX_MULT * atr_val)
        sl = entry + dist
        tp = entry - RISK_REWARD_RATIO * dist

    return {"stop_loss": sl, "take_profit": tp, "risk": dist, "risk_reward": RISK_REWARD_RATIO}


def detect_signal(candles):
    """
    Devuelve ('BUY'|'SELL'|None, info_dict) evaluando la vela recién cerrada contra
    la anterior, para detectar un cruce fresco de EMA9/EMA21 filtrado por RSI.
    Si hay señal, agrega niveles de entrada/stop loss/take profit.
    """
    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    rsi = calculate_rsi(closes, RSI_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)

    i = len(closes) - 1  # última vela cerrada
    prev = i - 1

    if None in (ema_fast[i], ema_slow[i], ema_fast[prev], ema_slow[prev], rsi[i], atr[i]):
        return None, {}

    crossed_up = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]

    entry = closes[i]
    info = {
        "close_time": candles[i]["close_time"],
        "price": entry,
        "ema_fast": round(ema_fast[i], 2),
        "ema_slow": round(ema_slow[i], 2),
        "rsi": round(rsi[i], 2),
        "atr": round(atr[i], 2),
    }

    direction = None
    if crossed_up and rsi[i] < 70:
        direction = "BUY"
    elif crossed_down and rsi[i] > 30:
        direction = "SELL"
    else:
        return None, info

    levels = calculate_trade_levels(candles, i, entry, atr[i], direction)
    info.update({
        "entry": round(entry, 2),
        "stop_loss": round(levels["stop_loss"], 2),
        "take_profit": round(levels["take_profit"], 2),
        "risk_reward": levels["risk_reward"],
    })
    return direction, info


def load_state():
    """
    Estado por temporalidad: {"H4": {"last_alerted_close_time": ...}, "M15": {...}}
    Mantiene compatibilidad si el archivo viejo tenía el formato plano (solo H4).
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}

    if "last_alerted_close_time" in state:  # formato viejo -> migrar a H4
        state = {"H4": {"last_alerted_close_time": state["last_alerted_close_time"]}}

    for tf in TIMEFRAMES:
        state.setdefault(tf["label"], {"last_alerted_close_time": None})

    return state


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


def format_message(signal, info, timeframe_label):
    emoji = "🟢" if signal == "BUY" else "🔴"
    accion = "COMPRA" if signal == "BUY" else "VENTA"
    ts = datetime.fromtimestamp(info["close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    entry = info["entry"]
    sl = info["stop_loss"]
    tp = info["take_profit"]
    riesgo_pct = abs(entry - sl) / entry * 100
    beneficio_pct = abs(tp - entry) / entry * 100

    return (
        f"{emoji} *Señal de {accion} - BTC/USD ({timeframe_label})*\n"
        f"Precio actual: ${info['price']:,.2f}\n"
        f"EMA9: {info['ema_fast']:,.2f} | EMA21: {info['ema_slow']:,.2f}\n"
        f"RSI(14): {info['rsi']} | ATR(14): {info['atr']:,.2f}\n\n"
        f"📍 Entrada: ${entry:,.2f}\n"
        f"🛑 Stop loss: ${sl:,.2f} (-{riesgo_pct:.2f}%)\n"
        f"🎯 Take profit: ${tp:,.2f} (+{beneficio_pct:.2f}%)\n"
        f"⚖️ Ratio riesgo:beneficio: 1:{info['risk_reward']:.0f}\n\n"
        f"Vela cerrada: {ts}\n\n"
        f"_Estrategia: cruce EMA9/EMA21 filtrado por RSI. Stop loss por debajo/encima del "
        f"swing reciente ajustado por ATR, take profit a 2x el riesgo. No es consejo "
        f"financiero — validá el setup antes de operar._"
    )


def check_timeframe(tf, state):
    label = tf["label"]
    candles = only_closed_candles(get_klines(interval_minutes=tf["kraken_interval"]))
    signal, info = detect_signal(candles)

    if not signal:
        print(f"[{datetime.now(timezone.utc).isoformat()}] [{label}] Sin señal nueva. "
              f"RSI={info.get('rsi')} EMA9={info.get('ema_fast')} EMA21={info.get('ema_slow')}")
        return

    if state[label].get("last_alerted_close_time") == info["close_time"]:
        print(f"[{label}] Señal ya alertada para esta vela, no se repite.")
        return

    message = format_message(signal, info, label)
    send_telegram_message(message)

    state[label]["last_alerted_close_time"] = info["close_time"]
    state[label]["last_signal"] = signal


def run_once():
    state = load_state()
    for tf in TIMEFRAMES:
        try:
            check_timeframe(tf, state)
        except Exception as e:
            print(f"[ERROR] [{tf['label']}] {e}")
    save_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Correr en loop continuo (revisa cada 1 min)")
    args = parser.parse_args()

    if args.loop:
        print("Bot corriendo en loop. Ctrl+C para detener.")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[ERROR] {e}")
            time.sleep(60)
    else:
        run_once()


if __name__ == "__main__":
    main()
