# Crypto, Gold & Stocks Briefing Bot

Sends a brief SMC-lite market update (price, 24h change, EMA trend bias, and
nearest swing-level support/resistance) for **BTC/USDT**, **ETH/USDT**,
**Gold futures (GC=F)**, and a **stock watchlist (NVDA, MRVL by default)**
to Telegram — three times a day, on autopilot. It also pulls a short news
section tagging which of your watched assets each headline could affect.

- **Crypto data:** OKX public market API (no API key needed)
- **Gold & stock data:** Yahoo Finance (`yfinance`)
- **News:** Yahoo Finance per-ticker headlines (`yfinance`), deduped and
  tagged with which watched assets they're relevant to
- **Delivery:** Telegram Bot API
- **Scheduler:** GitHub Actions (free, no server required)

## What the message looks like

```
Morning Briefing - 2026-07-03 08:00 (Taipei)

BTC/USDT: $109,432 (+1.2% 24h)
Trend: 🟢 Bullish (price > EMA20 > EMA50)
Key levels: Resistance ~$112,800 | Support ~$106,150

ETH/USDT: $3,845 (-0.4% 24h)
Trend: 🟡 Neutral/Mixed (price between EMAs)
Key levels: Resistance ~$3,980 | Support ~$3,690

Gold Futures (GC=F): $2,415 (+0.3% 24h)
Trend: 🟢 Bullish (price > EMA20 > EMA50)
Key levels: Resistance ~$2,432 | Support ~$2,388

NVDA: $137 (+0.4% 24h)
Trend: 🔴 Bearish (price < EMA20 < EMA50)
Key levels: Resistance ~$142 | Support ~$134

MRVL: $74 (+1.3% 24h)
Trend: 🟡 Neutral/Mixed (price between EMAs)
Key levels: Resistance ~$75 | Support ~$73

News & Potential Impact
- Fed signals possible rate cut amid cooling inflation (Reuters)
  Affects: BTC/USDT, ETH/USDT, Gold Futures (GC=F)
  https://example.com/fed
- NVIDIA unveils new AI chip lineup (Bloomberg)
  Affects: MRVL, NVDA
  https://example.com/nvda
```

Trend bias and key levels are intentionally simple: EMA20/EMA50 for bias
(mirrors the top-down bias logic in your Pine strategy), and recent daily
swing highs/lows as stand-ins for liquidity levels. This is meant as a quick
pulse-check, not a signal generator — your Pine Script strategy remains the
source of truth for actual entries. The "Affects" tags on news are a
transparent keyword heuristic, not sentiment analysis: a headline is tagged
with whichever tickers it was fetched under, plus a couple of hand-picked
keyword rules (Fed/rate/inflation language → crypto + gold; chip/semiconductor
language → your stock watchlist). It's meant to save you a glance, not to
make a call for you.

## Customizing the watchlist

Stocks live in `STOCK_TICKERS` near the top of `briefing_bot.py`:

```python
STOCK_TICKERS = {
    "NVDA": "NVDA",
    "MRVL": "MRVL",
}
```

Add any Yahoo Finance ticker as a new entry — it automatically gets price,
EMA trend bias, key levels, and news coverage with no other code changes.
If you add tickers from a different sector, consider adding a matching
keyword list (like `SEMI_KEYWORDS`) so relevant macro/sector news gets
tagged correctly for them too.

## Setup (about 10 minutes)

### 1. Create a Telegram bot
1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts, and copy the **bot token** it gives you.

### 2. Get your chat ID
1. Send any message to your new bot (search its username and say "hi").
2. In a browser, visit:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find `"chat":{"id": ...}` in the JSON — that number is your chat ID.
   (If you get an empty response, make sure you messaged the bot first.)

### 3. Create a GitHub repo for this bot
1. Create a new **private** GitHub repository.
2. Upload these files, keeping the folder structure:
   ```
   briefing_bot.py
   requirements.txt
   .github/workflows/market_briefing.yml
   README.md
   ```

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` = the token from step 1
- `TELEGRAM_CHAT_ID` = the chat ID from step 2

### 5. Test it manually
Go to the **Actions** tab → **Market Briefing Bot** → **Run workflow**
(this uses the `workflow_dispatch` trigger). Check Telegram for the message
and check the workflow log if anything fails.

### 6. Let it run on schedule
Once the manual test works, it will fire automatically at:
- 08:00 Taipei (Morning)
- 12:00 Taipei (Noon)
- 20:00 Taipei (Evening)

## Adjusting the schedule or timezone

Edit the `cron` lines in `.github/workflows/market_briefing.yml`. GitHub
Actions cron times are always **UTC**. Taipei is UTC+8, so subtract 8 hours
from your desired local time to get the UTC cron hour. If you're ever in a
different timezone, recompute the offset and update the three cron lines
(and the `BRIEFING_TZ_LABEL` env var, which is just a display label).

## Known limitations to be aware of

- **GitHub Actions scheduling isn't second-precise.** Under heavy platform
  load, scheduled runs can be delayed by a few to ~15 minutes. Fine for a
  briefing, not fine if you need exact timing.
- **Inactive repos get schedules paused.** GitHub disables scheduled
  workflows in a repo after 60 days with no commits. A trivial commit (or
  just re-enabling it in the Actions tab) restarts it — worth a periodic
  check.
- **Yahoo Finance (`yfinance`) is unofficial** and occasionally changes
  behavior or rate-limits. If the gold section starts failing, that's the
  first place to check; a fallback data source (e.g., a paid futures data
  API) can be swapped in later if needed.
- **Swing levels are a simplification.** The fractal pivot detection here
  is a lightweight stand-in for real liquidity pools/order blocks — good
  for a quick glance, not a replacement for your full SMC confluence model.
- **News fetching adds more Yahoo Finance calls per run** (one per watched
  asset, on top of the price/history calls). The script treats news as
  best-effort: if Yahoo's news endpoint fails, changes shape again, or
  rate-limits, the price/trend sections still send — you just get a
  briefing without the news block that run (check the Actions log to see
  why). Yahoo's per-ticker news has also been known to occasionally surface
  a headline unrelated to the ticker it was requested for; the "Affects"
  tag reflects which feed it came from, which is usually but not always
  accurate.
- **Stock prices reflect the last daily close**, same as gold. If the bot
  runs outside NVDA/MRVL's market hours, the price shown is from the most
  recent close, not a live intraday quote.

## Extending it later

- Swap in OKX's authenticated endpoints if you want account balance or open
  position info in the briefing.
- Add FVG/order-block detection ported from your Pine logic for closer
  parity with your live strategy.
- Add a 4th "urgent" trigger — e.g., only message you outside the normal
  schedule if BOS or a liquidity sweep fires intraday.