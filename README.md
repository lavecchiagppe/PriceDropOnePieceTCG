# PriceDrop One Piece TCG

Monitoraggio dei prezzi One Piece Card Game su CardTrader, con segnalazione delle
inserzioni che si staccano dal mercato e storico per l'analisi dei trend.

Le decisioni di progetto e i dettagli dell'API stanno in [SPEC.md](SPEC.md).

## Stato

| Fase | Contenuto | Stato |
|---|---|---|
| 0 | Verifica dell'API CardTrader | fatta |
| 1 | Database, provider, raccolta dati | fatta |
| 2 | Regola di alert e notifiche | fatta |
| 3 | Dashboard web | fatta |
| 4 | Collezione e P&L | fatta |
| 5 | Analisi dei trend | fatta |

## Prerequisiti

- Python 3.11 o successivo (testato su 3.14)
- Un token della Full API CardTrader, dalla pagina impostazioni del tuo profilo

## Installazione

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Poi copia `.env.example` in `.env` e incolla il token dopo
`CARDTRADER_API_TOKEN=`, senza la parola `Bearer` davanti.

## Avvio

```powershell
.venv\Scripts\python.exe -m pricedrop serve
```

Apre la dashboard su <http://127.0.0.1:8000>. La raccolta periodica la fa
l'attivita' pianificata descritta sotto, quindi va avanti anche a dashboard
chiusa. E il modo normale di usare il progetto: i comandi qui sotto servono a
configurarlo e a ispezionarlo.

La dashboard mostra le occasioni sul mercato in questo momento, le segnalazioni
recenti, le carte di maggior valore e l'esito dei cicli. In cima c'e' una
pastiglia per ogni espansione: cliccandola le tre tabelle mostrano solo quel
set, e "Tutte" torna alla vista completa. Il filtro vive nell'indirizzo
(`/?expansion=eb05`), quindi la vista di un set si puo salvare nei preferiti,
e il pulsante "Raccogli ora" ci riporta dopo il ciclo. Compaiono solo le
espansioni che hanno gia' carte quotate: un set appena aggiunto, o senza
inserzioni eleggibili, non lascia una pastiglia vuota.

Sotto c'e' la stessa riga per la rarita, e i due filtri si combinano
(`/?expansion=eb05&rarity=Super%20Rare`). Qui conta un dettaglio di
CardTrader: **"Alternate Art" e a sua volta un valore di rarita**, quindi una
Super Rare in versione alternativa non risulta "Super Rare" ma "Alternate
Art". Scegliere "Super Rare" da percio le SR normali, senza le alternative, ed
e esattamente la distinzione che serve per seguirle separatamente.

Le stesse due righe di pastiglie stanno anche su **Watchlist** e **Mercato**.
Su Mercato si sommano al periodo e valgono per tutta la pagina: rialzi e
ribassi, volatilita e curva dell'offerta seguono i filtri, mentre l'indice di
prezzo, che per costruzione ha gia una linea per espansione, si limita a
mostrare la linea del set scelto. Cambiare una pastiglia non azzera le altre,
periodo compreso. Dalla pagina di una
carta si vede l'andamento del prezzo minimo e del riferimento, e la si puo
aggiungere alla watchlist. Il pulsante "Raccogli ora" lancia un ciclo
immediato.

## Avvio automatico in background

Due strade, alternative fra loro:

- **Su questo PC**, con due attivita' pianificate di Windows (la sezione qui
  sotto). Richiede che il PC sia acceso o sospeso, con la rete disponibile.
- **Online su GitHub Actions**, a PC spento: vedi [CLOUD.md](CLOUD.md). La
  raccolta e le notifiche email e Telegram girano sui server di GitHub, la
  dashboard resta locale.

### Sul PC: attivita' pianificate di Windows

