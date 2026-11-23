# Far girare la raccolta online, a PC spento

La raccolta dei prezzi e l'invio delle notifiche girano sui server di GitHub
(GitHub Actions). Il tuo PC puo stare spento o scollegato: serve solo quando
vuoi guardare la dashboard, che resta locale.

Cosa cambia rispetto a ora:

| | Sul PC | Su GitHub Actions |
|---|---|---|
| Raccolta prezzi | attivita pianificata di Windows | workflow `.github/workflows/raccolta.yml`, ogni 5 minuti |
| Notifiche email e Telegram | si | si, identiche |
| Toast di Windows | si | no: i runner sono Linux, il canale si disattiva da se |
| Storico prezzi | `data/pricedrop.db` locale | cache fra i cicli, artifact a ogni ciclo, branch `dati` una volta al giorno |
| Dashboard | `python -m pricedrop serve` | non esposta online, si allinea al cloud da se' |

## Perche il repository deve essere pubblico

I minuti di GitHub Actions sono illimitati sui repository pubblici, mentre su
quelli privati il piano gratuito ne da 2000 al mese. Un ciclo ogni 5 minuti
sono fino a 8600 esecuzioni al mese: su un repository privato la quota
finirebbe in pochi giorni.

Pubblico significa che **il codice** e **lo storico dei prezzi** sono visibili a
chiunque. I segreti no: token CardTrader, password SMTP e credenziali Telegram
vivono nei Secrets del repository, che non compaiono ne nel codice ne nei log.
Il file `.env` resta escluso da git (`.gitignore`).

Se un domani preferisci il repository privato, basta alzare l'intervallo nel
cron del workflow: `0 * * * *` (ogni ora) sta comodamente nella quota.

## Ogni quanto, e perche 5 minuti e il minimo

Il cron di GitHub non scende sotto i 5 minuti: e il limite della piattaforma,
non una scelta. Ed e "best effort": nelle ore di punta un ciclo puo partire in
ritardo o essere saltato, quindi i cicli veri sono meno dei 288 teorici al
giorno.

Il vincolo che resta non sono i minuti ma l'API di CardTrader: un ciclo fa una
trentina di chiamate (le espansioni, i listing di ogni set, le riverifiche),
quindi a 5 minuti si arriva nell'ordine delle 9000 chiamate al giorno. Il
provider rallenta da se fra una chiamata e l'altra e gestisce i 429 con attesa
progressiva, ma se un domani CardTrader stringesse i limiti, il posto dove
intervenire e il cron qui sotto.

## Dove vive il database, e perche non su main

Lo storico deve sopravvivere fra un ciclo e l'altro: senza, ogni run
ripartirebbe da zero, la regola di calo nel tempo non si accenderebbe mai e le
stesse segnalazioni verrebbero rimandate in continuazione.

La prima versione di questo progetto lo committava su `main` a ogni ciclo. Era
insostenibile, e vale la pena sapere perche: git non sa fare delta fra due
versioni di un file SQLite, quindi **ogni** commit ristora il file intero, circa
275 KB compressi. A 5 minuti sono 288 commit al giorno, e la cronologia del
repository avrebbe superato in pochi giorni il gigabyte che GitHub consiglia di
non passare.

Ora il file viaggia su tre binari, con ruoli distinti:

| Binario | A cosa serve | Costo |
|---|---|---|
| Cache di Actions | passa il database da un ciclo al successivo | nessuno sulla cronologia git |
| Artifact del run | da qui si allinea il PC | nessuno; su repo pubblico lo spazio e gratuito |
| Branch `dati` | copia durevole se la cache viene sfrattata | un commit al giorno, ~275 KB |

Il branch `dati` tiene **un solo commit**: viene ricreato ogni volta come branch
orfano, perche serve l'ultima copia buona, non la storia di tutti i giorni.

Conseguenza pratica: su `main` il database non c'e, ed e giusto cosi. La
dashboard locale scrive nello stesso file (collezione, watchlist), e un file
versionato manderebbe in conflitto ogni `git pull`.

## Passi per attivarlo

### 1. Crea il repository su GitHub

Serve un account GitHub (gratuito). Poi, da questa cartella:

```powershell
git init
git add .
git commit -m "Primo commit"
```

Crea il repository vuoto su github.com (pulsante **New**, nome per esempio
`PriceDropOnePieceTCG`, visibilita **Public**, senza README ne .gitignore), poi:

```powershell
git remote add origin https://github.com/TUO-UTENTE/PriceDropOnePieceTCG.git
git branch -M main
git push -u origin main
```

### 2. Imposta i segreti

