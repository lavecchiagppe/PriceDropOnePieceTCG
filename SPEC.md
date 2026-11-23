# PriceDrop One Piece TCG — Specifica di progetto

> Documento di partenza. Raccoglie le decisioni emerse dalla fase di requisiti.
> Da rivedere e approvare prima di scrivere codice.

---

## 1. Obiettivo

Strumento personale per **individuare occasioni di acquisto** su carte One Piece TCG
e per **costruire uno storico dei prezzi** su cui fare analisi di mercato.

Tre funzioni:

1. **Alert su cali di prezzo** (la funzione principale)
2. **Dashboard storica** dell'andamento dei prezzi
3. **Valore della collezione** posseduta, con P&L

---

## 2. Fonte dati

**Provider primario: CardTrader API** (`api.cardtrader.com`).

Motivazione: Cardmarket non espone un'API pubblica accessibile (l'accesso Mkm è
riservato ad account professionali) e lo scraping viola i loro ToS. CardTrader è
un marketplace europeo, prezzi in EUR, copre One Piece, e il suo book di vendita
è molto correlato a quello Cardmarket.

### Esito della verifica (fase 0 — completata)

Fonte: documentazione ufficiale CardTrader Full API,
<https://www.cardtrader.com/en/docs/api/full/reference>

| # | Punto | Esito |
|---|---|---|
| V1 | Token API | ✅ Documentato: `Authorization: Bearer [TOKEN]`, token ottenibile "inside the settings page on your CardTrader profile". Nessun requisito Pro/venditore dichiarato nella documentazione. **Resta da confermare a vista** aprendo la pagina impostazioni del tuo account. |
| V2 | Flag CardTrader Zero | ✅ **Esiste**: campo `user.can_sell_via_hub` (booleano), descritto come "You can purchase this Product via CardTrader Zero". È esattamente il filtro che serve. |
| V3 | Rate limit | ✅ Molto generosi: 200 richieste / 10 secondi su tutti gli endpoint, e 10 richieste / secondo su `GET /marketplace/products`. Non sono un vincolo per il nostro caso d'uso. |
| V4 | Book delle inserzioni | ✅ Restituisce le **25 inserzioni più economiche per blueprint**, non solo il minimo. Sufficiente per calcolare il pavimento di prezzo. |

**Scoperta che semplifica molto il progetto:** `GET /marketplace/products?expansion_id=X`
restituisce un oggetto con *tutti* i blueprint dell'espansione come chiavi, ciascuno
con le sue 25 inserzioni più economiche. Un intero set si scarica con **una sola
chiamata**: 4 set monitorati = 4 chiamate per ciclo, contro le centinaia che
servirebbero interrogando carta per carta.

### Modello dati dell'API

- `GET /games` → trovare il `game_id` di One Piece
- `GET /expansions` → trovare gli `expansion_id` dei set da monitorare
- `GET /blueprints/export?expansion_id=X` → anagrafica carte, con `image_url`,
  `card_market_ids` e `tcg_player_id` (utile per collegare i dati a Cardmarket)
- `GET /marketplace/products?expansion_id=X&language=en` → il book di vendita

Campi rilevanti di ogni inserzione (Product):

| Campo | Uso nel progetto |
|---|---|
| `price.cents`, `price.currency` | Prezzo, in centesimi, nella valuta dell'account |
| `bundle_size` | **Numero di copie nel lotto.** Il prezzo è del lotto, non della singola carta |
| `properties_hash` | Contiene `condition` e lingua |
| `fixed_properties` (blueprint) | Contiene `collector_number` e `onepiece_rarity`; vuoto sui prodotti sigillati |
| `user.can_sell_via_hub` | Eleggibilità CardTrader Zero |
| `on_vacation` | Venditore in ferie: non acquistabile, da escludere |
| `graded` | Carta gradata: mercato a sé, da escludere |
| `quantity`, `user.max_sellable_in24h_quantity` | Disponibilità reale |
| `id` | ID inserzione: serve per il link e per l'anti-spam degli alert |

