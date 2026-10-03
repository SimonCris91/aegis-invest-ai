# AEGIS Secondary Engine

Pacchetto aggiornato il 2 ottobre 2026.

Contiene il runtime AEGIS con:

- selezione multi-asset;
- Alpha Vantage, Alpaca e GDELT;
- fallback candidato-specifico Google News RSS e Bing News RSS;
- cache e deduplicazione delle notizie;
- modalità iniziale Demo/read-only con kill switch attivo.

Il secondo PC non riceve credenziali. Dopo l’installazione, le credenziali
devono essere configurate localmente nel file `C:\AEGIS-Secondary\runtime\.env`.

Per installare:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-aegis-secondary.ps1
```

Per avviare il motore secondario:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-aegis-secondary.ps1
```

Il pacchetto parte senza inviare ordini. Non attivare l’esecuzione Demo sul
secondo PC finché non è stato definito un coordinamento esplicito tra i due
runner: due runner indipendenti sullo stesso conto possono duplicare decisioni.

## Relay news verso il principale

Il relay trasferisce solo contesti news sanitizzati e firmati. Non trasferisce
credenziali, posizioni o ordini. Sul principale genera una chiave con:

```powershell
powershell -ExecutionPolicy Bypass -File .\initialize-secondary-news-relay.ps1
```

Copia il file generato in `C:\AEGIS-Secondary\relay-key.txt` sul secondo PC.
Avvia poi il motore indicando il tunnel HTTPS corrente:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-aegis-secondary.ps1 -RelayUrl https://ORIGINE-TUNNEL/api/secondary/news
```

La chiave deve essere presente anche nel processo principale. Il relay resta
shadow-only: il principale riutilizza l’evidenza solo dentro i controlli news
e RiskManager.