Nel repository su GitHub: **Settings** -> **Secrets and variables** ->
**Actions** -> **New repository secret**. Vanno creati uno per uno, con questi
nomi esatti (i valori li copi dal tuo `.env` locale):

| Nome del secret | Valore |
|---|---|
| `CARDTRADER_API_TOKEN` | il token JWT della Full API |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | l'indirizzo con cui ti autentichi |
| `SMTP_PASSWORD` | la password per le app di 16 caratteri |
| `EMAIL_TO` | il destinatario degli alert |
| `TELEGRAM_BOT_TOKEN` | solo se usi Telegram |
| `TELEGRAM_CHAT_ID` | solo se usi Telegram |

I secret che lasci fuori semplicemente disattivano quel canale: il workflow non
si rompe.

### 3. Prova il primo ciclo a mano

Nel repository: tab **Actions** -> workflow **Raccolta prezzi** -> **Run
workflow**. Dopo un paio di minuti controlla il log dello step "Raccogli i
prezzi e invia le notifiche": deve stampare quante espansioni ha letto e quante
segnalazioni ha prodotto. Se c'erano segnalazioni nuove, l'email arriva.

Dal ciclo successivo parte da se, ogni 15 minuti.

### 4. Spegni la raccolta locale

Non e opzionale: se la lasci attiva, il PC e GitHub raccolgono entrambi, le
chiamate all'API raddoppiano e i due database divergono, perche ognuno scrive
snapshot che l'altro non ha. Va rimossa **solo** l'attivita di raccolta:

```powershell
Unregister-ScheduledTask -TaskName 'PriceDropOnePieceTCG-Raccolta' -Confirm:$false
```

Non usare `scripts\disinstalla-autostart.ps1` per questo: rimuove anche l'avvio
automatico della dashboard e il collegamento sul desktop, che in locale
restano utili.

## Tenere allineata la dashboard locale

Non serve fare nulla a mano: la dashboard si allinea da se'.

- **A ogni visita.** Aprire una pagina avvia un allineamento in sottofondo. La
  pagina non aspetta, quindi i dati appena scaricati si vedono al clic
  successivo: scaricare l'artifact prima di rispondere aggiungerebbe qualche
  secondo a ogni clic. Un intervallo minimo interno, pari al ciclo di raccolta,
  evita un download per ogni clic di navigazione.
- **Col pulsante "Aggiorna dal cloud"**, in alto a destra. Questo invece
  aspetta: al ricaricamento della pagina i dati nuovi sono gia' dentro.
- **Da riga di comando**, utile per vedere cosa ha portato:

```powershell
python -m pricedrop sync
```

In fondo a ogni pagina c'e' lo stato: quando e' avvenuto l'ultimo allineamento,
quanti snapshot ha portato, e l'eventuale motivo di un fallimento.

### Come funziona

Il sync scarica l'artifact dell'ultimo ciclo riuscito tramite la CLI `gh`, che
sul PC e' gia' autenticata: nessun token da incollare da qualche parte. Poi
**travasa** nel database locale le righe che mancano, riconoscendole dalla loro
chiave naturale (per gli snapshot la coppia carta + istante, per le
segnalazioni il fingerprint). Non sovrascrive il file e non cancella niente.

Questo e' il punto importante: **collezione e watchlist esistono solo in
locale**, perche le compili dalla dashboard e il cloud non le tocca mai.
Prendere il file remoto come verita' le avrebbe cancellate a ogni sync. Per lo
stesso motivo, un ciclo lanciato a mano con "Raccogli ora" non va perso.

Se `gh` non e' installato o non e' autenticato, il pulsante non compare
nemmeno, e il resto della dashboard funziona come sempre sui dati che il PC ha
gia'.

## Cose da sapere

- **Il cron di GitHub e "best effort".** Nelle ore di punta un ciclo puo partire
  con qualche minuto di ritardo o essere saltato. Per un monitoraggio prezzi non
  cambia nulla, ma non e un orologio svizzero.
- **I workflow pianificati si sospendono dopo 60 giorni di inattivita del
  repository.** Qui non succede: il commit giornaliero sul branch `dati` conta
  come attivita.
- **Se la cache viene sfrattata** (succede dopo 7 giorni senza accessi, o oltre
  i 10 GB di cache per repository) il workflow riparte dal branch `dati`,
  perdendo al massimo i cicli della giornata in corso. Con un ciclo ogni 5
  minuti la cache viene letta e riscritta in continuazione, quindi e' uno
  scenario raro.
- **Il repository non cresce piu' in modo preoccupante**: un commit al giorno su
  un branch che tiene un solo commit.
