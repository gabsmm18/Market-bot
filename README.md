# Markets App — Alert crolli azionari via Telegram

App che controlla periodicamente i titoli di S&P 500 e Nasdaq-100 e ti manda
una notifica su Telegram quando un'azione perde piu' del 15% rispetto alla
chiusura precedente, con le notizie recenti che possono spiegare il motivo.

## Architettura: i tre pezzi del sistema

Qualsiasi sistema di "monitoraggio + alert" si scompone in tre parti
indipendenti. Vale la pena capirle separatamente perche' sono anche i punti
in cui, in futuro, potrai intervenire per migliorare il progetto.

1. **Rilevatore** (`check_crashes.py`, funzioni `get_tickers`, `scan_market`,
   `get_price_change`): scarica l'elenco dei titoli, interroga Yahoo Finance
   per il prezzo attuale e la chiusura precedente di ciascuno, calcola la
   variazione percentuale e filtra quelli sotto soglia.
2. **Arricchimento** (funzione `get_reason`): per ogni crollo trovato,
   recupera le notizie recenti collegate al titolo, cosi' il messaggio non è
   solo "XYZ -18%" ma include anche un indizio del perche'.
3. **Notifica** (funzione `send_telegram_message`): manda il messaggio via
   Telegram Bot API. Telegram fa da "app sul telefono": non serve pubblicare
   nulla su App Store/Play Store, basta avere Telegram installato.

Il tutto gira **non sul tuo telefono**, ma su un server gratuito di GitHub
(GitHub Actions), a intervalli regolari — il telefono riceve solo il
messaggio finale. Questo e' il motivo per cui non serve sviluppare una vera
app nativa: il "backend" fa tutto il lavoro, il telefono e' solo il
destinatario.

## Come funziona nel dettaglio

- `is_market_open()`: controlla che siamo tra le 9:30 e le 16:00, ora di New
  York, in un giorno feriale. Se il mercato e' chiuso, lo script esce subito
  senza fare nulla (evita chiamate inutili e falsi allarmi).
- `get_tickers()`: scarica da Wikipedia l'elenco aggiornato dei componenti di
  S&P 500 e Nasdaq-100 (circa 550 titoli in unione, con duplicati tolti).
- `scan_market()`: interroga Yahoo Finance in parallelo (10 richieste alla
  volta) per non impiegare troppo tempo su ~550 titoli, e tiene solo quelli
  scesi oltre la soglia (`DROP_THRESHOLD_PCT`, default 15).
