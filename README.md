# Market Bot: Stock Crash Alerts on Telegram

An automated bot that scans about **580 large-cap stocks** in the US and Europe every 15 minutes and sends a **Telegram alert** when a stock falls more than **15%** in a day, together with the latest news that may explain the move.

It runs entirely on **GitHub Actions**

---

## Example alert

Illustrative message (the alert text is in Italian; ticker and headlines are made up):

```
XYZ: -18.4%
Prezzo attuale: 42.51 (chiusura prec.: 52.10)

Possibili motivi (dalle ultime notizie):
- XYZ cuts full-year guidance after weak quarter (Reuters)
- Analysts downgrade XYZ on margin pressure (Bloomberg)
- XYZ CEO to step down (Yahoo Finance)
```

The message shows the daily change, current price vs previous close, and up to three recent headlines with their source.

---

## Coverage

| Index | Stocks | Source |
|---|---|---|
| S&P 500 | ~500 | Wikipedia, refreshed at every run |
| FTSE MIB (Italy) | 38 | Fixed list in the code |
| Euro Stoxx 50 (euro area) | 41 | Fixed list in the code |
| Nasdaq-100 | – | Currently not available (see Limitations) |

Duplicates are removed, and each ticker gets the Yahoo Finance exchange suffix it needs (`.MI` Milan, `.PA` Paris, `.DE` Frankfurt, …).

---

## How it works

```
GitHub Actions (every 15 min, Mon-Fri, 07:00-21:45 UTC)
        │
        ▼
1. DETECT   → download tickers → get price vs previous close → keep drops ≤ -15%
        │
        ▼
2. EXPLAIN  → fetch the latest 3 news headlines for each crashed stock
        │
        ▼
3. NOTIFY   → send a Telegram message → save who was alerted today
```

| Step | Function | What it does |
|---|---|---|
| Market check | `is_any_market_open()` | Exits immediately if no market is open (weekends, nights) |
| Tickers | `get_tickers()` | Builds the list of stocks to monitor |
| Scan | `scan_market()`, `get_price_change()` | Queries Yahoo Finance in parallel (10 requests at a time) and keeps stocks below the threshold |
| News | `get_reason()` | Retrieves the latest headlines for each crashed stock |
| Alert | `send_telegram_message()` | Sends the message through the Telegram Bot API |
| Memory | `state.json` | Stores which stocks were already alerted today, so the same alert is not sent every 15 minutes. Resets daily |

---

## Repository structure

```
├── check_crashes.py             # the whole bot
├── state.json                   # stocks already alerted today
├── requirements.txt             # yfinance, requests, pandas, lxml
└── .github/workflows/monitor.yml   # schedule and GitHub Actions setup
```

---

## Setup (about 10 minutes)

**1. Create a Telegram bot**
- In Telegram, open **@BotFather** and send `/newbot`.
- Choose a name and a username ending in `bot`.
- Copy the **token** (e.g. `123456789:AAE...`).
- Send any message to your new bot to activate the chat.

**2. Get your chat ID**
- Open **@userinfobot** in Telegram: it replies with your numeric ID.

**3. Add the secrets to GitHub**
- Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | ID from userinfobot |

Secrets are never visible in the code, so the repository can safely be public.

**4. Enable and test**
- Open the **Actions** tab and enable the workflow *Monitor stock crashes*.
- Click **Run workflow** to run it immediately.
- To check that Telegram works, temporarily set `DROP_THRESHOLD_PCT` to `1` in `monitor.yml`, run it, then set it back to `15`.

From then on the bot runs on its own.

---

## Settings

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `DROP_THRESHOLD_PCT` | `monitor.yml` | `15` | Daily fall (%) that triggers an alert |
| `cron` | `monitor.yml` | `*/15 7-21 * * 1-5` | How often the bot runs |
| `FTSE_MIB_TICKERS`, `EURO_STOXX50_TICKERS` | `check_crashes.py` | – | Fixed lists of European stocks |

---

## Limitations

- **Nasdaq-100 not available.** Its Wikipedia page does not return a readable table to automated requests. The impact is small, because almost all Nasdaq-100 companies are already in the S&P 500.
- **Fixed European lists.** FTSE MIB and Euro Stoxx 50 members are hard-coded and do not update automatically; they should be checked a couple of times a year.
- **News headlines, not explanations.** The "reason" is a list of recent headlines, not a written summary.
- **No holiday calendar.** On exchange holidays the bot still runs, but finds no crashes.
- **Unofficial data source.** `yfinance` relies on undocumented Yahoo Finance endpoints, which can occasionally change or slow down.

---

## Possible improvements

- **AI summary**: pass the headlines to a language model to explain the crash in two or three sentences
- **Custom watchlist** in addition to the indices
- **Multiple thresholds** (−10%, −15%, −20%) with different urgency
- **Exchange holiday calendar** with `pandas-market-calendars`
- **Wider coverage**: Stoxx Europe 600, using an ETF's holdings file as source
- **History and dashboard** of all alerts



---

## Tech stack

Python · yfinance · pandas · Telegram Bot API · GitHub Actions
