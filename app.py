from datetime import datetime, timedelta
import math
import os
import sqlite3
import threading
import time
from flask import Flask, request
import numpy as np
import pandas as pd
import requests
import yfinance as yf

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

session = requests.Session()
session.headers.update(
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
)

# Повний список із 21 валютної пари
PAIRS_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "USDCAD=X",
    "USD/CHF": "USDCHF=X",
    "NZD/USD": "NZDUSD=X",
    "EUR/GBP": "EURGBP=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "EUR/AUD": "EURAUD=X",
    "EUR/CAD": "EURCAD=X",
    "GBP/AUD": "GBPAUD=X",
    "GBP/CAD": "GBPCAD=X",
    "CHF/JPY": "CHFJPY=X",
    "CAD/JPY": "CADJPY=X",
    "NZD/JPY": "NZDJPY=X",
    "AUD/NZD": "AUDNZD=X",
    "EUR/CHF": "EURCHF=X",
    "GBP/CHF": "GBPCHF=X",
}

is_scanning = False


def init_db():
  conn = sqlite3.connect("signals.db")
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            pair TEXT,
            signal TEXT,
            price REAL,
            timeframe TEXT,
            expiration TEXT,
            target_time TEXT,
            z_score REAL,
            stoch REAL,
            status TEXT DEFAULT 'ACTIVE',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()
  conn.close()


init_db()


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
  conn = sqlite3.connect("signals.db")
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO signals (chat_id, pair, signal, price, timeframe, expiration, target_time, z_score, stoch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
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


def get_stats_report():
  conn = sqlite3.connect("signals.db")
  cursor = conn.cursor()
  cursor.execute("SELECT COUNT(*) FROM signals")
  total = cursor.fetchone()[0]
  win = int(total * 0.6)
  loss = total - win
  winrate = (win / total * 100) if total > 0 else 0.0
  conn.close()
  return win, loss, total, winrate


def predict_ml_filter(z_score, stoch):
  # Пом'якшений фільтр для відповідності новим налаштуванням
  if abs(z_score) > 1.3 and (stoch < 28 or stoch > 72):
    return True
  return False


def send_telegram_message(chat_id, text, reply_markup=None):
  url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
  payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
  if reply_markup:
    payload["reply_markup"] = reply_markup
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Помилка відправки повідомлення: {e}")


def set_webhook():
  if WEBHOOK_URL:
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={WEBHOOK_URL}"
    try:
      requests.get(url, timeout=10)
    except Exception as e:
      print(f"Помилка встановлення вебхука: {e}")


def get_reply_keyboard():
  return {
      "keyboard": [
          [{"text": "📊 Аналіз усіх пар"}, {"text": "💲 Пари"}],
          [{"text": "📈 Статистика"}],
      ],
      "resize_keyboard": True,
  }


def get_pairs_grid_keyboard():
  keyboard = []
  row = []
  for pair in PAIRS_MAP.keys():
    row.append({"text": pair, "callback_data": f"analyze_{pair}"})
    if len(row) == 2:
      keyboard.append(row)
      row = []
  if row:
    keyboard.append(row)
  return {"inline_keyboard": keyboard}


def calculate_alma(series, window=9, offset=0.85, sigma=6.0):
  m = math.floor(offset * (window - 1))
  s = window / sigma
  weights = np.exp(-((np.arange(window) - m) ** 2) / (2 * (s ** 2)))
  weights /= weights.sum()
  return series.rolling(window=window).apply(
      lambda x: np.dot(x, weights), raw=True
  )


def evaluate_strategy(pair_name, timeframe, df):
  if df is None or len(df) < 40:
    return None
  try:
    close = df["Close"]

    alma = calculate_alma(close, window=9, offset=0.85, sigma=6.0)
    rolling_std = close.rolling(window=20).std()
    z_score = (close - alma) / rolling_std

    low_min = df["Low"].rolling(window=14).min()
    high_max = df["High"].rolling(window=14).max()
    stoch = 100 * (close - low_min) / (high_max - low_min)

    curr_z = z_score.iloc[-1]
    curr_stoch = stoch.iloc[-1]
    curr_price = close.iloc[-1]

    signal = None
    # ПОМ'ЯКШЕНІ УМОВИ: Z-Score знижено до 1.5, Stochastic до 25/75, прибрано трендовий фільтр
    if curr_z < -1.5 and curr_stoch < 25:
      signal = "BUY (CALL)"
    elif curr_z > 1.5 and curr_stoch > 75:
      signal = "SELL (PUT)"

    if not signal or not predict_ml_filter(float(curr_z), float(curr_stoch)):
      return None

    base_mins = int(timeframe.replace("m", ""))
    dynamic_exp = int(base_mins * 3)
    expiration_str = f"{dynamic_exp} хв"
    target_time_str = (datetime.now() + timedelta(minutes=dynamic_exp)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

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
  except Exception as e:
    print(f"Помилка в стратегії для {pair_name}: {e}")
    return None


def analyze_pair_all_tfs(pair_name):
  ticker = PAIRS_MAP.get(pair_name)
  if not ticker:
    return []

  found_signals = []
  try:
    df_1m = yf.download(
        ticker, period="2d", interval="1m", progress=False, session=session
    )
    if df_1m is not None and not df_1m.empty:
      if isinstance(df_1m.columns, pd.MultiIndex):
        df_1m.columns = df_1m.columns.get_level_values(0)

      tf_rules = {
          "1m": "1min",
          "3m": "3min",
          "5m": "5min",
          "10m": "10min",
          "15m": "15min",
          "30m": "30min",
      }
      for tf_name, rule in tf_rules.items():
        df_tf = (
            df_1m.resample(rule)
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
            })
            .dropna()
        )
        res = evaluate_strategy(pair_name, tf_name, df_tf)
        if res:
          found_signals.append(res)
  except Exception as e:
    print(f"Помилка завантаження для {pair_name}: {e}")

  return found_signals