Valori ammessi per `condition`: `Mint`, `Near Mint`, `Slightly Played`,
`Moderately Played`, `Played`, `Heavily Played`, `Poor`. Il nostro filtro prende
`Near Mint` e `Slightly Played`.

### Limiti noti da tenere presenti

1. **Il taglio a 25 inserzioni per blueprint, documentato, non si applica alle
   query per espansione.** Misurato su OP-17: 16.903 inserzioni eleggibili su 185
   carte, con una mediana di **106 inserzioni per carta** e punte oltre 180. Il
   book è quindi molto più ricco di quanto la documentazione faccia pensare, e il
   conteggio delle inserzioni è un indicatore di liquidità utilizzabile. Resta
   disponibile il ripiego per `blueprint_id` sulle carte di valore, che hanno
   book naturalmente più sottili (una carta a 800 € ne aveva 7).
2. La lingua si filtra lato server (`language=en`), la condizione no: va filtrata
   nel nostro codice.
3. **I due endpoint danno prezzi diversi sulla stessa inserzione.** Misurato dal
   vivo su Monkey.D.Garp (blueprint 406125): l'inserzione `465976500` di Arc
   Card Trading costava **63,16 € per `blueprint_id` e 64,46 € per
   `expansion_id`**, con le altre 16 inserzioni identiche. La risposta per
   espansione resta in cache più a lungo e serve il prezzo precedente al
   ribasso. Il prezzo in cache risulta **più alto** del vero, quindi sottostima
   lo scostamento e *nasconde* gli affari invece di inventarli. Per questo le
   carte interessanti vengono rilette una per una prima di salvare e segnalare
   (vedi §4.1).
4. Anche l'endpoint per singola carta è "lightly cached": il valore definitivo
   si vede solo nel carrello.
4. CardTrader Zero **non è gratuito**: il carrello espone un `ct_zero_fee_amount`.
   La spedizione del venditore è invece azzerata (`shipping_cost` = 0 nel subcart
   Zero).

### Architettura della fonte

Il codice usa un'interfaccia `PriceProvider` sostituibile, così che la fonte
(CardTrader, import CSV manuale, eventuale API Cardmarket futura) resti un
dettaglio implementativo.

---

## 3. Perimetro dei dati

- **Gioco:** One Piece Card Game
- **Set:** solo le espansioni recenti (ultimi 3–4 set), configurabili da file
- **Lingua:** solo **inglese**
- **Condizione:** **Near Mint** e **Slightly Played** (la condizione immediatamente
  inferiore nella scala CardTrader)
- **Spedizione:** solo inserzioni eleggibili **CardTrader Zero**

---

## 4. Regole di alert

### 4.1 Outlier sul book live — *regola primaria*

Rileva l'inserzione che compare molto sotto il prezzo prevalente delle altre.

Esempio dato dall'utente: una carta ha il pavimento intorno ai 5 € con spedizione
CardTrader Zero, le inserzioni successive sono a prezzi simili o superiori; compare
un venditore a 2,50 € → **segnalazione**.

