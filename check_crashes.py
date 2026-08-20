#!/usr/bin/env python3
"""
check_crashes.py
=================

Script che:
1. Scarica l'elenco dei titoli monitorati: S&P 500, Nasdaq-100, FTSE MIB
   (Borsa Italiana) ed EURO STOXX 50 (le 50 maggiori aziende dell'Eurozona)
2. Calcola la variazione percentuale odierna (prezzo attuale vs chiusura precedente)
3. Individua i titoli scesi di piu' della soglia (default 15%)
4. Per ogni crollo trovato, recupera le ultime notizie collegate al titolo
5. Manda un messaggio Telegram con ticker, variazione % e possibili motivi
6. Tiene traccia di cosa e' gia' stato notificato oggi, per non spammare

Pensato per girare periodicamente (es. ogni 15 minuti, dall'apertura dei
mercati europei alla chiusura di quello USA) tramite GitHub Actions -- vedi
.github/workflows/monitor.yml
"""

import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# Wikipedia rifiuta le richieste che non sembrano provenire da un browser
# (risponde 403 Forbidden alle richieste "anonime" tipiche di script/server,
# come quelle che partono dai runner di GitHub Actions). Impostando uno
# User-Agent che imita un browser reale, Wikipedia ci lascia passare.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

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

# Suffissi che Yahoo Finance richiede per riconoscere i titoli quotati sulle
# borse europee (l'elenco copre i paesi presenti nell'EURO STOXX 50).
COUNTRY_TO_YAHOO_SUFFIX = {
    "france": ".PA",
    "germany": ".DE",
    "italy": ".MI",
    "spain": ".MC",
    "netherlands": ".AS",
    "belgium": ".BR",
    "finland": ".HE",
    "ireland": ".IR",
    "luxembourg": ".LU",
    "portugal": ".LS",
    "austria": ".VI",
}

EUROPEAN_SUFFIXES = tuple(COUNTRY_TO_YAHOO_SUFFIX.values())


# ---------------------------------------------------------------------------
# 1. Universo dei titoli da monitorare
# ---------------------------------------------------------------------------

def _fetch_wikipedia_tables(url: str) -> list:
    """Scarica una pagina Wikipedia (con header da browser per evitare il
    403 Forbidden) e restituisce tutte le tabelle HTML trovate come
    DataFrame pandas.
    """
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _find_table_with_column(tables: list, keywords: list[str]):
    """Cerca, tra le tabelle passate, la prima che ha una colonna il cui nome
    contiene una delle parole chiave indicate (case-insensitive).
    Restituisce (tabella, nome_colonna) oppure (None, None) se non trovata.
    """
    for table in tables:
        for col in table.columns:
            col_name = str(col).lower()
            if any(kw in col_name for kw in keywords):
                return table, col
    return None, None


def get_sp500_tickers() -> list[str]:
    try:
        tables = _fetch_wikipedia_tables(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )
        table, col = _find_table_with_column(tables, ["symbol"])
        if table is None:
            raise ValueError("colonna 'Symbol' non trovata nella pagina")
        return [str(t).strip() for t in table[col].tolist()]
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista S&P 500: {e}")
        return []


def get_nasdaq100_tickers() -> list[str]:
    try:
        tables = _fetch_wikipedia_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
        table, col = _find_table_with_column(tables, ["ticker"])
        if table is None:
            raise ValueError("colonna 'Ticker' non trovata nella pagina")
        return [str(t).strip() for t in table[col].tolist()]
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista Nasdaq-100: {e}")
        return []


def get_ftsemib_tickers() -> list[str]:
    """Componenti del FTSE MIB (Borsa Italiana, circa 40 titoli). Yahoo
    Finance richiede il suffisso '.MI' per i titoli quotati a Milano.
    """
    try:
        tables = _fetch_wikipedia_tables("https://en.wikipedia.org/wiki/FTSE_MIB")
        table, col = _find_table_with_column(tables, ["ticker", "symbol"])
        if table is None:
            raise ValueError("colonna ticker/symbol non trovata nella pagina")
        return [
            f"{str(t).strip()}.MI"
            for t in table[col].tolist()
            if str(t).strip() and str(t).strip().lower() != "nan"
        ]
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista FTSE MIB: {e}")
        return []