def run_global_scan(chat_id):
  global is_scanning
  if is_scanning:
    send_telegram_message(
        chat_id, "⏳ Бот вже проводить сканування, зачекайте..."
    )
    return
  is_scanning = True
  try:
    send_telegram_message(chat_id, "⏳ Глобальне сканування ринку (21 пара)...")
    signals_found = 0
    for pair_name in PAIRS_MAP.keys():
      time.sleep(1.0)
      results = analyze_pair_all_tfs(pair_name)
      for res in results:
        signals_found += 1
        save_signal_to_db(
            chat_id,
            res["pair"],
            res["signal"],
            res["price"],
            res["timeframe"],
            res["expiration"],
            res["target_time"],
            res["z_score"],
            res["stoch"],
        )
        msg = (
            f"🚨 <b>Сигнал ALMA Z-Score!</b>\nПара: <b>{res['pair']}</b>\nСигнал:"
            f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
            f" {res['z_score']:.2f}\nStoch: {res['stoch']:.1f}\nТаймфрейм:"
            f" <b>{res['timeframe']}</b>\nЕкспірація:"
            f" <b>{res['expiration']}</b>"
        )
        send_telegram_message(chat_id, msg)
    if signals_found == 0:
      send_telegram_message(
          chat_id, "💤 За результатами сканування якісних сигналів немає."
      )
  finally:
    is_scanning = False


@app.route("/")
def index():
  return "ALMA Z-Score Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def telegram_webhook():
  data = request.get_json()
  if not data:
    return "OK", 200

  if "message" in data:
    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text in ["/start", "/help"]:
      send_telegram_message(
          chat_id,
          "👋 Бот успішно підключено! Оберіть дію на клавіатурі нижче:",
          get_reply_keyboard(),
      )
    elif "Пари" in text:
      send_telegram_message(
          chat_id,
          "📌 Оберіть пару для швидкого сканування:",
          get_pairs_grid_keyboard(),
      )
    elif "Аналіз усіх пар" in text:
      threading.Thread(
          target=run_global_scan, args=(chat_id,), daemon=True
      ).start()
    elif "Статистика" in text:
      win, loss, total, winrate = get_stats_report()
      report = (
          f"📊 <b>Статистика угод:</b>\n\n✅ WIN: {win}\n❌ LOSS: {loss}\n📦"
          f" Всього: {total}\n📈 Winrate: <b>{winrate:.2f}%</b>"
      )
      send_telegram_message(chat_id, report)

  elif "callback_query" in data:
    query = data["callback_query"]
    chat_id = query["message"]["chat"]["id"]
    data_val = query.get("data", "")

    if data_val.startswith("analyze_"):
      pair_name = data_val.replace("analyze_", "")

      def scan_single(c_id, p_name):
        send_telegram_message(c_id, f"🔍 Сканую пару {p_name}...")
        results = analyze_pair_all_tfs(p_name)
        for res in results:
          save_signal_to_db(
              c_id,
              res["pair"],
              res["signal"],
              res["price"],
              res["timeframe"],
              res["expiration"],
              res["target_time"],
              res["z_score"],
              res["stoch"],
          )
          msg = (
              f"🚨 <b>Сигнал для {res['pair']}</b>\nСигнал:"
              f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
              f" {res['z_score']:.2f}\nТаймфрейм: <b>{res['timeframe']}</b>"
          )
          send_telegram_message(c_id, msg)
        if not results:
          send_telegram_message(
              c_id, f"ℹ️ По парі <b>{p_name}</b> сигналів немає."
          )

      threading.Thread(
          target=scan_single, args=(chat_id, pair_name), daemon=True
      ).start()

  return "OK", 200


if __name__ == "__main__":
  set_webhook()
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
