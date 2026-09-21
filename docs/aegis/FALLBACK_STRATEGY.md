# Piano di fallback — corsia Demo

Questo piano resta preparato ma non attivo finché non viene verificato il nuovo profilo.

## Trigger

Applicare il fallback solo dopo tre cicli Crypto completi con dati di mercato coerenti,
news disponibili o dichiarate parziali, e zero `TOP_OPPORTUNITIES`.

## Cosa cambia

- ranking sull'intero universo validato, senza usare il capitale autorizzato per scegliere l'asset;
- soglia Crypto già calibrata a score 60 e confidence 0,50;
- selezione di un solo candidato Demo con il miglior rapporto segnale/qualità dati;
- importo determinato esclusivamente dopo la selezione, entro il capitale autorizzato;
- news usate come contesto del candidato, senza trasformare l'assenza di una fonte in un blocco totale.

## Cosa non cambia

- RiskManager, preflight, spread, liquidità e verifiche di negoziabilità restano obbligatori;
- nessun ordine viene forzato per raggiungere l'obiettivo di 1 € al giorno;
- nessuna scrittura Real: il test resta Demo;
- ogni rifiuto deve registrare il motivo deterministico.

## Criterio di successo

Il primo obiettivo non è il profitto: è completare un ciclo osservabile con candidato,
preflight, eventuale ordine Demo e riconciliazione. Solo dopo si misura il rendimento
su più giorni; 1 € al giorno resta un obiettivo sperimentale, non garantito.
