from datetime import datetime, timedelta
import os
import sqlite3
from flask import Flask, request
import pandas as pd
import requests
import yfinance as yf

app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_NAME = "bot_stats.db"

# Список усіх таймфреймів, які бот перевіряє одночасно
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
            pair TEXT,
            signal TEXT,
            price REAL,
            timeframe TEXT,
            expiration TEXT,
            result TEXT DEFAULT 'PENDING',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()
  conn.close()


def save_signal_to_db(pair, signal, price, timeframe, expiration):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "INSERT INTO trades (pair, signal, price, timeframe, expiration) VALUES"
      " (?, ?, ?, ?, ?)",
      (pair, signal, price, timeframe, expiration),
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


# --- ТЕЛЕГРАМ ФУНКЦІЇ ---
def send_telegram_message(chat_id, text, reply_markup=None):
  if not TELEGRAM_TOKEN:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
  if reply_markup:
    payload["reply_markup"] = reply_markup
  requests.post(url, json=payload)


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
  if not TELEGRAM_TOKEN:
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
  payload = {
      "chat_id": chat_id,
      "message_id": message_id,
      "text": text,
      "parse_mode": "HTML",
  }
  if reply_markup:
    payload["reply_markup"] = reply_markup
  requests.post(url, json=payload)


# Клавіатури інтерфейсу (без кнопки вибору таймфрейму, оскільки перевіряються всі одразу)
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


# --- ЗАВАНТАЖЕННЯ ДАНИХ ТА РЕСЕМПЛІНГ ПІД БУДЬ-ЯКИЙ ТАЙМФРЕЙМ ---
def get_market_data(ticker, timeframe):
  try:
    if timeframe in ["1m", "3m", "10m"]:
      df = yf.download(ticker, period="5d", interval="1m", progress=False)
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
      df = yf.download(ticker, period="60d", interval=timeframe, progress=False)
      if df.empty:
        return None
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      return df

    elif timeframe in ["1h", "4h"]:
      df = yf.download(ticker, period="max", interval="1h", progress=False)
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


