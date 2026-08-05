import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", 
    "USDCHF=X", "EURGBP=X", "EURJPY=X", "EURCHF=X", "GBPJPY=X", 
    "AUDJPY=X", "AUDCAD=X", "EURAUD=X", "CADJPY=X", "GBPCHF=X"
]

# Діапазон завантаження даних (глибина до трьох місяців)
SCAN_TIMEFRAMES = {
    "1m": "5d",
    "5m": "5d",
    "15m": "1mo",
    "30m": "2mo",
    "1h": "3mo",
    "4h": "3mo"
}

# Сховище для статистики в пам'яті
stats_history = []

def log_stat(pair, signal_type):
    global stats_history
    stats_history.append({
        "timestamp": datetime.now(),
        "pair": pair.replace("=X", ""),
        "signal": signal_type # "BUY", "SELL", або "NO_SIGNAL"
    })

def get_statistics():
    now = datetime.now()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    
    stats_day = {}
    stats_week = {}
    stats_all = {}
    
    for item in stats_history:
        pair = item["pair"]
        t = item["timestamp"]
        sig = item["signal"]
        
        for s_dict in [stats_day, stats_week, stats_all]:
            if pair not in s_dict:
                s_dict[pair] = {"requests": 0, "buy": 0, "sell": 0, "none": 0}
                
        # Весь час
        stats_all[pair]["requests"] += 1
        if sig == "BUY": stats_all[pair]["buy"] += 1
        elif sig == "SELL": stats_all[pair]["sell"] += 1
        else: stats_all[pair]["none"] += 1
        
        # Тиждень
        if t >= week_ago:
            stats_week[pair]["requests"] += 1
            if sig == "BUY": stats_week[pair]["buy"] += 1
            elif sig == "SELL": stats_week[pair]["sell"] += 1
            else: stats_week[pair]["none"] += 1
            
        # Доба
        if t >= day_ago:
            stats_day[pair]["requests"] += 1
            if sig == "BUY": stats_day[pair]["buy"] += 1
            elif sig == "SELL": stats_day[pair]["sell"] += 1
            else: stats_day[pair]["none"] += 1
            
    return stats_day, stats_week, stats_all

def format_stats_text(title, data):
    if not data:
        return f"📊 *{title}*:\n\nЗа цей період ще немає збережених даних по запитах."
    
    text = f"📊 *{title} (по парах)*:\n\n"
    for pair, counts in data.items():
        text += f"🌟 *{pair}*:\n"
        text += f"  • Запитів: `{counts['requests']}` | BUY: `{counts['buy']}` | SELL: `{counts['sell']}` | Без сигналу: `{counts['none']}`\n\n"
    return text

