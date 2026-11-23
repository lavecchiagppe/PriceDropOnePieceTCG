# Far girare la raccolta online, a PC spento

La raccolta dei prezzi e l'invio delle notifiche girano sui server di GitHub
(GitHub Actions). Il tuo PC puo stare spento o scollegato: serve solo quando
vuoi guardare la dashboard, che resta locale.

Cosa cambia rispetto a ora:

| | Sul PC | Su GitHub Actions |
|---|---|---|
| Raccolta prezzi | attivita pianificata di Windows | workflow `.github/workflows/raccolta.yml`, ogni 15 minuti |
| Notifiche email e Telegram | si | si, identiche |
| Toast di Windows | si | no: i runner sono Linux, il canale si disattiva da se |
| Storico prezzi | `data/pricedrop.db` locale | lo stesso file, ricommittato nel repository a ogni ciclo |
| Dashboard | `python -m pricedrop serve` | non esposta online, si apre in locale |

## Perche il repository deve essere pubblico

I minuti di GitHub Actions sono illimitati sui repository pubblici, mentre su
quelli privati il piano gratuito ne da 2000 al mese. Un ciclo ogni 15 minuti
sono circa 2900 esecuzioni al mese: su un repository privato sforerebbe la
quota a metà mese.

Pubblico significa che **il codice** e **lo storico dei prezzi** sono visibili a
chiunque. I segreti no: token CardTrader, password SMTP e credenziali Telegram
vivono nei Secrets del repository, che non compaiono ne nel codice ne nei log.
Il file `.env` resta escluso da git (`.gitignore`).

Se un domani preferisci il repository privato, basta alzare l'intervallo nel
cron del workflow: `0 * * * *` (ogni ora) sta comodamente nella quota.

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

Lo storico ora cresce su GitHub. Per vederlo in dashboard, prima di aprirla:

```powershell
git pull
```

Poi `python -m pricedrop serve` come sempre. Se hai raccolto anche in locale, il
`git pull` puo entrare in conflitto sul database: in quel caso vince la copia
remota, perche e quella che non si interrompe mai.

```powershell
git checkout --theirs data/pricedrop.db
```

## Cose da sapere

- **Il cron di GitHub e "best effort".** Nelle ore di punta un ciclo puo partire
  con qualche minuto di ritardo o essere saltato. Per un monitoraggio prezzi non
  cambia nulla, ma non e un orologio svizzero.
- **I workflow pianificati si sospendono dopo 60 giorni di inattivita del
  repository.** Qui non succede: ogni ciclo fa un commit, che conta come
  attivita.
- **Il repository cresce.** Il database e circa 1 MB e viene ricommittato a ogni
  ciclo. Nell'ordine di qualche centinaio di MB di cronologia all'anno: niente di
  preoccupante, e se un giorno dasse noia si ripulisce la storia dei commit.
