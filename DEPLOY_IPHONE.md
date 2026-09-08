# Pubblicare FOMO Runner Radar sul web per iPhone

L'app è ora pronta per essere ospitata come sito web. Non devi eseguire Python su iPhone.

## Opzione consigliata: Render

1. Crea un repository GitHub e carica tutti i file di questa cartella.
2. Vai su Render e crea un nuovo **Blueprint** oppure un **Web Service** dal repository.
3. Render rileverà `render.yaml` / `Dockerfile`.
4. Aggiungi la variabile ambiente:
   - `FOMOSCAN_API_KEY` = la tua chiave FomoScan
5. Pubblica il servizio.
6. Render ti darà un indirizzo HTTPS del tipo:
   `https://fomo-runner-radar.onrender.com`
7. Aprilo da Safari su iPhone.

## Aggiungerlo alla Home di iPhone

In Safari:
1. Apri il link del radar.
2. Tocca **Condividi**.
3. Tocca **Aggiungi alla schermata Home**.
4. Ora FOMO Runner Radar si apre quasi come una normale app.

## Nota sulla persistenza

Il database SQLite locale funziona bene per test, ma sui piani cloud gratuiti alcuni provider possono riavviare o ricreare il filesystem.

Per un radar 24/7 serio, il passo successivo è spostare gli snapshot su PostgreSQL/Supabase e far girare il collector come worker schedulato.