def send_telegram_message(chat_id, text, reply_markup=None):
    if not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка відправки: {e}")

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка редагування: {e}")

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def analyze_all_timeframes(pair):
    clean_name = pair.replace("=X", "")
    found_signal = "NO_SIGNAL"
    signal_text = ""
    
    for tf, period in SCAN_TIMEFRAMES.items():
        try:
            df = yf.download(pair, period=period, interval=tf, progress=False)
            if df.empty or len(df) < 30:
                continue
            
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df['Close']
            high = df['High']
            low = df['Low']
            
            ema_span = 200 if len(close) >= 200 else len(close) - 1
            ema200 = close.ewm(span=ema_span, adjust=False).mean().iloc[-1]
            current_price = close.iloc[-1]
            rsi = calculate_rsi(close, 14).iloc[-1]
            
            swing_high = high.iloc[-50:].max() if len(high) >= 50 else high.max()
            swing_low = low.iloc[-50:].min() if len(low) >= 50 else low.min()
            diff = swing_high - swing_low
            
            if diff == 0:
                continue
                
            fib_50 = swing_high - (diff * 0.5)
            fib_618 = swing_high - (diff * 0.618)
            in_fib_zone = (current_price <= fib_50) and (current_price >= fib_618)
            
            is_bullish_ob = (close.iloc[-2] < df['Open'].iloc[-2]) and (close.iloc[-1] > df['Open'].iloc[-1]) and ((close.iloc[-1] - df['Open'].iloc[-1]) > (high.iloc[-1] - low.iloc[-1]) * 0.6)
            is_bearish_ob = (close.iloc[-2] > df['Open'].iloc[-2]) and (close.iloc[-1] < df['Open'].iloc[-1]) and ((df['Open'].iloc[-1] - close.iloc[-1]) > (high.iloc[-1] - low.iloc[-1]) * 0.6)

            candle_size = high.iloc[-1] - low.iloc[-1]
            avg_size = (high - low).rolling(20).mean().iloc[-1]
            if pd.isna(avg_size) or avg_size == 0:
                avg_size = candle_size

            # Базовий час експлуатації залежно від таймфрейму (від 1 хв до 4 годин / 240 хв)
            if tf == "1m": base_exp = 5
            elif tf == "5m": base_exp = 15
            elif tf == "15m": base_exp = 30
            elif tf == "30m": base_exp = 60
            elif tf == "1h": base_exp = 120
            elif tf == "4h": base_exp = 240
            else: base_exp = 30

            if candle_size > avg_size * 1.8:
                exp_min = base_exp * 2
            elif candle_size > avg_size * 1.3:
                exp_min = int(base_exp * 1.5)
            elif candle_size < avg_size * 0.7:
                exp_min = max(1, int(base_exp * 0.5))
            else:
                exp_min = base_exp
            
            # Обмежуємо діапазон експірації від 1 хв до 4 годин (240 хв)
            exp_min = max(1, min(240, exp_min))

            # Форматування виведення часу (години або хвилини)
            if exp_min >= 60:
                hours = exp_min // 60
                mins = exp_min % 60
                exp_str = f"{hours} год" + (f" {mins} хв" if mins > 0 else "")
            else:
                exp_str = f"{exp_min} хв"

            if current_price > ema200 and in_fib_zone and is_bullish_ob and rsi < 40:
                found_signal = "BUY"
                signal_text = f"🟢 **BUY (ТФ: {tf})** | 🌟 **{clean_name}**\n⏱ Експірація: `{exp_str}`\nЦіна: `{current_price:.5f}` | RSI: `{rsi:.1f}`"
                break
            elif current_price < ema200 and in_fib_zone and is_bearish_ob and rsi > 60:
                found_signal = "SELL"
                signal_text = f"🔴 **SELL (ТФ: {tf})** | 🌟 **{clean_name}**\n⏱ Експірація: `{exp_str}`\nЦіна: `{current_price:.5f}` | RSI: `{rsi:.1f}`"
                break
        except Exception as e:
            continue
            
    log_stat(pair, found_signal)
    
    if found_signal != "NO_SIGNAL":
        return signal_text
    else:
        return f"📭 По парі 🌟 **{clean_name}** (ТФ від 1хв до 4г) зараз немає сигналу за стратегією."

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
                "👋 Вітаю! Бот працює за запитом (глибина даних до 3 місяців, експірація від 1 хв до 4 годин).\n\n"
                "📌 **Команди:**\n"
                "/signal — Вибрати пару для аналізу\n"
                "/stats — Переглянути статистику запитів по парах"
            )
            send_telegram_message(chat_id, reply)

        elif text == "/signal":
            keyboard = []
            row = []
            for pair in PAIRS:
                clean_name = pair.replace("=X", "")
                row.append({"text": clean_name, "callback_data": f"pair|{pair}"})
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            send_telegram_message(chat_id, "🔍 **Виберіть валютну пару для аналізу (1хв — 4 год):**", {"inline_keyboard": keyboard})

        elif text == "/stats":
            stats_day, stats_week, stats_all = get_statistics()
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📅 За добу", "callback_data": "stats|day"},
                     {"text": "📆 За тиждень", "callback_data": "stats|week"}],
                    [{"text": "📈 За весь час", "callback_data": "stats|all"}]
                ]
            }
            send_telegram_message(chat_id, "📊 **Виберіть період для перегляду статистики по парах:**", keyboard)

    elif "callback_query" in update:
        query = update["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]
        data = query["data"]

        if data.startswith("pair|"):
            _, pair = data.split("|")
            clean_name = pair.replace("=X", "")
            
            send_telegram_message(chat_id, f"⏳ Сканую таймфрейми (глибина до 3 міс) для **{clean_name}**...")
            result = analyze_all_timeframes(pair)
            send_telegram_message(chat_id, result)

        elif data.startswith("stats|"):
            _, period = data.split("|")
            stats_day, stats_week, stats_all = get_statistics()
            if period == "day":
                text = format_stats_text("Статистика за добу", stats_day)
            elif period == "week":
                text = format_stats_text("Статистика за тиждень", stats_week)
            else:
                text = format_stats_text("Статистика за весь час", stats_all)
                
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Оновити", "callback_data": f"stats|{period}"}],
                    [{"text": "« Назад до вибору періоду", "callback_data": "stats|menu"}]
                ]
            }
            edit_telegram_message(chat_id, message_id, text, keyboard)

        elif data == "stats|menu":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📅 За добу", "callback_data": "stats|day"},
                     {"text": "📆 За тиждень", "callback_data": "stats|week"}],
                    [{"text": "📈 За весь час", "callback_data": "stats|all"}]
                ]
            }
            edit_telegram_message(chat_id, message_id, "📊 **Виберіть період для перегляду статистики по парах:**", keyboard)

    return "OK", 200

@app.route("/")
def home():
    return "Optimized Bot is running!"