Logica (aggiornata dopo la verifica dell'API):

1. Si parte dalle inserzioni della carta ottenute con `language=en`.
2. **Normalizzazione del prezzo**: `prezzo_unitario = price.cents / bundle_size`.
   Senza questo passaggio un lotto da 4 copie a 10 € sembrerebbe una carta a 10 €
   e falserebbe ogni confronto.
3. **Filtri di eleggibilità**: si tengono solo le inserzioni con
   `user.can_sell_via_hub = true`, `on_vacation = false`, `graded = false`,
   `condition` in (`Near Mint`, `Slightly Played`).
4. Si ordina per prezzo unitario crescente.
5. Si calcola un **prezzo di riferimento** escludendo l'inserzione più bassa:
   la mediana delle successive N inserzioni (N configurabile, default 5).
6. Se `prezzo_minimo < prezzo_riferimento * (1 - soglia)` → alert.
   Soglia di default: **35 %**.
7. Si richiede un minimo di inserzioni comparabili (default 4) per evitare falsi
   positivi su carte con mercato sottile. Se dopo i filtri ne restano meno, si
   riprova con una chiamata dedicata su `blueprint_id` prima di scartare la carta.

**Riverifica contro la cache.** Il passaggio 1 legge il book dell'intera
espansione, che è memorizzato in cache. Prima di salvare lo snapshot e di
valutare le regole, le carte interessanti vengono rilette con una chiamata
dedicata su `blueprint_id`, che restituisce prezzi più freschi. Sono
interessanti le carte seguite in watchlist e quelle il cui scostamento arriva
almeno al 70 % della soglia di alert: il margine serve perché il prezzo in cache
sottostima lo scostamento, quindi una carta che sembra a −29 % con soglia a
−35 % potrebbe già essere un affare. Il costo misurato è di circa 16 chiamate
per ciclo su 157 carte quotate, con un tetto di 60.

**Confronto tra prezzi nudi, non "landed".** In regime CardTrader Zero la
spedizione del venditore è azzerata e la commissione Zero si applica al subcart
complessivo, non alla singola inserzione: non differenzia un'offerta dall'altra.
Quindi il confronto sui prezzi nudi è corretto. Il costo reale finale (commissione
Zero inclusa) va però mostrato nell'alert, perché su carte da pochi euro incide.

Parametri da tarare sui dati reali: soglia, N, numero minimo di inserzioni.

**Anti-spam:** ogni alert viene registrato con un'impronta dell'inserzione, così
la stessa offerta non viene notificata più di una volta.

**Soglia assoluta, aggiunta dopo la prima raccolta reale.** Oltre allo
scostamento percentuale serve un risparmio minimo in euro (`min_saving_cents`,
300 di partenza). Le due condizioni valgono insieme: senza la seconda, le
segnalazioni sono dominate da carte da pochi centesimi dove un −77 % vale 1,32 €.

### 4.2 Calo rispetto al passato recente — *regola secondaria*

Alert quando il prezzo minimo di oggi sta almeno X % sotto la **mediana dei
prezzi minimi registrati nella finestra di N giorni, escluse le rilevazioni di
oggi** (default: −15 %, finestra 7 giorni). La mediana, e non la media, perché
regge meglio al singolo valore anomalo — che è esattamente ciò che cerca la
regola 4.1.

Escludere il giorno corrente è necessario: altrimenti la regola misurerebbe oggi
contro se stesso, diluito dalle rilevazioni delle ore precedenti.

Resta inattiva finché la finestra non contiene almeno `min_points` rilevazioni
(5 di partenza), e si accende da sé quando le raggiunge. Anche questa regola
richiede un risparmio minimo in valore assoluto.

### 4.3 Anti-spam

Ogni segnalazione porta un'impronta:

- outlier: `outlier:<carta>:<inserzione>:<prezzo>` — la stessa offerta allo
  stesso prezzo non si ripete, ma un ribasso ulteriore è una notizia nuova;
- trend: `trend:<carta>:<giorno>` — al massimo una volta al giorno per carta.

Una segnalazione viene marcata come notificata **solo se almeno un canale l'ha
consegnata davvero**. Marcarla comunque la farebbe scomparire per sempre dietro
il dedup, senza che tu l'abbia mai vista.

---

## 5. Raccolta dati

- **Automatica ogni 15 minuti** tramite un'attività pianificata di Windows
  (`PriceDropOnePieceTCG-Raccolta`, vedi README). Lo scheduler interno
  all'applicazione esiste ancora ma sta spento in `config.toml`, per non
  raccogliere due volte quando la dashboard è aperta
- **On demand** con un pulsante nella dashboard e un comando CLI
- Ogni esecuzione salva uno snapshot per carta: prezzo minimo, prezzo di
  riferimento, numero di inserzioni eleggibili, timestamp
- **Costo per ciclo: una chiamata per set monitorato.** Con 5 set sono 5 chiamate
  ogni 15 minuti, più la rilettura senza cache delle sole carte candidate
  (una ventina a ciclo), contro un limite di 10 al secondo. Nessuna rotazione a priorità
  necessaria: il vincolo dei rate limit, che in fase di spec sembrava il collo di
  bottiglia, non esiste.

### Nota importante sullo storico

**Lo storico non è recuperabile a posteriori.** Nessuna fonte fornisce i prezzi
passati: il database parte vuoto e la serie storica si costruisce dall'attivazione
dello scheduler. La dashboard storica e l'analisi di mercato diventano significative
dopo alcune settimane di raccolta. La regola di alert 4.1, invece, **funziona dal
primo ciclo**, perché confronta inserzioni tra loro e non nel tempo.

---

## 6. Notifiche

- **Telegram** — bot dedicato, messaggio con nome carta, prezzo, scostamento dal
  riferimento e link diretto all'inserzione. Servono bot token e chat id.
- **Notifiche desktop Windows** — toast locale quando lo scheduler è attivo sul PC.

Entrambi i canali sono attivabili/disattivabili da configurazione.

---

## 7. Collezione

Gestione delle carte possedute con **quantità e prezzo di acquisto**, per calcolare:

- valore attuale della collezione
- P&L complessivo e per singolo acquisto
- andamento del valore nel tempo

**Scelte fatte in fase 4:**

- **Un acquisto, una riga.** Due copie della stessa carta comprate a prezzi
  diversi restano separate: fonderle perderebbe il P&L di ciascuna.
- **Valutazione al prezzo di riferimento, non al minimo.** Il minimo è spesso
  una singola offerta anomala — è esattamente ciò che la regola di alert cerca —
  e valutarci la collezione farebbe oscillare il totale a ogni ciclo di
  raccolta.
- **Costo e quotazione sono indipendenti e facoltativi.** Si può registrare cosa
  si possiede senza il prezzo pagato, e una carta senza mercato resta in elenco
  come non valutata. I totali sommano solo le righe complete e dichiarano in
  pagina quante ne restano fuori.
- **Nei giorni senza rilevazione il prezzo si trascina.** Altrimenti il valore
  totale crollerebbe ogni volta che una carta non viene quotata.
- La linea del costo nel grafico è ferma: è quanto è stato speso per ciò che si
  possiede oggi, non una ricostruzione storica degli acquisti.

---

## 8. Stack tecnico

Scelta: **Python + SQLite**, applicazione singola.

- **FastAPI** — API e dashboard web in locale
- **SQLite** — storico prezzi, watchlist, collezione, alert
- **APScheduler** — ciclo orario di raccolta, nello stesso processo
- **httpx** — chiamate al provider
- **ECharts** (o Chart.js) — grafici nella dashboard

Motivazione: un solo linguaggio e un solo processo da avviare, nessuna
infrastruttura da gestire, e Python è l'ambiente più comodo per le analisi
statistiche che servono alla regola di outlier e ai trend.

---

## 9. Struttura prevista

```
PriceDropOnePieceTCG/
├── SPEC.md
├── config.toml              # set monitorati, soglie, canali di notifica
├── .env                     # token API, credenziali Telegram (non versionato)
├── pricedrop/
│   ├── providers/           # interfaccia PriceProvider + CardTrader
│   ├── db/                  # modelli e migrazioni
│   ├── collector.py         # ciclo di raccolta
│   ├── rules.py             # logica di alert
│   ├── notifiers/           # Telegram, toast Windows
│   └── web/                 # FastAPI + template dashboard
└── tests/
```

---

## 10. Fasi di lavoro proposte

| Fase | Contenuto | Esito |
|---|---|---|
| 0 | ~~Verifica dei punti V1–V4 sull'API CardTrader~~ | ✅ **Completata: via libera** |
| 1 | ~~Schema database + provider + raccolta dati~~ | ✅ **Completata**: 157 snapshot su 519 carte, 10 chiamate API, 5 secondi per ciclo |
| 2 | ~~Regola di outlier + notifiche Telegram/Windows~~ | ✅ **Completata**: 3 segnalazioni reali al primo ciclo, dedup verificato |
| 3 | ~~Dashboard: watchlist, grafici, storico~~ | ✅ **Completata**: FastAPI + grafici SVG lato server, scheduler orario in-process |
| 4 | ~~Collezione e P&L~~ | ✅ **Completata**: valutazione al prezzo di riferimento, P&L per acquisto e andamento del valore |
| 5 | ~~Regola di calo percentuale e analisi di trend~~ | ✅ **Completata**: indice per espansione, movimenti, offerta e volatilità |

---

## 10bis. Analisi di mercato (fase 5)

- **Indice equipesato per espansione**, base 100: mediana delle variazioni di
  ogni carta rispetto al proprio prezzo iniziale. Non la somma dei prezzi, che
  sarebbe dominata dalle poche carte di valore, e non la media, che la singola
  carta anomala sposterebbe.
- **Solo le carte presenti al primo giorno** entrano nell'indice, altrimenti
  una nuova carta quotata a metà periodo muoverebbe la linea senza che nessun
  prezzo sia cambiato.
- **Movimenti con doppia soglia**, percentuale e assoluta, per la stessa
  ragione della regola di alert.
- **Volatilità sul prezzo minimo**, non sul riferimento: il minimo è la serie
  che oscilla, ed è proprio l'oscillazione che si vuole misurare. È l'unica
  misura della pagina che non usa il riferimento.
- **La copertura dello storico è dichiarata in pagina**, con un avviso quando i
  giorni raccolti non bastano per la finestra scelta. Il tool preferisce dire
  che non sa, invece di mostrare un numero che sembra una conclusione.

## 11. Punti aperti

**Risolti dalla verifica di fase 0:**

- ~~Numero massimo di carte monitorabili per ciclo~~ → nessun limite pratico:
  una chiamata per set.
- ~~Se la spedizione va sommata al prezzo nel confronto~~ → no, si confrontano i
  prezzi nudi (vedi §4.1).

**Risolti dalla fase 1 (dati reali):**

- ~~Token API~~ → funzionante, autenticazione verificata.
- ~~Quali espansioni~~ → One Piece è `game_id` 15, con 101 espansioni. Monitorati
  i quattro set principali: `op19`, `op18`, `op17`, `op16`.
- ~~Il book è censurato a 25 inserzioni~~ → no, mediana di 106 per carta.

**Ancora aperti:**

- Soglia percentuale (35 %), finestra (N = 5) e minimo comparabili (4): la prima
  raccolta reale ha mostrato che la **sola soglia percentuale non basta**. I
  quattro scostamenti maggiori su OP-17 erano carte sotto i 2 €, dove un −70 %
  vale 1,32 €, mentre il segnale di valore era una carta a 800,54 € contro un
  riferimento di 1.250,64 € (−36 %, 450 € di differenza). Da qui il parametro
  `min_saving_cents`, aggiunto a 300: lo applicherà la regola di alert in fase 2.
- ~~Se trattare le versioni alt-art/parallel come carte separate~~ → confermato
  dal vivo su EB-05 e OP-17: sono blueprint distinti, e **CardTrader espone
  `Alternate Art` come valore di `onepiece_rarity`**, non come variante di una
  rarità. Una SR alternativa ha quindi rarità `Alternate Art`, non `Super
  Rare`: filtrare su `Super Rare` dà le SR normali e basta. La rarità e il
  numero di collezione stanno dentro `fixed_properties` del blueprint, non in
  cima all'oggetto; letti dalla radice risultavano sempre vuoti. Entrambi sono
  ora salvati nella tabella `cards`, e la dashboard filtra per rarità.
- I prodotti sigillati (box, case, sleeve) sono blueprint dell'espansione come
  le carte, e si riconoscono perché `fixed_properties` è vuoto: restano senza
  rarità, quindi fuori da ogni filtro per rarità.
- Quanta parte del catalogo vale la pena storicizzare: il 64 % delle carte viene
  scartato perché il riferimento sta sotto 1 €. Sono i comuni, dove non si fanno
  affari; se servisse l'analisi di mercato completa, basta abbassare
  `min_reference_cents`. La soglia però colpiva anche le rarità da collezione dal
  prezzo basso — 13 Super Rare su 39 stavano sotto l'euro e non entravano nello
  storico. Da qui `track_rarities` in `[monitor]`: le rarità elencate passano la
  soglia comunque. Gli alert restano protetti dal rumore da `min_saving_cents`.
