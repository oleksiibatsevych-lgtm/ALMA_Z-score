from datetime import datetime, timedelta
import os
import sqlite3
import threading
import time
from flask import Flask, request
import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
import yfinance as yf

app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_NAME = "bot_stats.db"
is_scanning = False  # Замок від паралельних сканувань

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
})

ALL_TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h"]
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


# --- БАЗА ДАНИХ ТА СТАТИСТИКА ---
def init_db():
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            pair TEXT,
            signal TEXT,
            price REAL,
            timeframe TEXT,
            expiration TEXT,
            target_time TEXT,
            z_score REAL,
            stoch REAL,
            result TEXT DEFAULT 'PENDING',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()
  conn.close()


def save_signal_to_db(
    chat_id,
    pair,
    signal,
    price,
    timeframe,
    expiration,
    target_time,
    z_score,
    stoch,
):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "INSERT INTO trades (chat_id, pair, signal, price, timeframe, expiration,"
      " target_time, z_score, stoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      (
          chat_id,
          pair,
          signal,
          price,
          timeframe,
          expiration,
          target_time,
          z_score,
          stoch,
      ),
  )
  conn.commit()
  conn.close()


def update_signal_status(trade_id, status):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "UPDATE trades SET result = ? WHERE id = ?", (status, trade_id)
  )
  conn.commit()
  conn.close()


def get_stats_report():
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      'SELECT result, COUNT(*) FROM trades WHERE result IN ("WIN", "LOSS") GROUP'
      " BY result"
  )
  data = dict(cursor.fetchall())
  conn.close()
  win = data.get("WIN", 0)
  loss = data.get("LOSS", 0)
  total = win + loss
  winrate = (win / total * 100) if total > 0 else 0
  return win, loss, total, winrate


# --- ФОНОВИЙ ПЕРЕВІРНИК РЕЗУЛЬТАТІВ УГОД ---
def automated_trade_checker():
  while True:
    try:
      now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
      conn = sqlite3.connect(DB_NAME)
      cursor = conn.cursor()
      cursor.execute(
          "SELECT id, chat_id, pair, signal, price, target_time FROM trades"
          " WHERE result = 'PENDING' AND target_time <= ?",
          (now_str,),
      )
      pending_trades = cursor.fetchall()
      conn.close()

      for trade in pending_trades:
        trade_id, chat_id, pair_name, signal_type, entry_price, target_time = (
            trade
        )
        ticker = PAIRS_MAP.get(pair_name)
        if not ticker:
          continue

        df = yf.download(
            ticker, period="1d", interval="1m", progress=False, session=session
        )
        if df is None or df.empty:
          continue
        if isinstance(df.columns, pd.MultiIndex):
          df.columns = df.columns.get_level_values(0)

        current_price = float(df["Close"].iloc[-1])
        outcome = "LOSS"
        if "BUY" in signal_type and current_price > entry_price:
          outcome = "WIN"
        elif "SELL" in signal_type and current_price < entry_price:
          outcome = "WIN"

        update_signal_status(trade_id, outcome)
        icon = "✅ WIN" if outcome == "WIN" else "❌ LOSS"
        msg = (
            f"🏁 <b>Результат угоди!</b>\nПара: <b>{pair_name}</b>\nСигнал:"
            f" <b>{signal_type}</b>\nВхід: {entry_price:.5f}\nВихід:"
            f" {current_price:.5f}\nСтатус: <b>{icon}</b>"
        )
        send_telegram_message(chat_id, msg)
      time.sleep(30)
    except Exception:
      time.sleep(30)


# --- ML ФІЛЬТР ---
def predict_ml_filter(curr_z, curr_stoch):
  try:
    conn = sqlite3.connect(DB_NAME)
    query = (
        'SELECT z_score, stoch, result FROM trades WHERE result IN ("WIN",'
        ' "LOSS") AND z_score IS NOT NULL AND stoch IS NOT NULL'
    )
    df_history = pd.read_sql(query, conn)
    conn.close()
    if len(df_history) < 10:
      return True

    X = df_history[["z_score", "stoch"]].values
    y = df_history["result"].apply(lambda r: 1 if r == "WIN" else 0).values
    if len(np.unique(y)) < 2:
      return True

    model = LogisticRegression()
    model.fit(X, y)
    prediction = model.predict(np.array([[curr_z, curr_stoch]]))[0]
    probability = model.predict_proba(np.array([[curr_z, curr_stoch]]))[0][1]
    return prediction == 1 or probability >= 0.45
  except Exception:
    return True