Due attivita' pianificate di Windows, registrate per il tuo utente, tolgono il
lavoro manuale. Si installano una volta sola:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\installa-autostart.ps1
```

- `PriceDropOnePieceTCG` avvia la dashboard al login, in background e senza
  finestre, con un minuto di ritardo perche' la rete sia pronta.
- `PriceDropOnePieceTCG-Raccolta` lancia un ciclo di raccolta ogni 15 minuti
  (l'intervallo lo legge da `interval_minutes` in `config.toml`: se lo cambi,
  rilancia lo script di installazione), a dashboard
  aperta o chiusa, e con "Riattiva il computer per eseguire l'attivita'"
  attivo: se il PC e' sospeso, Windows lo sveglia, raccoglie e lo lascia
  tornare a dormire.

Lo script mette anche sul desktop il collegamento "PriceDrop One Piece", che
apre la dashboard con un clic: non serve piu' digitare l'indirizzo.

Poiche' la raccolta periodica ora e' compito dell'attivita' pianificata, in
`config.toml` il ciclo interno al server e' spento (`[schedule] enabled =
false`): tenerli accesi entrambi raddoppierebbe le chiamate all'API. Il
pulsante "Raccogli ora" della dashboard funziona comunque.

### Fin dove arriva

Le attivita' girano nella tua sessione, quindi funzionano a schermo spento,
col coperchio chiuso se il PC non va in sospensione, a sessione bloccata e con
la dashboard chiusa. Non funzionano a utente disconnesso o a PC spento: per
quello Windows chiede un'attivita' "anche se l'utente non ha effettuato
l'accesso", che va registrata da un amministratore. Al login successivo la
raccolta riprende dal primo ciclo utile, e lo storico resta quello che c'e'.

Il risveglio dalla sospensione dipende dai timer di riattivazione di Windows.
Su questo PC sono abilitati sia a corrente sia a batteria, quindi il portatile
si sveglia per raccogliere anche staccato dalla presa, a prezzo di un po' di
carica. Per rimetterli come li trova Windows di serie, cioe' solo a corrente:

```powershell
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 0; powercfg /setactive SCHEME_CURRENT
```

I timer in attesa si vedono con `powercfg /waketimers`.

### Comandi a mano

Avvio e arresto del server, utili subito dopo l'installazione o se lo chiudi
per sbaglio:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\avvia-in-background.ps1
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ferma.ps1
```

Lo script di avvio controlla prima se la porta e' gia' in ascolto, quindi non
parte mai un secondo server.

I log stanno in `data/logs/`: `serve.log` e `serve.err` per la dashboard,
sovrascritti a ogni avvio, e `collect.log` per la raccolta periodica, in append
con l'ora di ogni ciclo e troncato alle ultime 2000 righe quando supera 2 MB.