- `get_reason()`: prende le ultime notizie di Yahoo Finance per quel ticker
  e ne elenca i titoli. Non usa un'intelligenza artificiale per "riassumere"
  (vedi sotto perche' e i possibili miglioramenti).
- `state.json`: tiene traccia di chi e' gia' stato notificato **oggi**, cosi'
  non ricevi lo stesso alert ogni 15 minuti finche' il titolo resta sotto
  soglia. Viene azzerato automaticamente il giorno dopo.

## Setup — passo per passo

### 1. Crea il bot Telegram

1. Apri Telegram, cerca **@BotFather** e avvia una chat.
2. Manda `/newbot`, scegli un nome e uno username (deve finire in "bot").
3. BotFather ti da' un **token** tipo `123456789:AAExxxxxxx` — salvalo, ti
   servira' tra poco.
4. Cerca il tuo bot appena creato (con lo username scelto) e mandagli un
   messaggio qualsiasi (es. "ciao") — serve per "attivare" la chat.

### 2. Trova il tuo chat_id

1. Cerca **@userinfobot** su Telegram, avvia una chat: ti restituisce il tuo
   **Id** numerico (es. `987654321`). Quello e' il tuo `TELEGRAM_CHAT_ID`.

### 3. Carica il progetto su GitHub

1. Crea un account GitHub se non ce l'hai (gratuito): https://github.com
2. Crea un nuovo repository (puo' essere privato).
3. Carica tutti i file di questa cartella (`check_crashes.py`,
   `requirements.txt`, `state.json`, la cartella `.github/`, questo
   `README.md`) nel repository.

### 4. Aggiungi i secret

Nel repository su GitHub: **Settings → Secrets and variables → Actions →
New repository secret**. Crea due secret:

- `TELEGRAM_BOT_TOKEN` → il token ottenuto da BotFather
- `TELEGRAM_CHAT_ID` → il tuo id ottenuto da userinfobot

I secret non sono mai visibili nel codice: e' il modo corretto per gestire
credenziali in un progetto pubblico o condiviso.

### 5. Attiva GitHub Actions

Vai nella tab **Actions** del repository, dovresti vedere il workflow
"Monitor stock crashes". Se GitHub chiede di abilitarlo, clicca "I
understand my workflows, go ahead and enable them".

### 6. Primo test manuale

Nella tab Actions, seleziona il workflow e clicca **Run workflow** per
lanciarlo subito senza aspettare il prossimo orario schedulato. Controlla i
log: se il mercato e' chiuso vedrai "Mercato USA chiuso"; se e' aperto,
vedrai quanti titoli sono stati controllati e se sono stati trovati crolli.

Per essere sicuro che le notifiche Telegram funzionino, puoi abbassare
temporaneamente `DROP_THRESHOLD_PCT` (nel file `monitor.yml`) a un valore
piu' facile da raggiungere (es. 1), lanciare il workflow a mano, verificare
che il messaggio arrivi, e poi rimetterlo a 15.

Da quel momento in poi il sistema gira da solo, gratuitamente, ogni 15
minuti durante l'orario di mercato USA.

## Limiti di questa prima versione

- **Niente sintesi AI vera**: il "motivo" del crollo sono i titoli delle
  notizie piu' recenti, non un riassunto discorsivo. E' un buon punto di
  partenza ma non sempre e' immediato da leggere.
- **Nessun calendario festivo**: nei giorni di festa di borsa (es. Natale,
  Thanksgiving) lo script gira comunque ma semplicemente non trova crolli.
- **Solo S&P 500 + Nasdaq-100**: circa 550 titoli, non l'intero mercato USA
  (che conta migliaia di small cap). E' una scelta di compromesso tra
  copertura e velocità/affidabilità delle richieste.
- **Yahoo Finance non ufficiale**: `yfinance` si appoggia a endpoint non
  documentati ufficialmente da Yahoo; puo' occasionalmente avere
  rallentamenti o richiedere piccoli aggiustamenti nel tempo.

## Possibili miglioramenti (per quando vorrai estenderlo)

- **Sintesi AI del motivo**: al posto dei soli titoli delle notizie, passare
  le headline a un modello linguistico (es. tramite l'API di Claude) che le
  riassuma in 2-3 frasi discorsive in italiano. Richiede una chiave API di
  Anthropic (a pagamento in base all'uso, ma i costi restano minimi dato che
  scatta solo sui crolli effettivi).
- **Watchlist personalizzata**: aggiungere/rimuovere titoli specifici a tua
  scelta, oltre o al posto degli indici.
- **Calendario festivita' di borsa**: usare la libreria `pandas-market-calendars`
  per evitare esecuzioni inutili nei giorni di chiusura.
- **Storico e dashboard**: salvare ogni alert in un file/foglio e costruire
  una piccola pagina web che mostra lo storico dei crolli rilevati.
- **Soglie multiple**: notifiche differenziate per -10%, -15%, -20% con
  urgenza diversa.

## Costi

Tutto il progetto, cosi' com'e', e' **gratuito**: GitHub Actions offre
minuti gratuiti generosi per repository pubblici (e comunque un buon numero
anche per repository privati), Telegram e Yahoo Finance (via `yfinance`)
non richiedono pagamento. L'unico costo eventuale sarebbe se in futuro
aggiungi la sintesi AI con l'API di Claude (a consumo, tipicamente
centesimi al mese per questo tipo di volume).
