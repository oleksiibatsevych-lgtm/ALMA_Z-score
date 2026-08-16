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


# --- РОБОТА З БАЗОЮ ДАНИХ ---
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
  trade_id = cursor.lastrowid
  conn.commit()
  conn.close()
  return trade_id


def update_signal_status(trade_id, status):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "UPDATE trades SET result = ? WHERE id = ?", (status, trade_id)
  )
  conn.commit()
  conn.close()


def get_stats_report(days=None):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  query = 'SELECT result, COUNT(*) FROM trades WHERE result IN ("WIN", "LOSS")'
  params = []
  if days:
    date_limit = (datetime.now() - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    query += " AND timestamp >= ?"
    params.append(date_limit)
  query += " GROUP BY result"
  cursor.execute(query, params)
  data = dict(cursor.fetchall())
  conn.close()

  win = data.get("WIN", 0)
  loss = data.get("LOSS", 0)
  total = win + loss
  winrate = (win / total * 100) if total > 0 else 0
  return win, loss, total, winrate


# --- АВТОМАТИЧНИЙ ФОНОВЙ ПЕРЕВІРНИК УГОД ---
def automated_trade_checker():
  """Фоновий потік, який автоматично перевіряє завершені угоди без участі людини."""
  while True:
    try:
      time.sleep(30)  # Перевірка кожні 30 секунд
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

        # Завантажуємо актуальну ціну на момент перевірки
        df = yf.download(
            ticker, period="1d", interval="1m", progress=False, session=session
        )
        if df is None or df.empty:
          continue
        if isinstance(df.columns, pd.MultiIndex):
          df.columns = df.columns.get_level_values(0)

        current_price = float(df["Close"].iloc[-1])

        # Визначаємо результат автоматично
        outcome = "LOSS"
        if "BUY" in signal_type:
          if current_price > entry_price:
            outcome = "WIN"
        elif "SELL" in signal_type:
          if current_price < entry_price:
            outcome = "WIN"

        update_signal_status(trade_id, outcome)

        # Сповіщаємо користувача в Telegram
        icon = "✅ WIN" if outcome == "WIN" else "❌ LOSS"
        msg_text = (
            f"🏁 <b>Автоматичний результат угоди!</b>\nПара:"
            f" <b>{pair_name}</b>\nСигнал: <b>{signal_type}</b>\nЦіна входу:"
            f" {entry_price:.5f}\nЦіна виходу: {current_price:.5f}\nСтатус:"
            f" <b>{icon}</b>"
        )
        send_telegram_message(chat_id, msg_text)

    except Exception as e:
      print(f"Помилка у фоновому перевірнику: {e}")


# --- МАШИННЕ НАВЧАННЯ (ML FILTER) ---
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

    X_test = np.array([[curr_z, curr_stoch]])
    prediction = model.predict(X_test)[0]
    probability = model.predict_proba(X_test)[0][1]

    return prediction == 1 or probability >= 0.45
  except Exception as e:
    print(f"Помилка ML фільтра: {e}")
    return True


# --- ТЕЛЕГРАМ ФУНКЦІЇ ---
def send_telegram_message(chat_id, text, reply_markup=None):
  if not TELEGRAM_TOKEN:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
  if reply_markup:
    payload["reply_markup"] = reply_markup
  requests.post(url, json=payload)


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


# --- ЗАВАНТАЖЕННЯ ДАНИХ ---
def get_market_data(ticker, timeframe):
  try:
    if timeframe in ["1m", "3m", "10m"]:
      df = yf.download(
          ticker, period="5d", interval="1m", progress=False, session=session
      )
      if df.empty:
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
                "Volume": "sum",
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
                "Volume": "sum",
            })
            .dropna()
        )
      return df

    elif timeframe in ["5m", "15m", "30m"]:
      df = yf.download(
          ticker,
          period="60d",
          interval=timeframe,
          progress=False,
          session=session,
      )
      if df.empty:
        return None
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      return df

    elif timeframe in ["1h", "4h"]:
      df = yf.download(
          ticker, period="max", interval="1h", progress=False, session=session
      )
      if df.empty:
        return None
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

      if timeframe == "4h":
        df = (
            df.resample("4h")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            })
            .dropna()
        )
      return df
  except Exception as e:
    print(f"Помилка завантаження даних для {ticker}: {e}")
    return None
  return None


