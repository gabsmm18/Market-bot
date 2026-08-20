Markets App — Alert crolli azionari via Telegram

App che controlla periodicamente i titoli di S&P 500, Nasdaq-100, FTSE MIB (Borsa Italiana) ed EURO STOXX 50 (le 50 maggiori aziende dell'Eurozona) e ti manda una notifica su Telegram quando un'azione perde piu' del 15% rispetto alla chiusura precedente, con le notizie recenti che possono spiegare il motivo.

Architettura: i tre pezzi del sistema

Qualsiasi sistema di "monitoraggio + alert" si scompone in tre parti indipendenti. Vale la pena capirle separatamente perche' sono anche i punti in cui, in futuro, potrai intervenire per migliorare il progetto.

Rilevatore (check_crashes.py, funzioni get_tickers, scan_market, get_price_change): scarica l'elenco dei titoli, interroga Yahoo Finance per il prezzo attuale e la chiusura precedente di ciascuno, calcola la variazione percentuale e filtra quelli sotto soglia.
Arricchimento (funzione get_reason): per ogni crollo trovato, recupera le notizie recenti collegate al titolo, cosi' il messaggio non è solo "XYZ -18%" ma include anche un indizio del perche'.
Notifica (funzione send_telegram_message): manda il messaggio via Telegram Bot API. Telegram fa da "app sul telefono": non serve pubblicare nulla su App Store/Play Store, basta avere Telegram installato.

Il tutto gira non sul tuo telefono, ma su un server gratuito di GitHub (GitHub Actions), a intervalli regolari — il telefono riceve solo il messaggio finale. Questo e' il motivo per cui non serve sviluppare una vera app nativa: il "backend" fa tutto il lavoro, il telefono e' solo il destinatario.

Come funziona nel dettaglio
is_any_market_open(): controlla se siamo in una fascia oraria (7:00-21:00 UTC, giorni feriali) in cui almeno uno dei mercati monitorati potrebbe essere aperto — copre sia l'apertura dei mercati europei sia l'orario di mercato USA. Se sono tutti chiusi, lo script esce subito senza fare nulla (evita chiamate inutili e falsi allarmi). Se in un dato momento solo alcuni mercati sono aperti, non e' un problema: i titoli dei mercati chiusi hanno un prezzo invariato rispetto alla chiusura precedente, quindi non generano falsi allarmi.
get_tickers(): scarica da Wikipedia l'elenco aggiornato dei componenti di S&P 500, Nasdaq-100, FTSE MIB ed EURO STOXX 50 (circa 700 titoli in unione, con duplicati tolti), aggiungendo il suffisso corretto richiesto da Yahoo Finance per ciascuna borsa (es. .MI per Milano, .PA per Parigi, .DE per Francoforte). Se una delle quattro fonti fallisce, lo script continua comunque con le altre.
scan_market(): interroga Yahoo Finance in parallelo (10 richieste alla volta) per non impiegare troppo tempo, e tiene solo i titoli scesi oltre la soglia (DROP_THRESHOLD_PCT, default 15).
get_reason(): prende le ultime notizie di Yahoo Finance per quel ticker e ne elenca i titoli. Non usa un'intelligenza artificiale per "riassumere" (vedi sotto perche' e i possibili miglioramenti).
state.json: tiene traccia di chi e' gia' stato notificato oggi, cosi' non ricevi lo stesso alert ogni 15 minuti finche' il titolo resta sotto soglia. Viene azzerato automaticamente il giorno dopo.
Setup — passo per passo
1. Crea il bot Telegram
Apri Telegram, cerca @BotFather e avvia una chat.
Manda /newbot, scegli un nome e uno username (deve finire in "bot").
BotFather ti da' un token tipo 123456789:AAExxxxxxx — salvalo, ti servira' tra poco.
Cerca il tuo bot appena creato (con lo username scelto) e mandagli un messaggio qualsiasi (es. "ciao") — serve per "attivare" la chat.
2. Trova il tuo chat_id
Cerca @userinfobot su Telegram, avvia una chat: ti restituisce il tuo Id numerico (es. 987654321). Quello e' il tuo TELEGRAM_CHAT_ID.
3. Carica il progetto su GitHub
Crea un account GitHub se non ce l'hai (gratuito): https://github.com
Crea un nuovo repository (puo' essere privato).
Carica tutti i file di questa cartella (check_crashes.py, requirements.txt, state.json, la cartella .github/, questo README.md) nel repository.
4. Aggiungi i secret

Nel repository su GitHub: Settings → Secrets and variables → Actions → New repository secret. Crea due secret:

TELEGRAM_BOT_TOKEN → il token ottenuto da BotFather
TELEGRAM_CHAT_ID → il tuo id ottenuto da userinfobot

I secret non sono mai visibili nel codice: e' il modo corretto per gestire credenziali in un progetto pubblico o condiviso.

5. Attiva GitHub Actions

Vai nella tab Actions del repository, dovresti vedere il workflow "Monitor stock crashes". Se GitHub chiede di abilitarlo, clicca "I understand my workflows, go ahead and enable them".

6. Primo test manuale

Nella tab Actions, seleziona il workflow e clicca Run workflow per lanciarlo subito senza aspettare il prossimo orario schedulato. Controlla i log: se tutti i mercati sono chiusi vedrai un messaggio che lo dice; se almeno uno e' aperto, vedrai quanti titoli sono stati controllati e se sono stati trovati crolli.

Per essere sicuro che le notifiche Telegram funzionino, puoi abbassare temporaneamente DROP_THRESHOLD_PCT (nel file monitor.yml) a un valore piu' facile da raggiungere (es. 1), lanciare il workflow a mano, verificare che il messaggio arrivi, e poi rimetterlo a 15.

Da quel momento in poi il sistema gira da solo, gratuitamente, ogni 15 minuti, dall'apertura dei mercati europei alla chiusura di quello USA.

Limiti di questa prima versione
Niente sintesi AI vera: il "motivo" del crollo sono i titoli delle notizie piu' recenti, non un riassunto discorsivo. E' un buon punto di partenza ma non sempre e' immediato da leggere.
Nessun calendario festivo: nei giorni di festa di borsa (es. Natale, Ferragosto, Thanksgiving) lo script gira comunque ma semplicemente non trova crolli.
Copertura: S&P 500 (~500 titoli, da Wikipedia), FTSE MIB (~38 titoli) ed EURO STOXX 50 (~41 titoli) — questi ultimi due tramite elenchi fissi scritti nel codice (vedi sotto il perche'). Per l'Eurozona copriamo le 50 aziende piu' grandi e non l'indice allargato Euro Stoxx 600, che avrebbe richiesto una fonte dati meno affidabile e piu' complessa da mantenere.
Nasdaq-100 attualmente non funziona: in fase di test, la pagina Wikipedia di Nasdaq-100 non restituisce (alle richieste automatiche che facciamo da GitHub Actions) una tabella leggibile con i componenti dell'indice, nonostante due diversi metodi di lettura tentati. Il sistema continua a funzionare normalmente lo stesso: nei log comparira' sempre [WARN] impossibile scaricare la lista Nasdaq-100, e nel conteggio totale dei titoli monitorati il Nasdaq-100 contribuisce con 0 -- ma dato che quasi tutte le aziende del Nasdaq-100 sono gia' incluse nell'S&P 500, la perdita di copertura reale e' piccola. Se in futuro vuoi provare a risolverlo, o preferisci un elenco fisso anche per questo (col rischio di qualche ticker non piu' aggiornato, essendo ~100 nomi), e' un intervento successivo possibile.
Gli elenchi fissi di FTSE MIB ed EURO STOXX 50 vanno controllati ogni tanto: a differenza di S&P 500 (scaricato "fresco" ogni volta da Wikipedia), questi due elenchi sono scritti direttamente nel codice (FTSE_MIB_TICKERS ed EURO_STOXX50_TICKERS in check_crashes.py) e quindi non si aggiornano da soli se la composizione dell'indice cambia. Un controllo un paio di volte l'anno su borsaitaliana.it o stoxx.com basta a mantenerli corretti.
Yahoo Finance non ufficiale: yfinance si appoggia a endpoint non documentati ufficialmente da Yahoo; puo' occasionalmente avere rallentamenti o richiedere piccoli aggiustamenti nel tempo.
Possibili miglioramenti (per quando vorrai estenderlo)
Sintesi AI del motivo: al posto dei soli titoli delle notizie, passare le headline a un modello linguistico (es. tramite l'API di Claude) che le riassuma in 2-3 frasi discorsive in italiano. Richiede una chiave API di Anthropic (a pagamento in base all'uso, ma i costi restano minimi dato che scatta solo sui crolli effettivi).
Watchlist personalizzata: aggiungere/rimuovere titoli specifici a tua scelta, oltre o al posto degli indici.
Euro Stoxx 600 completo: se in futuro vuoi la copertura piu' ampia (600 titoli su 17 borse), serve una fonte dati diversa da Wikipedia (es. le liste componenti pubblicate da un ETF che replica l'indice) e un mantenimento periodico della lista.
Calendario festivita' di borsa: usare la libreria pandas-market-calendars per evitare esecuzioni inutili nei giorni di chiusura.
Storico e dashboard: salvare ogni alert in un file/foglio e costruire una piccola pagina web che mostra lo storico dei crolli rilevati.
Soglie multiple: notifiche differenziate per -10%, -15%, -20% con urgenza diversa.
Costi

Tutto il progetto, cosi' com'e', e' gratuito: GitHub Actions offre minuti gratuiti generosi per repository pubblici (e comunque un buon numero anche per repository privati), Telegram e Yahoo Finance (via yfinance) non richiedono pagamento. L'unico costo eventuale sarebbe se in futuro aggiungi la sintesi AI con l'API di Claude (a consumo, tipicamente centesimi al mese per questo tipo di volume).
