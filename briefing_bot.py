#!/usr/bin/env python3
"""
Crypto, Gold & Stocks Briefing Bot
-----------------------------------
Sends a brief SMC-lite market update (trend bias + nearest key levels) for
BTC, ETH, Gold futures, and a watchlist of stocks, plus a short news section
tagging which watched assets each headline could affect. Sent to Telegram.
Designed to be run 3x/day (morning, noon, evening) by a scheduler such as
GitHub Actions.

Data sources:
  - BTC/ETH prices: OKX public market API (no API key required)
  - Gold futures (GC=F) & stock prices: Yahoo Finance via yfinance
  - News headlines: Yahoo Finance per-ticker news via yfinance

Delivery:
  - Telegram Bot API (sendMessage)

Env vars required:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID
Optional:
  - BRIEFING_TZ_LABEL (default "Taipei") -- just a label shown in the message
"""

import os
import sys
import datetime
import requests
import numpy as np
import pandas as pd

# ---------------- Config ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TIMEZONE_LABEL = os.environ.get("BRIEFING_TZ_LABEL", "Taipei")

OKX_BASE = "https://www.okx.com"
EMA_FAST = 20
EMA_SLOW = 50
SWING_LOOKBACK = 3   # bars on each side required to confirm a pivot
SWING_WINDOW = 60    # how many recent daily candles to scan for swing levels

CRYPTO_INSTRUMENTS = {
    "BTC/USDT": "BTC-USDT",
    "ETH/USDT": "ETH-USDT",
}
GOLD_TICKER = "GC=F"

# Stocks to include alongside crypto/gold. Add/remove tickers here freely --
# they get the same EMA trend-bias + key-level treatment as gold.
STOCK_TICKERS = {
    "NVDA": "NVDA",
    "MRVL": "MRVL",
}

# Which Yahoo Finance ticker to pull NEWS from for each label. BTC/ETH prices
# come from OKX, but OKX has no news endpoint, so news for those two still
# goes through Yahoo Finance's BTC-USD / ETH-USD tickers.
NEWS_TICKERS = {
    "BTC/USDT": "BTC-USD",
    "ETH/USDT": "ETH-USD",
    "Gold Futures (GC=F)": "GC=F",
    **{label: ticker for label, ticker in STOCK_TICKERS.items()},
}

MAX_NEWS_PER_TICKER = 3     # headlines pulled per ticker before dedup/ranking
MAX_NEWS_IN_BRIEFING = 4    # headlines actually shown in the message

# Lightweight keyword tags used to flag "this news could also affect X".
# Not sentiment analysis -- just a fast, transparent heuristic.
MACRO_KEYWORDS = [
    "fed", "fomc", "interest rate", "rate cut", "rate hike", "inflation",
    "cpi", "jerome powell", "recession", "tariff", "yields",
]
MACRO_AFFECTS = ["BTC/USDT", "ETH/USDT", "Gold Futures (GC=F)"]

SEMI_KEYWORDS = ["chip", "semiconductor", "gpu", "ai chip", "nvidia", "marvell"]
SEMI_AFFECTS = [label for label in STOCK_TICKERS]  # e.g. NVDA, MRVL


# ---------------- Data fetching ----------------
def fetch_okx_candles(inst_id, bar="1D", limit=100):
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX candles error for {inst_id}: {data}")
    rows = data["data"][::-1]  # OKX returns newest-first; flip to oldest->newest
    df = pd.DataFrame(
        rows,
        columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"],
    )
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms")
    return df


def fetch_okx_ticker(inst_id):
    url = f"{OKX_BASE}/api/v5/market/ticker"
    params = {"instId": inst_id}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX ticker error for {inst_id}: {data}")
    t = data["data"][0]
    last = float(t["last"])
    open24h = float(t["open24h"])
    change_pct = (last - open24h) / open24h * 100 if open24h else 0.0
    return last, change_pct