Per togliere tutto, attivita' e collegamento:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\disinstalla-autostart.ps1
```

I grafici sono SVG generati dal server: nessuna libreria JavaScript, nessun
codice di terze parti.

Le immagini delle carte sono l'unica cosa che la dashboard carica da fuori:
arrivano dal CDN di CardTrader, con l'URL che l'API fornisce insieme
all'anagrafica. Le pagine funzionano anche senza, con un segnaposto al posto
della miniatura, quindi la dashboard resta usabile offline. Le immagini sono
servite con `referrerpolicy="no-referrer"`, cosi il CDN non riceve l'indirizzo
della pagina locale che le sta mostrando, e con `loading="lazy"`, cosi le
tabelle lunghe non scaricano decine di file prima di comparire.

## Cosa viene monitorato

Ogni ciclo legge **tutti i blueprint delle espansioni configurate**, carte di
ogni rarita e prodotti sigillati compresi, e ne calcola la quotazione con i
filtri di `config.toml`: lingua, condizioni accettate (`Near Mint`,
`Slightly Played`), solo inserzioni comprabili via CardTrader Zero, niente
gradate. Finisce nello storico solo chi ha almeno `min_comparables`
inserzioni eleggibili e un prezzo di riferimento sopra `min_reference_cents`
(1 EUR), perche' sulle carte da pochi centesimi ogni variazione e'
percentualmente enorme e non significa nulla.

Quella soglia pero' tagliava fuori anche le rarita da collezione dal prezzo
basso: una Super Rare a 80 centesimi veniva scartata insieme ai comuni. Per
questo c'e' `track_rarities` in `[monitor]`: le rarita elencate li' entrano
nello storico comunque. Con l'elenco di serie (Super Rare, Secret Rare,
Special Rare, Treasure Rare, Alternate Art) tutte le 39 SR semplici dei set
monitorati sono quotate, dove prima ne mancavano 13 perche' sotto l'euro. Le
segnalazioni restano protette dal rumore da `min_saving_cents`: una carta da
un euro non puo' generare un alert da 3 EUR di risparmio.

## Comandi

```powershell
.venv\Scripts\python.exe -m pricedrop check
```

Verifica il token e che il gioco One Piece sia raggiungibile.

```powershell
.venv\Scripts\python.exe -m pricedrop sets
```

Elenca le espansioni One Piece e suggerisce la riga da incollare in
`config.toml` per monitorare i set principali piu recenti. Per seguire un set
che il suggerimento non copre, per esempio gli Extra Booster, aggiungi il suo
codice a mano in `expansion_codes`: i codici vanno copiati dall'elenco, perche'
non seguono una regola unica (`eb05` e `eb04` sono senza trattino, `eb-03` e
`eb-02` con il trattino). Al ciclo successivo il set entra nella raccolta e
nel filtro della dashboard.

```powershell
.venv\Scripts\python.exe -m pricedrop collect
```

Esegue un ciclo di raccolta: scarica il book delle espansioni configurate,
calcola per ogni carta il prezzo minimo e il prezzo di riferimento, e salva uno
snapshot. Aggiungi `-v` per vedere le chiamate e il dettaglio per set.

```powershell
.venv\Scripts\python.exe -m pricedrop notify
```

Invia una segnalazione di prova su tutti i canali attivi: serve a verificare
email e Telegram senza aspettare un'occasione vera.

```powershell
.venv\Scripts\python.exe -m pricedrop alerts
```

Elenca le segnalazioni registrate, con link diretto alla carta e indicazione se
la notifica e stata consegnata.

```powershell
.venv\Scripts\python.exe -m pricedrop trends
```

Indice di prezzo per espansione e carte in salita o in discesa negli ultimi 30
giorni. Dice da solo quando lo storico non basta ancora.

```powershell
.venv\Scripts\python.exe -m pricedrop status
```

Mostra quanti snapshot ci sono, su quante carte, e l'esito degli ultimi cicli.

```powershell
.venv\Scripts\python.exe -m pytest
```

Esegue i test. Non toccano la rete: usano dati sintetici e un provider finto.

## Configurazione

`config.toml` contiene i set monitorati e le soglie. I valori delle soglie sono
di partenza e vanno tarati sui dati raccolti; il perche di ciascuno e spiegato
nei commenti del file e in [SPEC.md](SPEC.md).

`.env` contiene i segreti e non va versionato.

## Come funziona la raccolta

Una chiamata per espansione restituisce tutte le carte del set con le loro
inserzioni. Per ogni carta:

1. il prezzo di ogni inserzione viene diviso per `bundle_size`, perche l'API
   quota il lotto e non la singola copia;
2. restano solo le inserzioni acquistabili via CardTrader Zero, non gradate, da
   venditori non in ferie, in Near Mint o Slightly Played inglese;
3. si ordina per prezzo e si calcola il **prezzo di riferimento** come mediana
   delle inserzioni che seguono la piu economica;
4. le carte interessanti vengono rilette con una chiamata dedicata, perche la
   risposta per espansione e in cache e puo servire un prezzo vecchio;
5. si salva uno snapshot con minimo, riferimento e numero di inserzioni.

La riverifica del punto 4 riguarda le carte in watchlist e quelle vicine alla
soglia di alert, circa una quindicina per ciclo. Serve perche i due endpoint
dell'API possono dare prezzi diversi sulla stessa inserzione, e quello per
espansione e il piu vecchio: sottostima lo scostamento, quindi nasconde gli
affari invece di inventarli.

**Attenzione alla lingua.** Il tool guarda solo le carte inglesi, come
configurato in `config.toml`. Sul sito di CardTrader l'inserzione piu economica
di una carta e spesso giapponese, e quella non entra nei conti: se un prezzo
sembra non tornare, la prima cosa da controllare e la bandierina accanto alla
condizione.

Lo scostamento fra minimo e riferimento misura quanto una singola offerta si
stacca dal mercato.

## Le due regole di segnalazione

**Outlier** confronta le inserzioni di una carta fra loro, nello stesso istante.
Segnala quando la piu economica sta sotto il riferimento oltre la soglia
percentuale **e** il risparmio in euro supera `min_saving_cents`. Servono
entrambe le condizioni: sui dati veri la sola percentuale premia le carte da
pochi centesimi, dove un meno settanta per cento vale un euro e mezzo. Funziona
dal primo ciclo, perche non ha bisogno di storico.

**Trend** confronta il prezzo di oggi con la mediana dei prezzi minimi dei
giorni precedenti, escluse le rilevazioni di oggi. Resta inattiva finche lo
storico non ha almeno `min_points` rilevazioni nella finestra, e si accende da
se quando le raggiunge.

Ogni segnalazione ha una impronta: la stessa offerta allo stesso prezzo non
viene notificata due volte. Se il venditore abbassa ancora, quella e una notizia
nuova e arriva una seconda notifica. La regola di trend notifica al massimo una
volta al giorno per carta.

## Notifiche

**Toast di Windows**: attivi per impostazione predefinita, richiedono
`win11toast` (incluso in `requirements.txt`). Si disattivano con
`desktop = false` nella sezione `[notifications]` di `config.toml`.

**Telegram**: si attiva da se quando `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`
sono compilati in `.env`. Il token si ottiene da `@BotFather`; per il chat id,
scrivi un messaggio al tuo bot e apri
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

**Email**: si attiva da se quando `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` e
`EMAIL_TO` sono compilati in `.env`. Parte una email per segnalazione, con il
nome della carta nell'oggetto e nel corpo lo stesso testo degli altri canali:
prezzo, riferimento, scostamento, risparmio e link alla carta su CardTrader.
Il dedup sulle impronte vale anche qui, quindi la stessa inserzione non viene
rimandata a ogni ciclo.

Su **Gmail** la password dell'account non funziona: Google richiede una
*password per le app* di 16 caratteri, disponibile solo con la verifica in due
passaggi attiva (myaccount.google.com, Sicurezza, Password per le app). Va
incollata in `SMTP_PASSWORD`, senza spazi. I valori per Gmail sono
`SMTP_HOST=smtp.gmail.com` e `SMTP_PORT=587`; con la porta `465` il canale
passa da se a SSL diretto invece che a STARTTLS.

Per provare la configurazione senza aspettare un'occasione vera:

```powershell
.venv\Scripts\python.exe -m pricedrop notify
```

Manda una segnalazione finta su tutti i canali attivi e stampa l'esito di
ognuno. Con `-v` mostra anche l'errore del server, utile se la password viene
rifiutata.

Se nessun canale consegna, la segnalazione resta marcata come non inviata: cosi
il dedup non la nasconde per sempre.

## Collezione

Dalla pagina di una carta si registra un acquisto con quantita, prezzo pagato a
copia, condizione e data. La pagina Collezione mostra valore attuale, costo,
guadagno e l'andamento del valore nel tempo.

Ogni acquisto resta una riga a se: due copie della stessa carta comprate a
prezzi diversi non vengono fuse, altrimenti il guadagno di ciascuna si
perderebbe nella media.

**Il valore usa il prezzo di riferimento, non il minimo.** Il minimo e spesso
una singola offerta anomala, ed e proprio cio che il tool cerca: valutarci la
collezione farebbe ballare il totale a ogni ciclo. E una stima di mercato, non
un prezzo di realizzo: vendendo pagheresti commissioni e spedizione.

Prezzo di acquisto e quotazione sono facoltativi e indipendenti. Si puo
registrare cosa si possiede senza dire quanto si e pagato, e una carta senza
mercato resta in elenco come non valutata. In entrambi i casi la pagina dice
che i totali non coprono tutto, invece di mostrare un numero che sembra
completo.

## Analisi di mercato

La pagina Mercato mostra quattro cose, tutte calcolate sul prezzo di
riferimento e non sul minimo:

- **Indice per espansione**, base 100 al primo giorno del periodo. Ogni carta
  pesa uguale, perche l'indice e la mediana delle variazioni e non la somma dei
  prezzi: cosi una carta da 800 euro non domina il suo set. Entrano solo le
  carte quotate anche il primo giorno, altrimenti una carta che compare a meta
  periodo sposterebbe la linea senza essersi mossa.
- **Carte in salita e in discesa**, confrontando prima e ultima rilevazione del
  periodo. Come per gli alert, serve anche un movimento minimo in euro: una
  variazione percentuale su una carta da pochi euro non e una notizia.
- **Offerta**: mediana delle inserzioni per carta, giorno per giorno. Quando
  cresce, i prezzi tendono a cedere poco dopo.
- **Prezzi piu instabili**: deviazione standard del minimo rapportata alla sua
  media, perche cinque euro di oscillazione dicono poco su una carta da 500 e
  molto su una da 10.

La pagina dichiara in cima quanti giorni di storico ha e avverte quando sono
troppo pochi per il periodo scelto, invece di mostrare numeri che sembrano
conclusioni.

## Nota sullo storico

Lo storico dei prezzi non e recuperabile a posteriori: parte dal primo ciclo di
raccolta. La dashboard storica e l'analisi dei trend diventano significative dopo
alcune settimane. La regola di outlier, invece, funziona dal primo ciclo, perche
confronta inserzioni fra loro e non nel tempo.
