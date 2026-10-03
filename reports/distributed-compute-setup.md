# Secondo PC di calcolo — primo modulo di prova

Implementazione isolata in `app/distributed`, non collegata automaticamente al runner.
Il secondo PC analizza snapshot già acquisiti dal principale: azioni, ETF e crypto
usano lo stesso contratto. Calcola momentum a 5/20 barre, media a 20 barre e RMS
dei rendimenti. Non interpreta news, non genera BUY/SELL e non aumenta i limiti API.

## Sicurezza e comportamento

- Solo campi ammessi: simbolo, tipologia, tempo dello snapshot, osservazioni di chiusura.
- Nessun conto, posizione, chiave provider o credenziale broker nel lavoro.
- Chiave HMAC dedicata all'analisi, separata da eToro e dalla dashboard.
- Coda limitata, assegnazioni con scadenza, recupero del lavoro se il worker sparisce.
- Risultati alterati, fuori assegnazione, scaduti o non finiti sono rifiutati.
- Verifica numerica locale in questa fase: serve alla parità, NON dimostra accelerazione.
- Risultati sempre `shadow_only`; nessuna modifica a scanner, rischio o ordini.
- Servizio solo loopback. Reti diverse richiedono un relay HTTPS autenticato o VPN
  cifrata configurato separatamente; non aprire la dashboard o porte di trading.
- La coda è in memoria: dopo un riavvio il principale deve ricreare gli snapshot.

## Installazione worker

Il bundle contiene solo moduli Python standard, nessuna `.env` o dipendenza AEGIS.
Richiede Python 3.12+ sul secondo Windows. Eseguire dalla cartella estratta:

```powershell
py -3.12 -m aegis_compute.worker --server https://INDIRIZZO-PROTETTO --key-file D:\AegisCompute\analysis-key.txt --worker-id pc2
```

La chiave dedicata non è inclusa nel bundle; va fornita mediante un canale sicuro.
Proteggere il file con ACL del proprio utente. Mai usare una chiave eToro al suo posto.
Non è un EXE autonomo né un secondo motore di trading. L'indirizzo remoto e la chiave
non sono ancora configurati: il doppio PC non è attivo.

## Prossimo passaggio

Collegamento cifrato tra i due Windows su reti diverse, esportazione mirata degli
snapshot reali e misurazione di tempi/parità in shadow prima di qualsiasi integrazione
con la selezione. AEGIS continua sul PC principale senza attendere il worker remoto.
