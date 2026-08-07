import os
import sqlite3
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Flask, request
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Точний список із 21 валютної пари відповідно до вашого інтерфейсу
PAIRS_MAP = {
    "CHF/JPY": "CHFJPY=X",
    "AUD/CAD": "AUDCAD=X",
    "GBP/AUD": "GBPAUD=X",
    "EUR/USD": "EURUSD=X",
    "EUR/CAD": "EURCAD=X",
    "AUD/USD": "AUDUSD=X",
    "AUD/CHF": "AUDCHF=X",
    "CAD/CHF": "CADCHF=X",
    "EUR/CHF": "EURCHF=X",
    "GBP/CHF": "GBPCHF=X",
    "USD/CAD": "USDCAD=X",
    "GBP/USD": "GBPUSD=X",
    "GBP/JPY": "GBPJPY=X",
    "EUR/AUD": "EURAUD=X",
    "CAD/JPY": "CADJPY=X",
    "USD/CHF": "USDCHF=X",
    "EUR/GBP": "EURGBP=X",
    "USD/JPY": "USDJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/CAD": "GBPCAD=X",
}

SCAN_TIMEFRAMES = {
    "1m": "5d",
    "3m": "5d",
    "5m": "1mo"
}

# ==================== РОБОТА З БД (SQLITE) ====================
def init_db():
    conn = sqlite3.connect('bot_stats.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            pair TEXT,
            signal_type TEXT,
            price REAL,
            status TEXT DEFAULT 'PENDING',
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_signal_to_db(chat_id, pair, signal_type, price):
    try:
        conn = sqlite3.connect('bot_stats.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO signals (chat_id, pair, signal_type, price, status, timestamp)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
        ''', (chat_id, pair, signal_type, price, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return signal_id
    except Exception as e:
        print(f"Помилка збереження в БД: {e}")
        return None

def update_signal_status(signal_id, status):
    try:
        conn = sqlite3.connect('bot_stats.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('UPDATE signals SET status = ? WHERE id = ?', (status, signal_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Помилка оновлення статусу: {e}")

def get_db_statistics(period_days=None):
    try:
        conn = sqlite3.connect('bot_stats.db', check_same_thread=False)
        cursor = conn.cursor()
        
        time_filter = ""
        if period_days:
            limit_date = (datetime.utcnow() - timedelta(days=period_days)).strftime("%Y-%m-%d %H:%M:%S")
            time_filter = f"AND timestamp >= '{limit_date}'"

        cursor.execute(f"SELECT COUNT(*), SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END), SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END) FROM signals WHERE status != 'PENDING' {time_filter}")
        tot, wins, losses = cursor.fetchone()
        tot = tot or 0
        wins = wins or 0
        losses = losses or 0
        winrate = round((wins / tot * 100), 1) if tot > 0 else 0

        cursor.execute(f"""
            SELECT pair, COUNT(*), 
                   SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END), 
                   SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END)
            FROM signals 
            WHERE status != 'PENDING' {time_filter}
            GROUP BY pair
            ORDER BY COUNT(*) DESC
        """)
        pairs_data = cursor.fetchall()
        conn.close()
        
        return {"total": tot, "wins": wins, "losses": losses, "winrate": winrate, "pairs": pairs_data}
    except Exception as e:
        print(f"Помилка читання статистики: {e}")
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0, "pairs": []}

def format_stats_report(title, stats):
    text = (
        f"📊 *{title}*\n\n"
        f"🎯 Закрито угод: `{stats['total']}`\n"
        f"✅ Плюс (Win): `{stats['wins']}`\n"
        f"❌ Мінус (Loss): `{stats['losses']}`\n"
        f"📈 **Winrate:** `{stats['winrate']}%`\n\n"
        f"💱 *По валютних парах*:\n"
    )
    if not stats['pairs']:
        text += "_Ще немає закритого торгового результату за цей період._"
    else:
        for p, tot, w, l in stats['pairs']:
            p_wr = round((w / tot * 100), 1) if tot > 0 else 0
            text += f"🔹 *{p}*: угод `{tot}` | Плюс `{w}` | Мінус `{l}` | Winrate: **`{p_wr}%`**\n"
    return text

# ==================== МЕНЮ ТА КЛАВІАТУРИ ====================
def get_main_menu_keyboard():
    pairs_list = list(PAIRS_MAP.keys())
    keyboard = []
    for i in range(0, len(pairs_list), 2):
        row = [{"text": pairs_list[i], "callback_data": f"analyze_{pairs_list[i]}"}]
        if i + 1 < len(pairs_list):
            row.append({"text": pairs_list[i+1], "callback_data": f"analyze_{pairs_list[i+1]}"})
        keyboard.append(row)
    
    keyboard.append([{"text": "📊 Аналіз усіх пар", "callback_data": "analyze_all"}])
    keyboard.append([{"text": "📈 Статистика", "callback_data": "menu_stats"}])
    return {"inline_keyboard": keyboard}

# ==================== TELEGRAM API ====================
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

def edit_telegram_reply_markup(chat_id, message_id, reply_markup):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка оновлення розмітки: {e}")

def is_active_trading_session():
    now_utc = datetime.utcnow()
    hour = now_utc.hour
    if now_utc.weekday() < 5 and (7 <= hour <= 19):
        return True
    return False

def calculate_stochastic(df, k_period=14, d_period=3):
    low_min = df['Low'].rolling(window=k_period).min()
    high_max = df['High'].rolling(window=k_period).max()
    k_line = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    d_line = k_line.rolling(window=d_period).mean()
    return k_line, d_line

def calculate_rsi(df, period=14):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def get_htf_trend(ticker):
    try:
        df_htf = yf.download(ticker, period="5d", interval="1h", progress=False)
        if df_htf.empty or len(df_htf) < 20:
            return "NEUTRAL"
        if isinstance(df_htf.columns, pd.MultiIndex):
            df_htf.columns = df_htf.columns.get_level_values(0)
        
        close = df_htf['Close']
        ema50 = close.ewm(span=50 if len(close)>=50 else len(close)-1, adjust=False).mean().iloc[-1]
        current_price = close.iloc[-1]
        
        if current_price > ema50:
            return "BULLISH"
        elif current_price < ema50:
            return "BEARISH"
    except:
        pass
    return "NEUTRAL"

def analyze_single_pair(display_name, chat_id):
    ticker = PAIRS_MAP.get(display_name)
    if not ticker:
        return None

    htf_trend = get_htf_trend(ticker)
    if htf_trend == "NEUTRAL":
        return None

    for tf, period in SCAN_TIMEFRAMES.items():
        try:
            df = yf.download(ticker, period=period, interval=tf, progress=False)
            if df.empty or len(df) < 30:
                continue
            
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            close = df['Close']
            high = df['High']
            low = df['Low']
            open_p = df['Open']
            
            current_price = close.iloc[-1]
            k_line, _ = calculate_stochastic(df)
            k_val = k_line.iloc[-1]
            
            rsi_line = calculate_rsi(df)
            rsi_val = rsi_line.iloc[-1]
            
            if pd.isna(k_val) or pd.isna(rsi_val):
                continue
            
            swing_high = high.iloc[-30:].max()
            swing_low = low.iloc[-30:].min()
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

            liquidity_sweep_buy = low.iloc[-1] <= low.iloc[-10:-1].min()
            liquidity_sweep_sell = high.iloc[-1] >= high.iloc[-10:-1].max()

            if tf == "1m": exp_str = "3-5 хв"
            elif tf == "3m": exp_str = "5-10 хв"
            else: exp_str = "15-20 хв"

            if htf_trend == "BULLISH" and in_ote_buy and is_bullish_ob and liquidity_sweep_buy and k_val < 40 and rsi_val > 40:
                signal_id = save_signal_to_db(chat_id, display_name, "BUY", current_price)
                signal_text = (
                    f"🟢 **BUY (SMC + Fib + Sweep | ТФ: {tf})** | 🌟 **{display_name}**\n"
                    f"⏱ Експірація: `{exp_str}`\n"
                    f"Ціна: `{current_price:.5f}` | Stoch: `{k_val:.1f}` | RSI: `{rsi_val:.1f}`\n\n"
                    f"👇 **Позначте результат після завершення угоди:**"
                )
                keyboard = {
                    "inline_keyboard": [[
                        {"text": "✅ Плюс (Win)", "callback_data": f"win_{signal_id}"},
                        {"text": "❌ Мінус (Loss)", "callback_data": f"loss_{signal_id}"}
                    ]]
                }
                return (signal_text, keyboard)
            
            elif htf_trend == "BEARISH" and in_ote_sell and is_bearish_ob and liquidity_sweep_sell and k_val > 60 and rsi_val < 60:
                signal_id = save_signal_to_db(chat_id, display_name, "SELL", current_price)
                signal_text = (
                    f"🔴 **SELL (SMC + Fib + Sweep | ТФ: {tf})** | 🌟 **{display_name}**\n"
                    f"⏱ Експірація: `{exp_str}`\n"
                    f"Ціна: `{current_price:.5f}` | Stoch: `{k_val:.1f}` | RSI: `{rsi_val:.1f}`\n\n"
                    f"👇 **Позначте результат після завершення угоди:**"
                )
                keyboard = {
                    "inline_keyboard": [[
                        {"text": "✅ Плюс (Win)", "callback_data": f"win_{signal_id}"},
                        {"text": "❌ Мінус (Loss)", "callback_data": f"loss_{signal_id}"}
                    ]]
                }
                return (signal_text, keyboard)
        except Exception as e:
            continue
    return None

# ==================== FLASK ROUTE ====================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return "OK", 200

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text == "/start":
            send_telegram_message(chat_id, "👋 **Оберіть пару для аналізу або скористайтеся меню:**", get_main_menu_keyboard())

    elif "callback_query" in update:
        query = update["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]
        data = query["data"]

        if data.startswith("analyze_") and data != "analyze_all":
            display_name = data.replace("analyze_", "")
            send_telegram_message(chat_id, f"⏳ Сканую пару *{display_name}*...")
            
            res = analyze_single_pair(display_name, chat_id)
            if res:
                sig_text, kb = res
                send_telegram_message(chat_id, sig_text, kb)
            else:
                send_telegram_message(chat_id, f"📭 Зараз немає активних сигналів по парі *{display_name}* за суворими критеріями.")

        elif data == "analyze_all":
            if not is_active_trading_session():
                send_telegram_message(chat_id, "⚠️ Увага: Зараз поза межами активних сесій (робочий час: 10:00 - 22:00 за Києвом).")
            
            send_telegram_message(chat_id, "⏳ Починаю паралельне сканування всіх 21 пар...")
            
            signals_found = 0
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(analyze_single_pair, pair_name, chat_id): pair_name for pair_name in PAIRS_MAP.keys()}
                for future in as_completed(futures):
                    res = future.result()
                    if res:
                        sig_text, kb = res
                        send_telegram_message(chat_id, sig_text, kb)
                        signals_found += 1
            
            if signals_found == 0:
                send_telegram_message(chat_id, "📭 Зараз немає активних сигналів по жодній парі.")
            else:
                send_telegram_message(chat_id, f"✅ Сканування завершено. Знайдено якісних сигналів: {signals_found}")

        elif data == "menu_stats":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📅 За добу", "callback_data": "stats|day"},
                     {"text": "📆 За тиждень", "callback_data": "stats|week"}],
                    [{"text": "📈 За весь час", "callback_data": "stats|all"}],
                    [{"text": "🔙 Головне меню", "callback_data": "menu_back"}]
                ]
            }
            edit_telegram_message(chat_id, message_id, "📊 **Виберіть період статистики:**", keyboard)

        elif data == "menu_back":
            edit_telegram_message(chat_id, message_id, "👋 **Оберіть пару для аналізу або скористайтеся меню:**", get_main_menu_keyboard())

        elif data.startswith("stats|"):
            _, period = data.split("|")
            if period == "day":
                st = get_db_statistics(period_days=1)
                text = format_stats_report("Статистика за добу", st)
            elif period == "week":
                st = get_db_statistics(period_days=7)
                text = format_stats_report("Статистика за тиждень", st)
            else:
                st = get_db_statistics(period_days=None)
                text = format_stats_report("Статистика за весь час", st)

            keyboard = {
                "inline_keyboard": [
                    [{"text": "📅 За добу", "callback_data": "stats|day"},
                     {"text": "📆 За тиждень", "callback_data": "stats|week"}],
                    [{"text": "📈 За весь час", "callback_data": "stats|all"}],
                    [{"text": "🔄 Оновити", "callback_data": f"stats|{period}"}],
                    [{"text": "🔙 Головне меню", "callback_data": "menu_back"}]
                ]
            }
            edit_telegram_message(chat_id, message_id, text, keyboard)

        elif data.startswith("win_") or data.startswith("loss_"):
            status = "WIN" if data.startswith("win_") else "LOSS"
            signal_id = data.split("_")[1]
            
            update_signal_status(signal_id, status)
            
            status_text = "🟢 Зараховано як ПЛЮС (Win)" if status == "WIN" else "🔴 Зараховано як МІНУС (Loss)"
            edit_telegram_reply_markup(chat_id, message_id, {"inline_keyboard": [[
                {"text": f"Статус: {status_text}", "callback_data": "none"}
            ]]})

    return "OK", 200

@app.route("/")
def home():
    return "21-Pairs SMC Bot with SQLite & Win/Loss is running!"

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000)