# --- ТЕЛЕГРАМ ІНТЕРФЕЙС ---
def send_telegram_message(chat_id, text, reply_markup=None):
  if not TELEGRAM_TOKEN:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
  if reply_markup:
    payload["reply_markup"] = reply_markup
  try:
    requests.post(url, json=payload, timeout=5)
  except:
    pass


def get_reply_keyboard():
  return {
      "keyboard": [
          [{"text": "💲 Пари"}],
          [{"text": "📊 Аналіз усіх пар"}, {"text": "📈 Статистика"}],
      ],
      "resize_keyboard": True,
  }


def get_pairs_grid_keyboard():
  pairs_list = list(PAIRS_MAP.keys())
  keyboard = []
  for i in range(0, len(pairs_list), 2):
    row = [{"text": pairs_list[i], "callback_data": f"analyze_{pairs_list[i]}"}]
    if i + 1 < len(pairs_list):
      row.append({
          "text": pairs_list[i + 1],
          "callback_data": f"analyze_{pairs_list[i+1]}",
      })
    keyboard.append(row)
  return {"inline_keyboard": keyboard}


# --- ЗАВАНТАЖЕННЯ ДАНИХ РИНКУ ---
def get_market_data(ticker, timeframe):
  try:
    if timeframe in ["1m", "3m", "10m"]:
      df = yf.download(
          ticker, period="5d", interval="1m", progress=False, session=session
      )
      if df is None or df.empty:
        return None
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      if timeframe == "3m":
        df = (
            df.resample("3min")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
            })
            .dropna()
        )
      elif timeframe == "10m":
        df = (
            df.resample("10min")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
            })
            .dropna()
        )
      return df
    else:
      df = yf.download(
          ticker,
          period="60d",
          interval=timeframe,
          progress=False,
          session=session,
      )
      if df is None or df.empty:
        return None
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      return df
  except:
    return None


# --- СТРАТЕГІЯ АНАЛІЗУ ---
def analyze_pair(pair_name, timeframe):
  ticker = PAIRS_MAP.get(pair_name)
  if not ticker:
    return None
  try:
    df = get_market_data(ticker, timeframe)
    if df is None or len(df) < 50:
      return None

    close = df["Close"]
    sma = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    z_score = (close - sma) / std

    low_min = df["Low"].rolling(window=14).min()
    high_max = df["High"].rolling(window=14).max()
    stoch = 100 * (close - low_min) / (high_max - low_min)
    trend = close.rolling(window=50).mean()

    curr_z = z_score.iloc[-1]
    curr_stoch = stoch.iloc[-1]
    curr_trend = trend.iloc[-1]
    curr_price = close.iloc[-1]

    signal = None
    if curr_z < -2.0 and curr_stoch < 20 and curr_price > curr_trend:
      signal = "BUY (CALL)"
    elif curr_z > 2.0 and curr_stoch > 80 and curr_price < curr_trend:
      signal = "SELL (PUT)"

    if not signal or not predict_ml_filter(float(curr_z), float(curr_stoch)):
      return None

    multiplier = 3 if "m" in timeframe else (2 if "h" in timeframe else 1)
    base_mins = (
        int(timeframe.replace("m", "").replace("h", ""))
        if timeframe.endswith(("m", "h"))
        else 5
    )
    if "h" in timeframe:
      base_mins *= 60

    dynamic_exp = int(base_mins * multiplier)
    expiration_str = (
        f"{dynamic_exp} хв"
        if dynamic_exp < 60
        else f"{dynamic_exp // 60} год"
    )
    target_time_str = (
        datetime.now() + timedelta(minutes=dynamic_exp)
    ).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "pair": pair_name,
        "signal": signal,
        "price": float(curr_price),
        "z_score": float(curr_z),
        "stoch": float(curr_stoch),
        "expiration": expiration_str,
        "target_time": target_time_str,
        "timeframe": timeframe,
    }
  except:
    return None


