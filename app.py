import os
import requests
import pandas as pd
import yfinance as yf
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = "8884979632:AAH9RZE8XZ2qK9eqyzvLofk854IcOQW6HFk"

PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", 
    "USDCHF=X", "EURGBP=X", "EURJPY=X", "EURCHF=X", "GBPJPY=X", 
    "AUDJPY=X", "AUDCAD=X", "EURAUD=X", "CADJPY=X", "GBPCHF=X"
]

TIMEFRAME = "5m"

def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка відправки: {e}")

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def analyze_single_pair(pair):
    try:
        df = yf.download(pair, period="5d", interval=TIMEFRAME, progress=False)
        if df.empty or len(df) < 200:
            return f"⚠️ Недостатньо даних для {pair.replace('=X', '')}"
        
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df['Close']
        high = df['High']
        low = df['Low']
        
        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        current_price = close.iloc[-1]
        rsi = calculate_rsi(close, 14).iloc[-1]
        
        swing_high = high.iloc[-50:].max()
        swing_low = low.iloc[-50:].min()
        diff = swing_high - swing_low
        
        fib_50 = swing_high - (diff * 0.5)
        fib_618 = swing_high - (diff * 0.618)
        in_fib_zone = (current_price <= fib_50) and (current_price >= fib_618)
        
        is_bullish_ob = (close.iloc[-2] < df['Open'].iloc[-2]) and (close.iloc[-1] > df['Open'].iloc[-1]) and ((close.iloc[-1] - df['Open'].iloc[-1]) > (high.iloc[-1] - low.iloc[-1]) * 0.6)
        is_bearish_ob = (close.iloc[-2] > df['Open'].iloc[-2]) and (close.iloc[-1] < df['Open'].iloc[-1]) and ((df['Open'].iloc[-1] - close.iloc[-1]) > (high.iloc[-1] - low.iloc[-1]) * 0.6)

        clean_name = pair.replace("=X", "")
        candle_size = high.iloc[-1] - low.iloc[-1]
        avg_size = (high - low).rolling(20).mean().iloc[-1]
        
        if candle_size > avg_size * 1.8:
            exp_min = 30
        elif candle_size > avg_size * 1.3:
            exp_min = 20
        elif candle_size < avg_size * 0.7:
            exp_min = 5
        else:
            exp_min = 15

        if current_price > ema200 and in_fib_zone and is_bullish_ob and rsi < 40:
            return f"🟢 **BUY (Експірація: {exp_min} хв)** | 🌟 **{clean_name}**\nЦіна: `{current_price:.5f}` | RSI: `{rsi:.1f}`"
        elif current_price < ema200 and in_fib_zone and is_bearish_ob and rsi > 60:
            return f"🔴 **SELL (Експірація: {exp_min} хв)** | 🌟 **{clean_name}**\nЦіна: `{current_price:.5f}` | RSI: `{rsi:.1f}`"
        else:
            return f"📭 По парі 🌟 **{clean_name}** зараз немає сигналу за умовами стратегії.\nЦіна: `{current_price:.5f}` | RSI: `{rsi:.1f}`"
    except Exception as e:
        return f"Помилка при аналізі {pair}: {e}"

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return "OK", 200
        
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text == "/start":
            reply = (
                "👋 Вітаю! Бот Racio_1 активний.\n"
                "⏱ Ручний вибір пари з динамічною експірацією (**від 5 до 30 хв**).\n\n"
                "📌 **Команди:**\n"
                "/signal — Вибрати пару для аналізу"
            )
            send_telegram_message(chat_id, reply)

        elif text == "/signal":
            keyboard = []
            row = []
            for pair in PAIRS:
                clean_name = pair.replace("=X", "")
                row.append({"text": clean_name, "callback_data": pair})
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            send_telegram_message(chat_id, "🔍 **Виберіть валютну пару для перевірки:**", {"inline_keyboard": keyboard})

    elif "callback_query" in update:
        query = update["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        pair = query["data"]
        
        send_telegram_message(chat_id, f"⏳ Аналізую графік для `{pair.replace('=X', '')}`...")
        result = analyze_single_pair(pair)
        send_telegram_message(chat_id, result)

    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!"
