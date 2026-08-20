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
import re
import sys
import time
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

# Suffissi che Yahoo Finance usa per i titoli quotati sulle borse europee
# (servono per non convertire i punti in trattini nella pulizia finale,
# vedi get_tickers()).
EUROPEAN_SUFFIXES = (".MI", ".PA", ".DE", ".AS", ".MC", ".BR")

# FTSE MIB (Borsa Italiana, circa 40 titoli) ed EURO STOXX 50 (le 50
# maggiori aziende dell'Eurozona) sono elenchi FISSI scritti qui nel codice,
# invece di essere scaricati da Wikipedia come S&P 500 e Nasdaq-100.
#
# Perche': le pagine Wikipedia di questi due indici non hanno una tabella
# componenti pulita e standard come quella di S&P 500 -- in fase di test lo
# scraping ha prodotto ticker corrotti (es. "$TEN.MI.MI" invece di
# "TEN.MI"), a causa di celle di tabella unite o mal strutturate. Un elenco
# fisso e' meno elegante ma molto piu' affidabile per indici di queste
# dimensioni, che cambiano composizione solo poche volte l'anno.
#
# Manutenzione: controlla ogni tanto (un paio di volte l'anno bastano) che
# la composizione sia ancora corretta, ad esempio su borsaitaliana.it (per
# il FTSE MIB) o stoxx.com (per l'EURO STOXX 50), e aggiorna le liste qui
# sotto se necessario.
FTSE_MIB_TICKERS = [
    "A2A.MI", "AMP.MI", "AZM.MI", "BAMI.MI", "BC.MI", "BGN.MI", "BMPS.MI",
    "BPE.MI", "CPR.MI", "DIA.MI", "ENEL.MI", "ENI.MI", "FBK.MI", "G.MI",
    "HER.MI", "IG.MI", "INW.MI", "IP.MI", "ISP.MI", "IVG.MI", "LDO.MI",
    "MB.MI", "MONC.MI", "NEXI.MI", "PIRC.MI", "PRY.MI", "PST.MI", "RACE.MI",
    "REC.MI", "SPM.MI", "SRG.MI", "STLAM.MI", "STM", "TEN.MI", "TIT.MI",
    "TRN.MI", "UCG.MI", "UNI.MI",
]

EURO_STOXX50_TICKERS = [
    # Francia
    "MC.PA", "OR.PA", "SAN.PA", "TTE.PA", "AI.PA", "SU.PA", "BNP.PA", "CS.PA",
    "DG.PA", "SAF.PA", "EL.PA", "RMS.PA", "RI.PA", "AIR.PA",
    # Germania
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "BAS.DE", "BAYN.DE", "MBG.DE",
    "BMW.DE", "VOW3.DE", "MUV2.DE", "DB1.DE", "IFX.DE", "ADS.DE", "DHL.DE",
    # Paesi Bassi
    "ASML.AS", "ADYEN.AS", "HEIA.AS",
    # Spagna
    "ITX.MC", "IBE.MC", "BBVA.MC",
    # Italia
    "ENEL.MI", "ENI.MI", "ISP.MI", "UCG.MI", "STLAM.MI", "RACE.MI",
    # Belgio
    "ABI.BR",
]


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


_TICKER_LIKE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


def _looks_like_ticker(value) -> bool:
    return bool(_TICKER_LIKE.match(str(value).strip()))


def _find_ticker_column(tables: list, min_matches: int):
    """Cerca, in TUTTE le tabelle e TUTTE le colonne trovate nella pagina,
    quella che contiene il maggior numero di valori con l'aspetto di un
    ticker azionario (poche lettere maiuscole, es. "AAPL", "BRK.B").

    E' un approccio piu' robusto della ricerca per nome di colonna (es.
    "Ticker" o "Symbol"): non dipende da come la pagina Wikipedia chiama
    quella colonna in un dato momento, ma guarda direttamente il contenuto.

    Restituisce (tabella, nome_colonna) solo se la colonna migliore trovata
    ha almeno `min_matches` valori che sembrano ticker validi -- una soglia
    bassa indicherebbe che abbiamo preso per errore una tabella diversa
    (es. un box di navigazione o una tabella di dati storici).
    """
    best_table, best_col, best_count = None, None, 0
    for table in tables:
        for col in table.columns:
            try:
                values = table[col].astype(str)
            except Exception:
                continue
            count = sum(1 for v in values if _looks_like_ticker(v))
            if count > best_count:
                best_table, best_col, best_count = table, col, count
    if best_count >= min_matches:
        return best_table, best_col
    return None, best_count  # restituiamo comunque il conteggio migliore, utile per il debug


def get_sp500_tickers() -> list[str]:
    try:
        tables = _fetch_wikipedia_tables(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )
        table, col_or_count = _find_ticker_column(tables, min_matches=400)
        if table is None:
            raise ValueError(f"nessuna colonna con abbastanza ticker trovata (migliore: {col_or_count} valori)")
        return [str(t).strip() for t in table[col_or_count].tolist()]
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista S&P 500: {e}")
        return []


def get_nasdaq100_tickers() -> list[str]:
    try:
        tables = _fetch_wikipedia_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
        table, col_or_count = _find_ticker_column(tables, min_matches=80)
        if table is None:
            raise ValueError(f"nessuna colonna con abbastanza ticker trovata (migliore: {col_or_count} valori)")
        return [str(t).strip() for t in table[col_or_count].tolist()]
    except Exception as e:
        print(f"[WARN] impossibile scaricare la lista Nasdaq-100: {e}")
        return []


def get_ftsemib_tickers() -> list[str]:
    """Componenti del FTSE MIB. Elenco fisso, vedi commento su
    FTSE_MIB_TICKERS piu' sopra nel file per il perche'.
    """
    return list(FTSE_MIB_TICKERS)


def get_eurostoxx50_tickers() -> list[str]:
    """Componenti dell'EURO STOXX 50. Elenco fisso, vedi commento su
    EURO_STOXX50_TICKERS piu' sopra nel file per il perche'.
    """
    return list(EURO_STOXX50_TICKERS)


def get_tickers() -> list[str]:
    """Unisce le liste di tutti i mercati monitorati (S&P 500, Nasdaq-100,
    FTSE MIB, EURO STOXX 50), toglie i duplicati e normalizza il formato
    dei ticker per Yahoo Finance.

    S&P 500 e Nasdaq-100 vengono scaricati da Wikipedia; se una delle due
    fonti fallisce (es. Wikipedia cambia struttura), lo script continua
    comunque con le altre: viene solo stampato un avviso, non si interrompe
    tutto. FTSE MIB ed EURO STOXX 50 sono elenchi fissi (vedi sopra), quindi
    non possono fallire per problemi di rete o di struttura della pagina.
    """
    all_tickers = set()
    all_tickers.update(get_sp500_tickers())
    # Piccola pausa tra le due richieste a Wikipedia: aiuta a evitare un
    # eventuale blocco temporaneo (rate limit) se le richieste sono troppo
    # ravvicinate.
    time.sleep(2)
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