# --- ФОНОВЕ СКАНУВАННЯ З ЗАХИСТОМ ВІД БАНУ ---
def run_global_scan(chat_id):
  global is_scanning
  if is_scanning:
    send_telegram_message(
        chat_id, "⏳ Бот вже проводить сканування, зачекайте..."
    )
    return
  is_scanning = True
  try:
    send_telegram_message(
        chat_id, "⏳ Глобальне сканування ринку запущено (з урахуванням ML)..."
    )
    signals_found = 0
    for pair_name in PAIRS_MAP.keys():
      for tf in ALL_TIMEFRAMES:
        time.sleep(1.5)  # ПАУЗА ПРОТИ БАНУ YAHOO FINANCE
        res = analyze_pair(pair_name, tf)
        if res and res["signal"]:
          signals_found += 1
          save_signal_to_db(
              chat_id,
              res["pair"],
              res["signal"],
              res["price"],
              tf,
              res["expiration"],
              res["target_time"],
              res["z_score"],
              res["stoch"],
          )
          msg = (
              f"🚨 <b>Сигнал (AI Filtered)!</b>\nПара: <b>{res['pair']}</b>\nСигнал:"
              f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
              f" {res['z_score']:.2f}\nStoch:"
              f" {res['stoch']:.1f}\nТаймфрейм: <b>{tf}</b>\nЕкспірація:"
              f" <b>{res['expiration']}</b>"
          )
          send_telegram_message(chat_id, msg)
    if signals_found == 0:
      send_telegram_message(
          chat_id, "💤 За результатами сканування якісних сигналів немає."
      )
  finally:
    is_scanning = False


# --- WEBHOOK ОБРОБНИК ---
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
  update = request.get_json()
  if not update:
    return "OK", 200

  if "message" in update:
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    if text in ["/start", "/help"]:
      send_telegram_message(
          chat_id,
          "👋 Бот успішно підключено! Оберіть дію на клавіатурі нижче:",
          get_reply_keyboard(),
      )
    elif text == "💲 Пари":
      send_telegram_message(
          chat_id,
          "📌 Оберіть пару для швидкого сканування:",
          get_pairs_grid_keyboard(),
      )
    elif text == "📊 Аналіз усіх пар":
      threading.Thread(
          target=run_global_scan, args=(chat_id,), daemon=True
      ).start()
    elif text == "📈 Статистика":
      win, loss, total, winrate = get_stats_report()
      report = (
          f"📊 <b>Статистика угод:</b>\n\n✅ WIN: {win}\n❌ LOSS:"
          f" {loss}\n📦 Всього: {total}\n📈 Winrate: <b>{winrate:.2f}%</b>"
      )
      send_telegram_message(chat_id, report)

  elif "callback_query" in update:
    cb = update["callback_query"]
    data = cb["data"]
    chat_id = cb["message"]["chat"]["id"]
    if data.startswith("analyze_"):
      pair_name = data.replace("analyze_", "")

      def scan_single(c_id, p_name):
        send_telegram_message(c_id, f"🔍 Скаנую пару {p_name} по всіх таймфреймах...")
        found = 0
        for tf in ALL_TIMEFRAMES:
          time.sleep(1.0)
          res = analyze_pair(p_name, tf)
          if res and res["signal"]:
            found += 1
            save_signal_to_db(
                c_id,
                res["pair"],
                res["signal"],
                res["price"],
                tf,
                res["expiration"],
                res["target_time"],
                res["z_score"],
                res["stoch"],
            )
            msg = (
                f"🚨 <b>Сигнал для {res['pair']}</b>\nСигнал:"
                f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
                f" {res['z_score']:.2f}\nТаймфрейм: <b>{tf}</b>"
            )
            send_telegram_message(c_id, msg)
        if found == 0:
          send_telegram_message(c_id, f"ℹ️ По парі <b>{p_name}</b> сигналів немає.")

      threading.Thread(
          target=scan_single, args=(chat_id, pair_name), daemon=True
      ).start()

  return "OK", 200


if __name__ == "__main__":
  init_db()
  threading.Thread(target=automated_trade_checker, daemon=True).start()
  app.run(host="0.0.0.0", port=10000)