def fetch_yf_history(ticker, period="6mo", interval="1d"):
    """Generic daily OHLC fetch via yfinance. Used for gold futures and stocks."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df.empty:
        raise RuntimeError(f"No price data returned from Yahoo Finance for {ticker}")
    df = df.rename(columns=str.lower).reset_index()
    return df


def _parse_news_item(item):
    """
    Defensively parse one yfinance news item. Yahoo has changed this schema
    more than once: newer responses nest fields under 'content', older ones
    are flat. Handle both; return None if neither shape matches.
    """
    content = item.get("content") if isinstance(item, dict) else None
    if isinstance(content, dict):
        title = content.get("title")
        publisher = (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else content.get("publisher")
        link = (content.get("clickThroughUrl") or {}).get("url") if isinstance(content.get("clickThroughUrl"), dict) else content.get("link")
        published = content.get("pubDate") or content.get("displayTime")
    else:
        title = item.get("title")
        publisher = item.get("publisher")
        link = item.get("link")
        published = item.get("providerPublishTime")

    if not title or not link:
        return None
    return {"title": title.strip(), "publisher": publisher or "Unknown", "link": link, "published": published}


def fetch_yf_news(ticker, limit=MAX_NEWS_PER_TICKER):
    """Fetch recent headlines for a ticker via yfinance. Never raises -- news
    is a nice-to-have; a failure here should not break the price briefing."""
    import yfinance as yf
    try:
        raw_items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    parsed = []
    for item in raw_items[:limit]:
        p = _parse_news_item(item)
        if p:
            parsed.append(p)
    return parsed


# ---------------- News gathering & impact tagging ----------------
def _normalize_title(title):
    return " ".join(title.lower().split())


def _keyword_hits(text, keywords):
    text_l = text.lower()
    return any(kw in text_l for kw in keywords)


def gather_news_bundle():
    """
    Pull recent headlines per watched asset, dedup identical headlines that
    surface under multiple tickers, and tag each with which watched assets
    it's likely relevant to:
      - always tagged with whichever ticker(s) it was fetched under
      - additionally tagged via keyword heuristics (macro -> crypto+gold,
        semiconductor terms -> the stock watchlist)
    Returns a list of dicts sorted with the most "broadly relevant" first.
    """
    bundle = {}  # normalized title -> {title, publisher, link, published, affects:set}

    for label, ticker in NEWS_TICKERS.items():
        for item in fetch_yf_news(ticker):
            key = _normalize_title(item["title"])
            if key not in bundle:
                bundle[key] = {**item, "affects": set()}
            bundle[key]["affects"].add(label)

    for entry in bundle.values():
        title = entry["title"]
        if _keyword_hits(title, MACRO_KEYWORDS):
            entry["affects"].update(MACRO_AFFECTS)
        if _keyword_hits(title, SEMI_KEYWORDS):
            entry["affects"].update(SEMI_AFFECTS)

    items = list(bundle.values())
    # Most broadly-relevant (affects the most watched assets) first; ties
    # broken by recency when a publish timestamp is available.
    items.sort(key=lambda e: (-len(e["affects"]), -(e["published"] or 0) if isinstance(e["published"], (int, float)) else 0))
    return items[:MAX_NEWS_IN_BRIEFING]


# ---------------- Analysis (SMC-lite) ----------------
def compute_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def find_swings(df, lookback=SWING_LOOKBACK, window=SWING_WINDOW):
    """Simple fractal pivot high/low detection over the most recent `window` bars."""
    d = df.tail(window).reset_index(drop=True)
    highs, lows = [], []
    for i in range(lookback, len(d) - lookback):
        seg_h = d["high"].iloc[i - lookback: i + lookback + 1]
        seg_l = d["low"].iloc[i - lookback: i + lookback + 1]
        if d["high"].iloc[i] == seg_h.max():
            highs.append(d["high"].iloc[i])
        if d["low"].iloc[i] == seg_l.min():
            lows.append(d["low"].iloc[i])
    return highs, lows


def nearest_levels(price, highs, lows):
    above = [h for h in highs if h > price]
    below = [l for l in lows if l < price]
    resistance = min(above) if above else None
    support = max(below) if below else None
    return resistance, support


def trend_bias(price, ema_fast_val, ema_slow_val):
    if price > ema_fast_val > ema_slow_val:
        return "Bullish", "price > EMA20 > EMA50"
    if price < ema_fast_val < ema_slow_val:
        return "Bearish", "price < EMA20 < EMA50"
    return "Neutral/Mixed", "price between EMAs"


def analyze_ohlc(df, label, live_price=None, live_change=None):
    df = df.copy()
    df["ema_fast"] = compute_ema(df["close"], EMA_FAST)
    df["ema_slow"] = compute_ema(df["close"], EMA_SLOW)

    price = live_price if live_price is not None else float(df["close"].iloc[-1])
    if live_change is not None:
        change_pct = live_change
    else:
        prev_close = float(df["close"].iloc[-2])
        change_pct = (price - prev_close) / prev_close * 100

    ema_fast_val = float(df["ema_fast"].iloc[-1])
    ema_slow_val = float(df["ema_slow"].iloc[-1])
    bias_label, bias_reason = trend_bias(price, ema_fast_val, ema_slow_val)

    highs, lows = find_swings(df)
    resistance, support = nearest_levels(price, highs, lows)

    return {
        "label": label,
        "price": price,
        "change_pct": change_pct,
        "bias_label": bias_label,
        "bias_reason": bias_reason,
        "resistance": resistance,
        "support": support,
    }


# ---------------- Formatting & delivery ----------------
def fmt_price(p):
    if p is None:
        return "n/a"
    return f"${p:,.0f}" if p >= 1000 else f"${p:,.2f}"


BIAS_EMOJI = {"Bullish": "\U0001F7E2", "Bearish": "\U0001F534", "Neutral/Mixed": "\U0001F7E1"}


def build_message(results, news_items, session_label):
    now = datetime.datetime.now()
    lines = [f"*{session_label} Briefing - {now.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE_LABEL})*", ""]
    for r in results:
        sign = "+" if r["change_pct"] >= 0 else ""
        emoji = BIAS_EMOJI.get(r["bias_label"], "")
        lines.append(f"*{r['label']}*: {fmt_price(r['price'])} ({sign}{r['change_pct']:.1f}% 24h)")
        lines.append(f"Trend: {emoji} {r['bias_label']} ({r['bias_reason']})")
        res = fmt_price(r["resistance"]) if r["resistance"] else "none nearby"
        sup = fmt_price(r["support"]) if r["support"] else "none nearby"
        lines.append(f"Key levels: Resistance ~{res} | Support ~{sup}")
        lines.append("")

    if news_items:
        lines.append("*News & Potential Impact*")
        for n in news_items:
            affects = ", ".join(sorted(n["affects"])) if n["affects"] else "unclear"
            lines.append(f"- {n['title']} ({n['publisher']})")
            lines.append(f"  Affects: {affects}")
            lines.append(f"  {n['link']}")
        lines.append("")

    return "\n".join(lines).strip()


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------- Main ----------------
def main():
    session_label = sys.argv[1] if len(sys.argv) > 1 else "Market"
    results = []

    for label, inst_id in CRYPTO_INSTRUMENTS.items():
        df = fetch_okx_candles(inst_id)
        price, change_pct = fetch_okx_ticker(inst_id)
        results.append(analyze_ohlc(df, label, live_price=price, live_change=change_pct))

    gold_df = fetch_yf_history(GOLD_TICKER)
    results.append(analyze_ohlc(gold_df, "Gold Futures (GC=F)"))

    for label, ticker in STOCK_TICKERS.items():
        stock_df = fetch_yf_history(ticker)
        results.append(analyze_ohlc(stock_df, label))

    try:
        news_items = gather_news_bundle()
    except Exception as e:
        print(f"News gathering failed, continuing without it: {e}")
        news_items = []

    message = build_message(results, news_items, session_label)
    print(message)  # also lands in the GitHub Actions log for debugging
    send_telegram(message)


if __name__ == "__main__":
    main()