# --- СТРАТЕГІЯ STOCHASTIC Z-SCORE ---
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

    # Динамічний час експірації залежно від таймфрейму
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

    return {
        "pair": pair_name,
        "signal": signal,
        "price": float(curr_price),
        "z_score": float(curr_z),
        "stoch": float(curr_stoch),
        "expiration": expiration_str,
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
          "👋 Бот активовано! Стратегія Stochastic Z-Score.\nБот"
          " <b>автоматично перевіряє всі таймфрейми одразу</b> (1m, 3m, 5m,"
          " 10m, 15m, 30m, 1h, 4h). Оберіть дію:",
          get_reply_keyboard(),
      )

    elif text == "💲 Пари":
      send_telegram_message(
          chat_id,
          "📌 Оберіть пару для миттєвого сканування по всіх таймфреймах:",
          get_pairs_grid_keyboard(),
      )

    elif text == "📊 Аналіз усіх пар":
      send_telegram_message(
          chat_id,
          "⏳ Повне глобальне сканування всіх пар та всіх таймфреймів...",
      )
      signals_found = 0
      for pair_name in PAIRS_MAP.keys():
        for tf in ALL_TIMEFRAMES:
          res = analyze_pair(pair_name, tf)
          if res and res["signal"]:
            signals_found += 1
            trade_id = save_signal_to_db(
                res["pair"],
                res["signal"],
                res["price"],
                tf,
                res["expiration"],
            )
            markup = {
                "inline_keyboard": [[
                    {"text": "✅ WIN", "callback_data": f"res_{trade_id}_WIN"},
                    {
                        "text": "❌ LOSS",
                        "callback_data": f"res_{trade_id}_LOSS",
                    },
                ]]
            }
            msg_text = (
                f"🚨 <b>Сигнал (Stochastic Z-Score)!</b>\nПара:"
                f" <b>{res['pair']}</b>\nСигнал:"
                f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
                f" {res['z_score']:.2f}\nStochastic:"
                f" {res['stoch']:.1f}\n⏱ Таймфрейм: <b>{tf}</b>\n⏳ Експірація:"
                f" <b>{res['expiration']}</b>"
            )
            send_telegram_message(chat_id, msg_text, markup)

      if signals_found == 0:
        send_telegram_message(
            chat_id,
            "💤 На жодному з таймфреймів екстремальних точок входів наразі"
            " немає.",
        )

    elif text == "📈 Статистика":
      win, loss, total, winrate = get_stats_report()
      report = (
          f"📊 <b>Статистика торгових сигналів:</b>\n\n✅ Перемог (WIN):"
          f" {win}\n❌ Поразок (LOSS): {loss}\n📦 Загалом угод:"
          f" {total}\n📈 Вінрейт (Winrate): <b>{winrate:.2f}%</b>"
      )
      send_telegram_message(
          chat_id,
          report,
          reply_markup={
              "inline_keyboard": [
                  [
                      {"text": "📅 За добу", "callback_data": "stats_day"},
                      {"text": "📆 За тиждень", "callback_data": "stats_week"},
                  ],
                  [{"text": "📈 За весь час", "callback_data": "stats_all"}],
              ]
          },
      )

  elif "callback_query" in update:
    cb = update["callback_query"]
    data = cb["data"]
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]

    if data.startswith("res_"):
      _, trade_id, outcome = data.split("_")
      update_signal_status(trade_id, outcome)
      new_text = cb["message"]["text"] + f"\n\n<b>Статус: {outcome} ✅</b>"
      edit_telegram_message(chat_id, message_id, new_text, reply_markup=None)

    elif data.startswith("analyze_"):
      pair_name = data.replace("analyze_", "")
      send_telegram_message(
          chat_id, f"🔍 Сканую всі таймфрейми для пари {pair_name}..."
      )
      signals_found = 0
      for tf in ALL_TIMEFRAMES:
        res = analyze_pair(pair_name, tf)
        if res and res["signal"]:
          signals_found += 1
          trade_id = save_signal_to_db(
              res["pair"],
              res["signal"],
              res["price"],
              tf,
              res["expiration"],
          )
          markup = {
              "inline_keyboard": [[
                  {"text": "✅ WIN", "callback_data": f"res_{trade_id}_WIN"},
                  {
                      "text": "❌ LOSS",
                      "callback_data": f"res_{trade_id}_LOSS",
                  },
              ]]
          }
          msg_text = (
              f"🚨 <b>Сигнал для {res['pair']}</b>\nСигнал:"
              f" <b>{res['signal']}</b>\nЦіна: {res['price']:.5f}\nZ-Score:"
              f" {res['z_score']:.2f}\nStochastic:"
              f" {res['stoch']:.1f}\n⏱ Таймфрейм: <b>{tf}</b>\n⏳ Експірація:"
              f" <b>{res['expiration']}</b>"
          )
          send_telegram_message(chat_id, msg_text, markup)

      if signals_found == 0:
        send_telegram_message(
            chat_id,
            f"ℹ️ По парі <b>{pair_name}</b> на жодному з таймфреймів немає"
            " екстремуму.",
        )

    elif data.startswith("stats_"):
      days = None
      period_name = "за весь час"
      if data == "stats_day":
        days = 1
        period_name = "за добу"
      elif data == "stats_week":
        days = 7
        period_name = "за тиждень"

      win, loss, total, winrate = get_stats_report(days)
      report = (
          f"📊 <b>Статистика ({period_name}):</b>\n\n✅ Перемог:"
          f" {win}\n❌ Поразок:"
          f" {loss}\n📦 Угод: {total}\n📈 Winrate: <b>{winrate:.2f}%</b>"
      )
      edit_telegram_message(chat_id, message_id, report)

  return "OK", 200


if __name__ == "__main__":
  init_db()
  app.run(host="0.0.0.0", port=10000)
