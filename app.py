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

SCAN_TIMEFRAMES = {
    "1m": "5d",
    "5m": "5d",
    "15m": "1mo",
    "30m": "2mo",
    "1h": "3mo",
    "4h": "3mo"
}

stats_history = []

def log_stat(pair, signal_type):
    global stats_history
    stats_history.append({
        "timestamp": datetime.now(),
        "pair": pair.replace("=X", ""),
        "signal": signal_type
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
                
        stats_all[pair]["requests"] += 1
        if sig == "BUY": stats_all[pair]["buy"] += 1
        elif sig == "SELL": stats_all[pair]["sell"] += 1
        else: stats_all[pair]["none"] += 1
        
        if t >= week_ago:
            stats_week[pair]["requests"] += 1
            if sig == "BUY": stats_week[pair]["buy"] += 1
            elif sig == "SELL": stats_week[pair]["sell"] += 1
            else: stats_week[pair]["none"] += 1
            
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

def calculate_stochastic(df, k_period=14, d_period=3):
    low_min = df['Low'].rolling(window=k_period).min()
    high_max = df['High'].rolling(window=k_period).max()
    k_line = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    d_line = k_line.rolling(window=d_period).mean()
    return k_line, d_line

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
            open_p = df['Open']
            
            ema_span = 200 if len(close) >= 200 else len(close) - 1
            ema200 = close.ewm(span=ema_span, adjust=False).mean().iloc[-1]
            current_price = close.iloc[-1]
            
            k_line, d_line = calculate_stochastic(df)
            k_val = k_line.iloc[-1]
            d_val = d_line.iloc[-1]
            
            if pd.isna(k_val) or pd.isna(d_val):
                continue
            
            swing_high = high.iloc[-50:].max() if len(high) >= 50 else high.max()
            swing_low = low.iloc[-50:].min() if len(low) >= 50 else low.min()
            diff = swing_high - swing_low
            
            if diff == 0:
                continue
                
            fib_618 = swing_high - (diff * 0.618)
            fib_786 = swing_high - (diff * 0.786)
            in_ote_buy = (current_price <= fib_618) and (current_price >= fib_786)

            fib_sell_low = swing_low + (diff * 0.382)
            fib_sell_high = swing_low + (diff * 0.618)
            in_ote_sell = (current_price >= fib_sell_low) and (current_price <= fib_sell_high)

            is_bullish_ob = (close.iloc[-1] > open_p.iloc[-1]) and (close.iloc[-2] < open_p.iloc[-2])
            is_bearish_ob = (close.iloc[-1] < open_p.iloc[-1]) and (close.iloc[-2] > open_p.iloc[-2])

            candle_size = high.iloc[-1] - low.iloc[-1]
            avg_size = (high - low).rolling(20).mean().iloc[-1]
            if pd.isna(avg_size) or avg_size == 0:
                avg_size = candle_size

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
            
            exp_min = max(1, min(240, exp_min))

            if exp_min >= 60:
                hours = exp_min // 60
                mins = exp_min % 60
                exp_str = f"{hours} год" + (f" {mins} хв" if mins > 0 else "")
            else:
                exp_str = f"{exp_min} хв"

            if current_price > ema200 and in_ote_buy and is_bullish_ob and k_val < 40:
                found_signal = "BUY"
                signal_text = f"🟢 **BUY (SMC + Fib | ТФ: {tf})** | 🌟 **{clean_name}**\n⏱ Експірація: `{exp_str}`\nЦіна: `{current_price:.5f}` | Stoch %K: `{k_val:.1f}`"
                break
            elif current_price < ema200 and in_ote_sell and is_bearish_ob and k_val > 60:
                found_signal = "SELL"
                signal_text = f"🔴 **SELL (SMC + Fib | ТФ: {tf})** | 🌟 **{clean_name}**\n⏱ Експірація: `{exp_str}`\nЦіна: `{current_price:.5f}` | Stoch %K: `{k_val:.1f}`"
                break
        except Exception as e:
            continue
            
    log_stat(pair, found_signal)
    return found_signal, signal_text

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return "OK", 200
        
    main_menu_keyboard = {
        "keyboard": [
            [{"text": "📊 Аналізувати пару"}, {"text": "📈 Статистика"}]
        ],
        "resize_keyboard": True
    }

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text == "/start":
            reply = (
                "👋 Вітаю! Бот налаштований на масове сканування.\n\n"
                "Натисніть **«📊 Аналізувати пару»**, щоб перевірити всі валютні пари одразу:"
            )
            send_telegram_message(chat_id, reply, main_menu_keyboard)

        elif text in ["/signal", "📊 Аналізувати пару"]:
            send_telegram_message(chat_id, "⏳ Починаю масове сканування всіх валютних пар за стратегією SMC + Фібоначі...")
            
            signals_found = 0
            for pair in PAIRS:
                found_signal, signal_text = analyze_all_timeframes(pair)
                if found_signal != "NO_SIGNAL":
                    send_telegram_message(chat_id, signal_text)
                    signals_found += 1
            
            if signals_found == 0:
                send_telegram_message(chat_id, "📭 Зараз немає активних сигналів ні по одній парі.")
            else:
                send_telegram_message(chat_id, f"✅ Сканування завершено. Знайдено сигналів: {signals_found}")

        elif text in ["/stats", "📈 Статистика"]:
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

        if data.startswith("stats|"):
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
    return "Mass Scan SMC Bot is running!"