def get_eurostoxx50_tickers() -> list[str]:
    """Componenti dell'EURO STOXX 50 (le 50 maggiori aziende dell'Eurozona).
    A differenza di S&P 500, Nasdaq-100 e FTSE MIB, qui i titoli sono
    quotati su borse diverse a seconda del paese dell'azienda, quindi
    serve anche la colonna "Country" della tabella per scegliere il
    suffisso Yahoo Finance corretto (es. '.PA' per Parigi, '.DE' per
    Francoforte, '.MI' per Milano) tramite COUNTRY_TO_YAHOO_SUFFIX.

    Nota: la struttura della pagina Wikipedia di un indice come questo puo'
    cambiare nel tempo; se in futuro questa funzione smette di trovare
    titoli, il modo per sistemarla e' aprire la pagina Wikipedia e vedere
    come si chiamano ora le colonne della tabella dei componenti.
    """
    try:
        tables = _fetch_wikipedia_tables("https://en.wikipedia.org/wiki/EURO_STOXX_50")
        target = ticker_col = country_col = None
        for table in tables:
            cols_lower = {str(c): str(c).lower() for c in table.columns}
            t_col = next((c for c, lo in cols_lower.items() if "ticker" in lo), None)
            c_col = next(
                (c for c, lo in cols_lower.items() if "country" in lo or "domicile" in lo),
                None,
            )
            if t_col and c_col:
                target, ticker_col, country_col = table, t_col, c_col
                break

        if target is None:
            raise ValueError("tabella con colonne 'Ticker' e 'Country' non trovata nella pagina")

        tickers = []
        for _, row in target.iterrows():
            raw = str(row[ticker_col]).strip()
            country = str(row[country_col]).strip().lower()
            if not raw or raw.lower() == "nan":
                continue
            suffix = COUNTRY_TO_YAHOO_SUFFIX.get(country, "")
            if not suffix:
                # Paese non nella mappa: lo segnaliamo ma non blocchiamo il resto
                print(f"[WARN] EURO STOXX 50: paese '{country}' non mappato per il ticker {raw}, saltato")
                continue
            tickers.append(f"{raw}{suffix}")
        return tickers
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista EURO STOXX 50: {e}")
        return []


def get_tickers() -> list[str]:
    """Unisce le liste di tutti i mercati monitorati (S&P 500, Nasdaq-100,
    FTSE MIB, EURO STOXX 50), toglie i duplicati e normalizza il formato
    dei ticker per Yahoo Finance.

    Se una delle quattro fonti fallisce (es. Wikipedia cambia struttura),
    lo script continua comunque con le altre tre: viene solo stampato un
    avviso, non si interrompe tutto.
    """
    all_tickers = set()
    all_tickers.update(get_sp500_tickers())
    all_tickers.update(get_nasdaq100_tickers())
    all_tickers.update(get_ftsemib_tickers())
    all_tickers.update(get_eurostoxx50_tickers())

    # Yahoo Finance vuole i ticker USA con "-" al posto di "." (es. BRK.B ->
    # BRK-B). Questa sostituzione NON va fatta sui ticker europei, che hanno
    # gia' un suffisso come ".MI" o ".PA" e in cui il punto e' corretto cosi'.
    cleaned = set()
    for t in all_tickers:
        t = str(t).strip()
        if not t or t.lower() == "nan":
            continue
        if t.endswith(EUROPEAN_SUFFIXES):
            cleaned.add(t)
        else:
            cleaned.add(t.replace(".", "-"))

    cleaned = sorted(cleaned)
    print(f"[INFO] Universo monitorato: {len(cleaned)} titoli")
    return cleaned


# ---------------------------------------------------------------------------
# 2. Controllo se almeno uno dei mercati monitorati e' aperto
# ---------------------------------------------------------------------------

def is_any_market_open(now: datetime | None = None) -> bool:
    """Controlla se siamo in una fascia oraria in cui ALMENO UNO dei mercati
    monitorati (USA, Borsa Italiana, borse dell'Eurozona) potrebbe essere
    aperto. Invece di calcolare l'orario esatto di ciascuna borsa (che
    cambia con l'ora legale in modo diverso da paese a paese), usiamo una
    finestra ampia in UTC che copre dall'apertura europea (le 9:00 circa,
    ora locale) alla chiusura USA (le 16:00 ora di New York): dalle 7:00
    alle 21:00 UTC, tutti i giorni feriali.

    Se in un dato momento solo alcuni dei mercati monitorati sono
    effettivamente aperti, non e' un problema: i titoli dei mercati chiusi
    in quel momento hanno semplicemente un prezzo invariato rispetto alla
    chiusura precedente, quindi non generano falsi allarmi.
    """
    now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))

    if now.weekday() >= 5:  # 5=sabato, 6=domenica
        return False

    return dtime(7, 0) <= now.time() <= dtime(21, 0)


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
    if not is_any_market_open():
        print("[INFO] Nessuno dei mercati monitorati e' aperto in questo momento, non faccio nulla.")
        return

    # Usiamo la data UTC per lo stato "chi ho gia' notificato oggi": e'
    # coerente su tutti i mercati monitorati (USA + Europa) ed evita di dover
    # scegliere un unico fuso orario di riferimento tra i tanti coinvolti.
    today = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
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