# --- СТРАТЕГІЯ + ШІ ФІЛЬТРАЦІЯ ---
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

    if not signal:
      return None

    if not predict_ml_filter(float(curr_z), float(curr_stoch)):
      return None

    multiplier = (
        3
        if "m" in timeframe
        else (2 if "h" in timeframe else 1)
    )
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

    # Розрахунок точного часу завершення угоди для автоматичної перевірки
    target_time_dt = datetime.now() + timedelta(minutes=dynamic_exp)
    target_time_str = target_time_dt.strftime("%Y-%m-%d %H:%M:%S")

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
    print(f"Помилка аналізу {pair_name} на {timeframe}: {e}")
    return None


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

    if text == "/start":
      send_telegram_message(
          chat_id,
          "👋 Бот активовано! Повністю <b>автоматична перевірка угод</b> без"
          " людського фактора + ШІ-фільтр.\nОберіть дію:",
          get_reply_keyboard(),
      )

    elif text == "💲 Пари":
      send_telegram_message(
          chat_id,
          "📌 Оберіть пару для сканування:",
          get_pairs_grid_keyboard(),
      )

    elif text == "📊 Аналіз усіх пар":
      send_telegram_message(
          chat_id,
          "⏳ Глобальне сканування ринку з автофіксацією результатів...",
      )
      signals_found = 0
      for pair_name in PAIRS_MAP.keys():
        for tf in ALL_TIMEFRAMES:
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
            msg_text = (
                f"🚨 <b>Авто-Сигнал (AI Filtered)!</b>\nПара:"
                f" <b>{res['pair']}</b>\nСигнал:"
                f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
                f" {res['z_score']:.2f}\nStochastic:"
                f" {res['stoch']:.1f}\n⏱ Таймфрейм: <b>{tf}</b>\n⏳ Експірація:"
                f" <b>{res['expiration']}</b>\nℹ️ <i>Результат буде"
                " перевірено автоматично!</i>"
            )
            send_telegram_message(chat_id, msg_text)

      if signals_found == 0:
        send_telegram_message(chat_id, "💤 Наразі немає якісних сигналів.")

    elif text == "📈 Статистика":
      win, loss, total, winrate = get_stats_report()
      report = (
          f"📊 <b>Статистика торгових сигналів:</b>\n\n✅ Перемог (WIN):"
          f" {win}\n❌ Поразок (LOSS): {loss}\n📦 Загалом угод:"
          f" {total}\n📈 Вінрейт (Winrate): <b>{winrate:.2f}%</b>"
      )
      send_telegram_message(chat_id, report)

  elif "callback_query" in update:
    cb = update["callback_query"]
    data = cb["data"]
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]

    if data.startswith("analyze_"):
      pair_name = data.replace("analyze_", "")
      send_telegram_message(
          chat_id, f"🔍 Сканую пару {pair_name} по всіх таймфреймах..."
      )
      signals_found = 0
      for tf in ALL_TIMEFRAMES:
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
          msg_text = (
              f"🚨 <b>Авто-Сигнал для {res['pair']}</b>\nСигнал:"
              f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
              f" {res['z_score']:.2f}\nStochastic:"
              f" {res['stoch']:.1f}\n⏱ Таймфрейм: <b>{tf}</b>\n⏳ Експірація:"
              f" <b>{res['expiration']}</b>\nℹ️ <i>Автоматичний контроль"
              " активний!</i>"
          )
          send_telegram_message(chat_id, msg_text)

      if signals_found == 0:
        send_telegram_message(
            chat_id, f"ℹ️ По парі <b>{pair_name}</b> сигналів немає."
        )

  return "OK", 200


if __name__ == "__main__":
  init_db()
  # Запуск фонового потоку для автоматичної перевірки угод
  checker_thread = threading.Thread(
      target=automated_trade_checker, daemon=True
  )
  checker_thread.start()

  app.run(host="0.0.0.0", port=10000)
