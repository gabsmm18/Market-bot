#!/usr/bin/env python3
"""
check_crashes.py
=================

Script che:
1. Scarica l'elenco dei titoli S&P 500 + Nasdaq-100
2. Calcola la variazione percentuale odierna (prezzo attuale vs chiusura precedente)
3. Individua i titoli scesi di piu' della soglia (default 15%)
4. Per ogni crollo trovato, recupera le ultime notizie collegate al titolo
5. Manda un messaggio Telegram con ticker, variazione % e possibili motivi
6. Tiene traccia di cosa e' gia' stato notificato oggi, per non spammare

Pensato per girare periodicamente (es. ogni 15-30 minuti durante l'orario
di mercato USA) tramite GitHub Actions -- vedi .github/workflows/monitor.yml
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

# Soglia di crollo che fa scattare la notifica (in percentuale, valore positivo)
DROP_THRESHOLD_PCT = float(os.environ.get("DROP_THRESHOLD_PCT", "15"))

# File dove salviamo quali ticker abbiamo gia' notificato "oggi", per non
# mandare lo stesso alert ogni 15 minuti finche' il titolo resta sotto soglia.
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Credenziali Telegram, lette dalle variabili d'ambiente (mai scritte nel codice)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Numero di richieste in parallelo verso Yahoo Finance. Troppo alto puo'
# causare rate-limiting (errori temporanei); 10 e' un buon compromesso.
MAX_WORKERS = 10


# ---------------------------------------------------------------------------
# 1. Universo dei titoli da monitorare
# ---------------------------------------------------------------------------

def get_tickers() -> list[str]:
    """Scarica la lista dei componenti di S&P 500 e Nasdaq-100 da Wikipedia
    e restituisce l'unione dei due elenchi (senza duplicati).

    Usiamo Wikipedia perche' e' pubblica, gratuita e aggiornata a ogni
    modifica degli indici. In alternativa si potrebbe usare un file CSV
    statico da aggiornare a mano.
    """
    tickers = set()

    try:
        sp500_tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )
        sp500 = sp500_tables[0]["Symbol"].tolist()
        tickers.update(sp500)
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista S&P 500: {e}")

    try:
        nasdaq_tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        # La tabella con i componenti e' quella che contiene la colonna "Ticker"
        for table in nasdaq_tables:
            if "Ticker" in table.columns:
                tickers.update(table["Ticker"].tolist())
                break
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista Nasdaq-100: {e}")

    # Yahoo Finance vuole i ticker con "-" al posto di "." (es. BRK.B -> BRK-B)
    cleaned = sorted({str(t).replace(".", "-").strip() for t in tickers if isinstance(t, str)})
    print(f"[INFO] Universo monitorato: {len(cleaned)} titoli")
    return cleaned


# ---------------------------------------------------------------------------
# 2. Controllo se il mercato USA e' aperto
# ---------------------------------------------------------------------------

def is_market_open(now: datetime | None = None) -> bool:
    """Controllo semplice sugli orari del NYSE/Nasdaq (9:30-16:00 ora di
    New York, dal lunedi' al venerdi'). NON tiene conto dei giorni festivi
    di borsa (es. Natale, Independence Day): in quei giorni lo script gira
    comunque ma non trovera' variazioni di prezzo significative.
    """
    ny_tz = ZoneInfo("America/New_York")
    now = (now or datetime.now(ny_tz)).astimezone(ny_tz)

    if now.weekday() >= 5:  # 5=sabato, 6=domenica
        return False

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


# ---------------------------------------------------------------------------
# 3. Calcolo variazione percentuale per ogni titolo
# ---------------------------------------------------------------------------

def get_price_change(ticker: str) -> dict | None:
    """Restituisce {ticker, last_price, prev_close, pct_change} oppure None
    se non e' stato possibile recuperare i dati.
    """
    try:
        info = yf.Ticker(ticker).fast_info
        last_price = info.get("lastPrice") or info.get("last_price")
        prev_close = info.get("previousClose") or info.get("previous_close")
        if not last_price or not prev_close:
            return None
        pct_change = (last_price - prev_close) / prev_close * 100
        return {
            "ticker": ticker,
            "last_price": last_price,
            "prev_close": prev_close,
            "pct_change": pct_change,
        }
    except Exception:
        return None


def scan_market(tickers: list[str]) -> list[dict]:
    """Interroga Yahoo Finance in parallelo per tutti i ticker e restituisce
    solo quelli scesi oltre la soglia configurata.
    """
    crashes = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(get_price_change, t): t for t in tickers}
        for future in as_completed(futures):
            result = future.result()
            if result and result["pct_change"] <= -DROP_THRESHOLD_PCT:
                crashes.append(result)

    crashes.sort(key=lambda r: r["pct_change"])  # dal crollo peggiore
    return crashes


# ---------------------------------------------------------------------------
# 4. Recupero del possibile motivo del crollo
# ---------------------------------------------------------------------------

def get_reason(ticker: str, max_headlines: int = 3) -> str:
    """Recupera le ultime notizie associate al titolo da Yahoo Finance e le
    riassume come elenco di titoli (headline). Questa e' una versione "v1":
    non usa un modello AI per sintetizzare, mostra direttamente i titoli
    delle notizie piu' recenti, che di solito bastano per capire il motivo.

    Per una spiegazione discorsiva generata da un LLM (es. Claude), vedi la
    sezione "Possibili miglioramenti" nel README.
    """
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        news = []

    if not news:
        return "Nessuna notizia recente trovata: potrebbe essere un movimento tecnico o di mercato generale."

    lines = []
    for item in news[:max_headlines]:
        title = item.get("title") or item.get("content", {}).get("title")
        publisher = item.get("publisher") or item.get("content", {}).get("provider", {}).get("displayName", "")
        if title:
            lines.append(f"- {title} ({publisher})" if publisher else f"- {title}")

    return "\n".join(lines) if lines else "Notizie trovate ma senza titolo leggibile."


# ---------------------------------------------------------------------------
# 5. Notifica Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID non impostati, salto l'invio.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[ERROR] Invio Telegram fallito: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# 6. Stato: quali ticker sono gia' stati notificati oggi
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not is_market_open():
        print("[INFO] Mercato USA chiuso in questo momento, non faccio nulla.")
        return

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    state = load_state()
    already_notified = set(state.get(today, []))

    tickers = get_tickers()
    if not tickers:
        print("[ERROR] Nessun ticker recuperato, interrompo.")
        sys.exit(1)

    crashes = scan_market(tickers)
    print(f"[INFO] Trovati {len(crashes)} titoli sotto -{DROP_THRESHOLD_PCT}% oggi")

    new_crashes = [c for c in crashes if c["ticker"] not in already_notified]

    for crash in new_crashes:
        reason = get_reason(crash["ticker"])
        message = (
            f"*{crash['ticker']}*: {crash['pct_change']:.1f}%\n"
            f"Prezzo attuale: {crash['last_price']:.2f} (chiusura prec.: {crash['prev_close']:.2f})\n\n"
            f"Possibili motivi (dalle ultime notizie):\n{reason}"
        )
        print(f"[ALERT] {crash['ticker']}: {crash['pct_change']:.1f}%")
        send_telegram_message(message)
        already_notified.add(crash["ticker"])

    state[today] = sorted(already_notified)
    # Puliamo lo stato dei giorni precedenti per non far crescere il file all'infinito
    state = {today: state[today]}
    save_state(state)


if __name__ == "__main__":
    main()
