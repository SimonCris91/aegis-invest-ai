(() => {
  const root = document.getElementById("app");
  const apiUrl = "/api/home";
  const AUTO_REFRESH_MS = 15000;
  const expandedSections = new Map();
  function improveSections(snapshot) {
    const sections = root.querySelectorAll('.grid > .section');
    sections.forEach((section, index) => {
      const heading = section.querySelector('h2');
      if (!heading) return;
      const historical = section.classList.contains('top-opportunities') || heading.textContent === 'Aegis is watching';
      if (heading.textContent === 'Aegis is watching') heading.textContent = 'Watchlist storica';
      const collapsible = historical || section.matches('.news-intelligence, .overnight-activity, .live-system-status, .positions-summary');
      if (!collapsible) return;
      const key = section.dataset.section || `section-${index}`;
      const details = document.createElement('details');
      details.className = 'section-disclosure';
      details.open = expandedSections.get(key) ?? false;
      const summary = document.createElement('summary');
      summary.textContent = heading.textContent;
      details.append(summary);
      if (historical) {
        const note = document.createElement('p');
        note.className = 'health-note';
        note.textContent = `Archivio del ${formatTimestamp(snapshot.as_of)}. Non rappresenta la valutazione attuale.`;
        details.append(note);
      }
      while (section.firstChild) details.append(section.firstChild);
      section.append(details);
      details.addEventListener('toggle', () => {
        if (details.isConnected) expandedSections.set(key, details.open);
      });
    });
    root.querySelectorAll('[data-news-disclosure]').forEach(details => {
      const key = `news:${details.dataset.newsDisclosure}`;
      details.open = expandedSections.get(key) ?? false;
      details.addEventListener('toggle', () => {
        if (details.isConnected) expandedSections.set(key, details.open);
      });
    });
    if (snapshot.live_system_status?.cycle?.state === 'NO_CYCLE') {
      const conclusion = root.querySelector('.conclusion');
      if (conclusion) {
        conclusion.querySelector('h2').textContent = 'In attesa della prossima barra';
        conclusion.querySelector('p').textContent = `Ultima analisi completata: ${formatTimestamp(snapshot.as_of)}. I risultati sono consultabili nelle sezioni dell’ultima scansione; nessuna nuova decisione in questo controllo.`;
        conclusion.querySelector('.conclusion-detail')?.remove();
      }
    }
    if (snapshot.live_system_status?.cycle?.state === 'BLOCKED') {
      const conclusion = root.querySelector('.conclusion');
      if (conclusion) {
        const closed = allMarketsClosed(snapshot);
        conclusion.querySelector('h2').textContent = closed ? 'Mercati chiusi: Aegis resta in attesa' : 'Analisi Aegis in attesa di dati validi';
        conclusion.querySelector('p').textContent = closed
          ? 'Tutti gli strumenti del ciclo risultano a mercato chiuso. Nessun ordine viene autorizzato da questo ciclo. Lo stato delle news è consultabile nel pannello dedicato.'
          : 'Il ciclo è bloccato: non è ancora disponibile una nuova valutazione del mercato. Controlla lo stato acquisizione e news nei pannelli espandibili.';
        conclusion.querySelector('.conclusion-detail')?.remove();
      }
    }
  }
  const liveDemoUrl = "/api/etoro/demo";
  const liveOrdersUrl = "/api/etoro/demo/orders";
  const liveInstrumentsUrl = "/api/etoro/instruments";
  let liveInstrumentOffset = 0;
  let liveInstrumentRows = [];
  let liveInstrumentsLoaded = false;
  let demoPositions = [];
  let manualToken = "";
  let manualBusy = false;
  let manualDraft = null;
  let draftVersion = 0;
  let loadInFlight = false;
  async function manualRequest(path, body) {
    if (!manualToken) {
      const session = await fetch('/api/manual/session', {method: 'POST', headers: {'X-Aegis-Request': 'manual'}});
      const auth = await session.json();
      if (!session.ok) throw new Error(auth.message || 'Accesso ordini richiesto. Apri http://127.0.0.1:8765 dal PC.');
      manualToken = auth.token;
    }
    const response = await fetch(path, {method: 'POST', cache: 'no-store', headers: {
      'Content-Type': 'application/json', 'Authorization': `Bearer ${manualToken}`}, body: JSON.stringify(body)});
    const result = await response.json();
    if (response.status === 401) manualToken = '';
    if (!response.ok) throw new Error(result.message || 'Richiesta non completata.');
    return result;
  }
  function selectManualOrder(button) {
    if (manualBusy) return;
    draftVersion++;
    manualDraft = null;
    const item = liveInstrumentRows.find(row => row.symbol === button.dataset.orderSymbol);
    const panel = document.getElementById('manual-order-ticket');
    if (!item || !panel) return;
    const side = button.dataset.orderSide;
    const positions = demoPositions.filter(p => String(p.instrument_id) === String(item.instrument_id) && p.direction === 'LONG');
    const selectedPosition = null;
    const liveAmount = side === 'BUY' ? item.ask : (selectedPosition && item.bid ? Number(item.bid) * Number(selectedPosition.units) : null);
    const numericAmount = Number(liveAmount);
    const amountText = liveAmount !== null && Number.isFinite(numericAmount) ? (side === 'SELL' ? numericAmount.toFixed(2) : String(liveAmount)) : '';
    const quantityText = side === 'BUY' ? '1.000000' : 'Seleziona posizione';
    panel.innerHTML = `<strong>${escapeHtml(side)} ${escapeHtml(item.symbol)} · conto DEMO</strong>
      ${side === 'SELL' ? `<label>Posizione da vendere<select id="manual-position"><option value="">Seleziona posizione</option>${positions.map(p => `<option value="${escapeHtml(p.position_id)}">${escapeHtml(p.position_id)} · ${escapeHtml(p.units)} unità</option>`).join('')}</select></label><label class="partial-close"><input id="manual-close-partial" type="checkbox"> Chiudi solo una parte</label><label id="manual-partial-units-wrap" hidden>Unità da chiudere<input id="manual-partial-units" type="number" min="0.000001" step="0.000001" inputmode="decimal" disabled></label><small>La chiusura totale è predefinita. La chiusura parziale deve rispettare il minimo eToro.</small>` : ''}
      <label>Importo live Demo (USD)<input id="manual-order-amount" type="number" min="0.01" step="0.01" inputmode="decimal" value="${escapeHtml(amountText)}" readonly></label>
      <div class="trade-quantity"><span>Quantità live</span><strong id="manual-order-quantity">${escapeHtml(quantityText || 'Seleziona posizione')}</strong></div>
      ${side === 'SELL' ? '<div id="manual-sale-estimate" class="trade-estimate">Seleziona la posizione per calcolare il P/L stimato.</div>' : ''}
      <small>${side === 'BUY' ? 'Importo automatico: Ask live per una unità. Non modificabile.' : 'Importo automatico: valore live della posizione selezionata. Non modificabile.'}</small>
      <button type="button" class="order-submit" ${amountText ? '' : 'disabled'}>Prepara ordine di ${side === 'BUY' ? 'acquisto' : 'vendita'} Demo</button><div id="manual-order-message" role="status"></div>`;
    const invalidate = () => { draftVersion++; manualDraft = null; panel.querySelector('#manual-order-message').textContent = 'Prepara il nuovo riepilogo.'; };
    const updateSellDraft = () => {
      const select = panel.querySelector('#manual-position');
      const position = positions.find(p => String(p.position_id) === String(select?.value));
      const partial = panel.querySelector('#manual-close-partial')?.checked === true;
      const unitsInput = panel.querySelector('#manual-partial-units');
      const units = partial && unitsInput?.value ? Number(unitsInput.value) : position ? Number(position.units) : null;
      const value = position && item.bid && units !== null ? Number(item.bid) * units : null;
      const average = position ? Number(position.average_entry_price) : null;
      const pnl = value !== null && Number.isFinite(average) ? value - average * units : null;
      const pnlPercent = Number.isFinite(average) && average > 0 && item.bid ? (Number(item.bid) / average - 1) * 100 : null;
      const input = panel.querySelector('#manual-order-amount');
      const prepare = panel.querySelector('.order-submit');
      if (input) input.value = value !== null && Number.isFinite(value) ? value.toFixed(2) : '';
      if (prepare) prepare.disabled = !(value !== null && Number.isFinite(value) && value > 0);
      const estimate = panel.querySelector('#manual-sale-estimate');
      const quantity = panel.querySelector('#manual-order-quantity');
      if (quantity) quantity.textContent = units !== null && Number.isFinite(units) ? `${units.toFixed(6)} unità` : 'Seleziona posizione';
      if (estimate) estimate.innerHTML = pnl !== null && pnlPercent !== null ? `Media acquisto: ${average.toFixed(2)} USD · P/L stimato: <strong class="${pnl >= 0 ? 'positive' : 'negative'}">${pnl.toFixed(2)} USD (${pnlPercent.toFixed(2)}%)</strong>` : 'Seleziona una posizione per calcolare il P/L stimato.';
      invalidate();
    };
    if (panel.querySelector('select')) panel.querySelector('select').onchange = updateSellDraft;
    if (panel.querySelector('#manual-close-partial')) panel.querySelector('#manual-close-partial').onchange = (event) => {
      const wrap = panel.querySelector('#manual-partial-units-wrap');
      const input = panel.querySelector('#manual-partial-units');
      const position = positions.find(p => String(p.position_id) === String(panel.querySelector('#manual-position')?.value));
      if (wrap) wrap.hidden = !event.target.checked;
      if (input) { input.disabled = !event.target.checked; if (event.target.checked && position) input.value = Number(position.units).toFixed(6); }
      updateSellDraft();
    };
    if (panel.querySelector('#manual-partial-units')) panel.querySelector('#manual-partial-units').oninput = updateSellDraft;
    panel.onclick = async (event) => {
      const action = event.target.closest('button');
      if (!action || manualBusy) return;
      const message = panel.querySelector('#manual-order-message');
      manualBusy = true;
      action.disabled = true;
      const version = draftVersion;
      try {
        if (action.classList.contains('order-submit')) {
          manualDraft = null;
          message.textContent = 'Verifica conto, saldo e prezzo eToro…';
          const result = await manualRequest('/api/etoro/demo/preview', {mode:'MANUAL', instrument_id:item.instrument_id,
            symbol:item.symbol, side, amount:panel.querySelector('#manual-order-amount').value,
            position_id:panel.querySelector('#manual-position')?.value || '',
            close_partial:panel.querySelector('#manual-close-partial')?.checked === true,
            units:panel.querySelector('#manual-partial-units')?.value || ''});
          if (version !== draftVersion) return;
          manualDraft = result;
          const t = result.ticket;
          const actionLabel = side === 'BUY' ? 'acquisto' : 'vendita';
          const estimate = t.estimated_pnl !== undefined ? `<p class="trade-estimate">Media acquisto: ${escapeHtml(t.average_entry_price)} USD · P/L stimato: <strong class="${Number(t.estimated_pnl) >= 0 ? 'positive' : 'negative'}">${escapeHtml(t.estimated_pnl)} USD (${escapeHtml(t.estimated_pnl_percent)}%)</strong></p>` : '';
          const quantity = t.units ? ` · quantità ${escapeHtml(t.units)} unità` : '';
          message.innerHTML = `<p>${escapeHtml(t.side)} ${escapeHtml(t.symbol)} · ${escapeHtml(t.amount)} USD · prezzo indicativo ${escapeHtml(t.price)} USD${quantity}</p>${estimate}<p>Ordine di ${actionLabel} a mercato: il prezzo finale può variare. Riepilogo valido 2 minuti.</p><button type="button" class="order-confirm">Conferma e invia ordine di ${actionLabel} Demo</button>`;
        } else if (action.classList.contains('order-confirm') && manualDraft) {
          const id = manualDraft.preview_id;
          sessionStorage.setItem('aegis-manual-last-order', id);
          manualDraft = null;
          message.textContent = 'Invio ordine Demo in corso…';
          const result = await manualRequest('/api/etoro/demo/order', {preview_id:id, confirmed:true});
          message.textContent = result.message;
          addStatusButton(message, id);
          loadLiveDemo();
        } else if (action.classList.contains('order-status')) {
          const result = await manualRequest('/api/etoro/demo/order-status', {preview_id:action.dataset.orderId});
          message.textContent = result.message;
          addStatusButton(message, action.dataset.orderId);
          loadLiveDemo();
        }
      } catch (error) {
        message.textContent = error.message;
        const id = sessionStorage.getItem('aegis-manual-last-order');
        if (id) addStatusButton(message, id);
      } finally { manualBusy = false; action.disabled = false; }
    };
    panel.scrollIntoView({behavior:'smooth', block:'center'});
    panel.querySelector('input').focus({preventScroll:true});
  }
  function addStatusButton(message, id) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'order-status'; button.dataset.orderId = id;
    button.textContent = 'Verifica esito ordine'; message.appendChild(button);
  }
  const modeStorageKey = "aegis-ui-mode";
  let selectedMode = window.localStorage.getItem(modeStorageKey) || "MANUAL";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
  const formatNumber = (value) => value === null || value === undefined ? "—" : escapeHtml(value);
  const numericValue = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const formatTimestamp = (value) => value ? new Date(value).toLocaleString("it-IT") : "Non disponibile";
  const manualStatusLabel = (value) => ({
    PREVIEW: 'PRONTO PER CONFERMA', SENDING: 'INVIO IN CORSO', UNKNOWN: 'ESITO DA VERIFICARE',
    SUBMITTED: 'INVIATO · IN VERIFICA', UNCONFIRMED: 'NON CONFERMATO', PENDING: 'IN ATTESA',
    PARTIALLY_FILLED: 'PARZIALMENTE ESEGUITO', FILLED: 'ESEGUITO', REJECTED: 'RIFIUTATO',
    CANCELLED: 'ANNULLATO'
  }[String(value || '').toUpperCase()] || 'STATO NON DISPONIBILE');
  const formatRelative = (value) => {
    if (!value) return "Non disponibile";
    const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
    return minutes < 1 ? "Just now" : minutes === 1 ? "1 min ago" : `${minutes} min ago`;
  };

  function renderLoading() {
    root.innerHTML = '<section class="state-panel" data-state="loading"><span class="spinner" aria-hidden="true"></span><p>Caricamento dello stato operativo...</p></section>';
  }
  function renderModePanel() {
    const manual = selectedMode === "MANUAL";
    return `<section class="section mode-panel" data-section="operating-mode">
      <div class="section-head"><div><span class="section-kicker">00 / MODALITÀ OPERATIVA</span><h2>Come vuoi operare?</h2></div><span class="badge ${manual ? "" : "safe"}">${manual ? "MANUALE" : "AEGIS AI"}</span></div>
      <div class="mode-switch" role="group" aria-label="Operating mode">
        <button type="button" class="mode-choice ${!manual ? "active" : ""}" data-mode="AI">Aegis Invest AI<span>Analisi, rischio e dimensionamento</span></button>
        <button type="button" class="mode-choice ${manual ? "active" : ""}" data-mode="MANUAL">Vista manuale eToro<span>Scegli personalmente lo strumento</span></button>
      </div>
      <div class="mode-description ${manual ? "manual" : "ai"}">
        <strong>${manual ? "Modalità manuale selezionata" : "Modalità Aegis AI selezionata"}</strong>
        <span>${manual ? "Il ticket manuale usa solo strumenti attualmente negoziabili secondo eToro. Le decisioni di Aegis non vengono applicate." : "Aegis prepara opportunità, rischio e dimensionamento. Da questa schermata non viene inviato alcun ordine automaticamente."}</span>
      </div>
      ${manual ? `<div class="manual-preview"><div><span class="label">Conto Demo eToro</span><strong id="live-demo-status">Caricamento conto live...</strong></div><span class="badge" id="live-demo-badge">LETTURA LIVE</span><p id="live-demo-detail">Lettura di saldo e posizioni da eToro. Nessun controllo d’ordine è attivo.</p></div>
        <section class="etoro-portfolio" aria-labelledby="manual-portfolio-title">
          <details id="portfolio-positions-disclosure" class="portfolio-disclosure" open><summary><span>Portafoglio e posizioni</span><small>Vista Demo · Live eToro</small></summary>
          <div class="portfolio-toolbar"><div><span class="section-kicker">PORTAFOGLIO DEMO</span><h3 id="manual-portfolio-title">Portafoglio</h3></div><span class="portfolio-live"><i></i> Live eToro</span></div>
          <nav class="portfolio-tabs" aria-label="Sezioni portafoglio"><button type="button" class="active" data-portfolio-tab="portfolio-positions-disclosure" aria-controls="portfolio-positions-disclosure" aria-current="page">Posizioni</button><button type="button" data-portfolio-tab="portfolio-orders-disclosure" aria-controls="portfolio-orders-disclosure">Ordini</button><button type="button" data-portfolio-tab="live-instruments-panel" aria-controls="live-instruments-panel">Operazioni manuali</button><button type="button" data-portfolio-tab="live-instruments-panel" aria-controls="live-instruments-panel">Crypto</button></nav>
          <div class="portfolio-summary" id="manual-portfolio-summary"><div><span>Cash disponibile</span><strong>—</strong></div><div><span>Valore portafoglio</span><strong>—</strong></div><div><span>P/L corrente</span><strong>—</strong></div></div>
          <div class="portfolio-table"><div class="portfolio-table-head"><span>Asset</span><span>Prezzo live</span><span>Unità</span><span>Media apertura</span><span>P/L</span><span>Valore netto</span><span>Azioni</span></div><div id="manual-portfolio-list" class="portfolio-table-body"><div class="portfolio-empty">Caricamento posizioni live…</div></div></div></details>
          <details id="portfolio-orders-disclosure" class="portfolio-disclosure orders-disclosure" open><summary><span>Ordini e verifiche</span><small id="manual-orders-summary">Controllo stato ordini Demo</small></summary><section class="manual-orders-panel" aria-labelledby="manual-orders-title"><div class="section-head"><div><span class="section-kicker">STATO ORDINI DEMO</span><h3 id="manual-orders-title">Ordini e asset in verifica</h3></div><span class="badge" id="manual-orders-badge">Caricamento…</span></div><div id="manual-orders-list" class="manual-orders-list"><div class="portfolio-empty">Controllo gli ordini locali…</div></div></section></details>
        </section>` : ""}
    </section>`;
  }
  function bindModePanel() {
    root.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
      if (manualBusy) return;
      selectedMode = button.dataset.mode;
      window.localStorage.setItem(modeStorageKey, selectedMode);
      render(currentSnapshot);
    }));
    root.querySelectorAll('[data-portfolio-tab]').forEach((button) => button.addEventListener('click', () => {
      root.querySelectorAll('[data-portfolio-tab]').forEach(tab => {
        const active = tab === button;
        tab.classList.toggle('active', active);
        tab.toggleAttribute('aria-current', active);
      });
      const target = document.getElementById(button.dataset.portfolioTab);
      if (target) {
        if (target.tagName === 'DETAILS') target.open = true;
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }));
    if (selectedMode === "MANUAL") {
      loadLiveDemo();
      loadLiveOrders();
      if (!liveInstrumentsLoaded) loadLiveInstruments();
    }
  }
  async function loadLiveOrders() {
    const list = document.getElementById('manual-orders-list');
    const badge = document.getElementById('manual-orders-badge');
    if (!list) return;
    try {
      const response = await fetch(liveOrdersUrl, {headers: {Accept: 'application/json'}, cache: 'no-store'});
      const payload = await response.json();
      if (!response.ok || payload.status !== 'LOCAL_ORDER_LEDGER') throw new Error('unavailable');
      const orders = Array.isArray(payload.orders) ? payload.orders : [];
      const pendingStates = new Set(['SENDING', 'UNKNOWN', 'SUBMITTED', 'UNCONFIRMED', 'PENDING', 'PARTIALLY_FILLED']);
      const pending = orders.filter(order => pendingStates.has(order.status));
      const staleCutoff = Date.now() - 24 * 60 * 60 * 1000;
      const stalePending = pending.filter(order => {
        const created = Date.parse(order.created_at || '');
        return Number.isFinite(created) && created < staleCutoff;
      });
      if (badge) badge.textContent = pending.length ? `${pending.length} da verificare` : 'Nessuno da verificare';
      const ordersSummary = document.getElementById('manual-orders-summary');
      if (ordersSummary) ordersSummary.textContent = pending.length ? `${pending.length} da verificare · storico separato` : 'Nessun ordine in sospeso';
      if (!orders.length) { list.innerHTML = '<div class="portfolio-empty">Nessun ordine Demo registrato.</div>'; return; }
      const sortedOrders = [...orders].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
      list.innerHTML = `${stalePending.length ? `<div class="orders-notice"><strong>${stalePending.length} ordine/i precedente/i</strong><span>Non sono nuovi ordini: sono richieste rimaste nello storico eToro. Verifica l’esito solo se ti serve riconciliare il passato.</span></div>` : ''}${sortedOrders.map(order => {
        const isPending = pendingStates.has(order.status);
        const created = Date.parse(order.created_at || '');
        const isStale = isPending && Number.isFinite(created) && created < staleCutoff;
        const label = order.side === 'SELL' ? 'VENDITA' : 'ACQUISTO';
        const details = [order.amount ? `${formatNumber(order.amount)} USD` : '', order.units ? `${formatNumber(order.units)} unità` : '', order.broker_order_id ? `ID eToro ${escapeHtml(order.broker_order_id)}` : ''].filter(Boolean).join(' · ');
        const action = isPending ? `<button type="button" class="order-status" data-order-id="${escapeHtml(order.preview_id)}">Verifica esito</button>` : '';
        const status = String(order.status || 'UNKNOWN').toUpperCase();
        const symbol = String(order.symbol || '').toUpperCase();
        const name = assetNames[symbol] || symbol || 'Asset non specificato';
        const visualStatus = isStale ? 'STORICO · ESITO DA VERIFICARE' : manualStatusLabel(status);
        return `<div class="manual-order-row ${isPending ? 'pending' : ''} ${isStale ? 'stale' : ''}"><div><strong>${label} ${escapeHtml(symbol || '—')}</strong><small>${escapeHtml(name)} · ${details || 'Riepilogo locale'} · ${formatTimestamp(order.created_at)}</small></div><span class="badge ${isPending ? '' : 'safe'}">${escapeHtml(visualStatus)}</span>${action}</div>`;
      }).join('')}`;
      if (pending.length && pending.length !== stalePending.length) window.setTimeout(() => { if (selectedMode === 'MANUAL') loadLiveOrders(); }, 30000);
      list.querySelectorAll('.order-status').forEach(button => button.addEventListener('click', async () => {
        if (button.disabled) return;
        button.disabled = true;
        try {
          const result = await manualRequest('/api/etoro/demo/order-status', {preview_id: button.dataset.orderId});
          await loadLiveOrders();
          await loadLiveDemo();
          button.setAttribute('aria-label', result.status || 'UNKNOWN');
        } catch (error) { button.textContent = error.message; button.disabled = false; }
      }));
    } catch (error) {
      if (badge) badge.textContent = 'Non disponibile';
      list.innerHTML = '<div class="portfolio-empty">Stato ordini temporaneamente non disponibile.</div>';
    }
  }
  async function loadLiveInstruments() {
    let list = document.getElementById("live-instruments-list");
    let badge = document.getElementById("live-instruments-badge");
    if (!list) {
      const panel = root.querySelector('[data-section="operating-mode"]');
      panel?.insertAdjacentHTML("beforeend", '<div id="live-instruments-panel" class="live-instruments"><div class="section-head"><div><span class="section-kicker">LIVE ETORO INSTRUMENTS</span><h3>Currently available to review</h3></div><span class="badge" id="live-instruments-badge">Loading...</span></div><div id="live-instruments-list" class="list"><div class="empty">Loading current eToro instruments...</div></div><div id="manual-order-ticket" class="manual-order-ticket"><strong>No order selected</strong><span>Choose BUY or SELL to prepare a Demo order preview.</span></div></div>');
      list = document.getElementById("live-instruments-list");
      badge = document.getElementById("live-instruments-badge");
    }
    try {
      const response = await fetch(`${liveInstrumentsUrl}?offset=${liveInstrumentOffset}`, { headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status !== "LIVE_READ_ONLY") throw new Error("unavailable");
      const items = Array.isArray(payload.instruments) ? payload.instruments : [];
      liveInstrumentRows = [...liveInstrumentRows, ...items];
      liveInstrumentsLoaded = true;
      if (badge) badge.textContent = `${liveInstrumentRows.length} caricati · live`;
      if (list) list.innerHTML = liveInstrumentRows.length ? liveInstrumentRows.map((item) => `<div class="row"><span class="symbol">${escapeHtml(item.symbol)}</span><span class="reason">${escapeHtml(item.name || item.symbol)} · Bid (vendita) ${formatNumber(item.bid)} · Ask (acquisto) ${formatNumber(item.ask)} · Last ${formatNumber(item.last_price)}</span><span class="meta">${escapeHtml(item.asset_class)}</span><span class="order-actions"><button type="button" class="order-button buy" data-order-side="BUY" data-order-symbol="${escapeHtml(item.symbol)}" ${item.buy_allowed === false ? "disabled" : ""}>ACQUISTA</button><button type="button" class="order-button sell" data-order-side="SELL" data-order-symbol="${escapeHtml(item.symbol)}" ${item.sell_allowed === false ? "disabled" : ""}>VENDI</button></span></div>`).join("") + (payload.has_more ? '<button type="button" class="load-more" id="load-more-instruments">Carica altri strumenti</button>' : '') : renderEmpty("Nessuno strumento disponibile", "eToro non ha restituito strumenti attualmente negoziabili.");
      list?.querySelectorAll('[data-order-side]').forEach(button => { button.onclick = () => selectManualOrder(button); });
      document.getElementById("load-more-instruments")?.addEventListener("click", () => { liveInstrumentOffset += items.length || 10; loadLiveInstruments(); });
      renderManualPortfolio();
    } catch (error) {
      if (badge) badge.textContent = "Non disponibile";
      if (list) list.innerHTML = renderEmpty("Elenco live non disponibile", "La lettura eToro non è terminata; non vengono sostituiti dati storici.");
    }
  }
  async function loadLiveDemo() {
    const status = document.getElementById("live-demo-status");
    const detail = document.getElementById("live-demo-detail");
    try {
      const response = await fetch(liveDemoUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status !== "LIVE_READ_ONLY") throw new Error(payload.status || "unavailable");
      demoPositions = Array.isArray(payload.positions) ? payload.positions : [];
      if (status) status.textContent = `${formatNumber(payload.cash)} ${escapeHtml(payload.currency)} cash`;
      if (detail) detail.textContent = `${Array.isArray(payload.positions) ? payload.positions.length : 0} open positions · live as of ${formatTimestamp(payload.as_of)} · broker writes 0`;
      const summary = document.getElementById('manual-portfolio-summary');
      if (summary) summary.innerHTML = `<div><span>Cash disponibile</span><strong>${formatNumber(payload.cash)} ${escapeHtml(payload.currency)}</strong></div><div><span>Valore portafoglio</span><strong>${formatNumber(payload.total_value)} ${escapeHtml(payload.currency)}</strong></div><div><span>P/L corrente</span><strong class="${numericValue(payload.current_pnl) >= 0 ? 'positive' : 'negative'}">${formatNumber(payload.current_pnl)} ${escapeHtml(payload.currency)}</strong></div>`;
      renderManualPortfolio();
    } catch (error) {
      if (status) status.textContent = "Conto live non disponibile";
      if (detail) detail.textContent = "Il dashboard resta disponibile, ma la lettura live eToro non è terminata.";
    }
  }
  function renderManualPortfolio() {
    const list = document.getElementById('manual-portfolio-list');
    if (!list) return;
    if (!demoPositions.length) {
      list.innerHTML = '<div class="portfolio-empty">Nessuna posizione aperta nel conto Demo.</div>';
      return;
    }
    list.innerHTML = demoPositions.map((position) => {
      const item = liveInstrumentRows.find(row => String(row.instrument_id) === String(position.instrument_id));
      const price = numericValue(item?.bid);
      const units = numericValue(position.units);
      const average = numericValue(position.average_entry_price);
      const value = price !== null && units !== null ? price * units : null;
      const pnl = price !== null && units !== null && average !== null ? (price - average) * units : null;
      const pnlClass = pnl === null ? '' : pnl >= 0 ? 'positive' : 'negative';
      const symbol = item?.symbol || instrumentNamesById[String(position.instrument_id)] || `ID ${position.instrument_id}`;
      const name = item?.name || assetNames[symbol] || 'Posizione Demo';
      return `<div class="portfolio-table-row"><span class="portfolio-asset"><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(name)} · ${escapeHtml(position.direction)}</small></span><span>${formatNumber(item?.bid)}</span><span>${formatNumber(position.units)}</span><span>${formatNumber(position.average_entry_price)}</span><span class="${pnlClass}">${pnl === null ? '—' : `${pnl.toFixed(2)} USD`}</span><span>${value === null ? '—' : `${value.toFixed(2)} USD`}</span><span class="portfolio-row-actions"><button type="button" class="order-button buy" data-order-side="BUY" data-order-symbol="${escapeHtml(symbol)}">ACQUISTA</button><button type="button" class="order-button sell" data-order-side="SELL" data-order-symbol="${escapeHtml(symbol)}">VENDI</button></span></div>`;
    }).join('');
    list.querySelectorAll('[data-order-side]').forEach(button => { button.onclick = () => selectManualOrder(button); });
  }
  let currentSnapshot = null;
  function renderError(message) {
    root.innerHTML = `<section class="state-panel error" data-state="error"><p>${escapeHtml(message)}</p><button class="retry" type="button">Riprova lettura</button></section>`;
    root.querySelector(".retry").addEventListener("click", load);
  }
  const assetNames = {
    API3: 'API3', ARKB: 'ARK 21Shares Bitcoin ETF', BBEU: 'JPMorgan BetaBuilders Europe ETF',
    BBJP: 'JPMorgan BetaBuilders Japan ETF', BCH: 'Bitcoin Cash', BTC: 'Bitcoin',
    EEMV: 'iShares MSCI Emerging Markets Min Vol Factor ETF', EFV: 'iShares MSCI EAFE Value ETF',
    ESGV: 'Vanguard ESG U.S. Stock ETF', ETC: 'Ethereum Classic', ETH: 'Ethereum',
    FLOT: 'iShares Floating Rate Bond ETF', ICVT: 'iShares Convertible Bond ETF',
    IFRA: 'iShares U.S. Infrastructure ETF', LTC: 'Litecoin', MOAT: 'VanEck Morningstar Wide Moat ETF',
    VSGX: 'Vanguard ESG International Stock ETF', XRP: 'XRP'
  };
  const instrumentNamesById = { '100002': 'BCH' };
  function displayAssetName(item) {
    const symbol = String(item?.symbol || '').toUpperCase();
    return item?.name && item.name !== item.symbol ? item.name : (assetNames[symbol] || symbol || 'Nome non disponibile');
  }
  function renderEmpty(title, detail) { return `<div class="empty"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`; }
  function observationDetailRows(item, rows) {
    return rows.filter(([, value]) => value !== undefined && value !== null && value !== '' && (!Array.isArray(value) || value.length)).map(([label, value]) => {
      const rendered = Array.isArray(value) ? value.join(', ') : value;
      return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(rendered)}</dd></div>`;
    }).join('');
  }
  function renderWatchlist(items) {
    if (!items.length) return renderEmpty("Nessun titolo osservato", "Nessun asset qualificato è attualmente sotto osservazione.");
    return `<div class="list observed-list">${items.map((item) => {
      const details = observationDetailRows(item, [
        ['Nome completo', displayAssetName(item)], ['Classe', item.asset_class], ['Score', item.score],
        ['Confidence', item.confidence], ['Azione', item.action], ['Timeframe', item.timeframe],
        ['Mercato', item.current_market_state], ['Qualità dati', item.data_quality], ['Freshness', item.freshness],
        ['Provenienza dati', item.provider_provenance], ['Sentiment news', item.news_sentiment],
        ['Eventi materiali', item.material_event_count], ['Fattori di rischio', item.risk_flags],
        ['Motivi osservazione', item.reasons], ['Eventi news', item.headline_event_summaries],
        ['Rischi news', item.news_risk_flags],
      ]);
      return `<details class="observation-disclosure"><summary><span class="observation-summary"><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(displayAssetName(item))}</small></span><span class="meta">Score ${formatNumber(item.score)}</span><span class="badge">Watch · ${formatNumber(item.rank)}</span></summary><dl class="observation-details">${details || '<div><dt>Dettagli</dt><dd>Non disponibili per questa osservazione.</dd></div>'}</dl></details>`;
    }).join("")}</div>`;
  }
  function renderPositions(items) {
    if (!items.length && currentSnapshot?.positions?.open_count == null) return renderEmpty("POSIZIONI NON DISPONIBILI", "Questo riepilogo non conferma il numero di posizioni. Consulta il portafoglio Demo per il dato del broker.");
    if (!items.length) return renderEmpty("NESSUNA POSIZIONE NEL RIEPILOGO", "Il riepilogo della scansione non sostituisce la lettura del portafoglio Demo.");
    return `<div class="list observed-list">${items.map((item) => {
      const details = observationDetailRows(item, [
        ['Nome completo', item.name || displayAssetName(item)], ['Classe', item.asset_class], ['Stato posizione', item.state],
        ['Valore / prezzo corrente', item.value], ['Score', item.score], ['Confidence', item.confidence],
        ['Azione', item.action], ['Timeframe', item.timeframe], ['Ciclo osservato', formatTimestamp(item.scan_cycle_timestamp)],
        ['Barra osservata', formatTimestamp(item.bar_timestamp)], ['Qualità dati', item.data_quality],
        ['Provenienza dati', item.provider_provenance], ['Freshness', item.freshness],
        ['Sessione mercato', item.market_session_state], ['Confrontabile per ingresso', item.eligible_for_entry_comparison],
        ['Motivo eleggibilità', item.eligibility_reason_code], ['Mercato', item.current_market_state],
        ['Fattori di rischio', item.risk_flags],
      ]);
      return `<details class="observation-disclosure"><summary><span class="observation-summary"><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.name || displayAssetName(item))}</small></span><span class="meta">${escapeHtml(item.state)}</span><span class="badge safe">Osservata</span></summary><dl class="observation-details">${details || '<div><dt>Dettagli</dt><dd>Non disponibili per questa osservazione.</dd></div>'}</dl></details>`;
    }).join("")}</div>`;
  }
  function allMarketsClosed(snapshot) {
    const acquisition = snapshot.live_system_status?.acquisition || {};
    const counts = acquisition.outcome_counts || {};
    const total = Number(acquisition.requested);
    const closed = Number(counts.MARKET_CLOSED_NO_NEW_BAR || 0) + Number(counts.SESSION_NOT_EXPECTED || 0);
    return acquisition.status === 'COMPLETE' && total > 0 && closed === total;
  }
  function userFacingRuntimeState(snapshot) {
    const status = snapshot.live_system_status || {};
    const runner = status.runner || {};
    const heartbeat = status.heartbeat || {};
    const cycle = status.cycle || {};
    const liveScanner = status.scanner || {};
    const session = String(cycle.market_session_state || snapshot.data_health?.market_session_state || "").toUpperCase();
    const liveTopCount = Number(liveScanner.top_opportunity_count || 0);
    if (heartbeat.stale || runner.state === "STALE") return { label: "RUNNER STALE", detail: "The last runner heartbeat is no longer current. Refresh rereads the persisted snapshot; it does not start a new scan." };
    if (cycle.state === "BLOCKED") return { label: "CICLO BLOCCATO", detail: "Il ciclo non ha superato tutti i controlli. Una opportunità TOP non equivale a un ordine autorizzato." };
    if (liveTopCount > 0 && cycle.state !== "NO_CYCLE") return { label: "OPPORTUNITÀ TOP", detail: "Selezione completata; l’ordine richiede ancora i controlli previsti." };
    if (allMarketsClosed(snapshot) || session === "CLOSED" || session === "MARKET_CLOSED") return { label: "MERCATI CHIUSI — IN ATTESA", detail: "Aegis attende una nuova barra completata valida alla riapertura dei mercati." };
    if (cycle.state === "SCANNING") return { label: "SCANNING", detail: "Aegis is evaluating the latest eligible market data." };
    if (runner.state === "RUNNING") return { label: "RUNNER ATTIVO", detail: "Aegis attende la prossima barra completata valida. I conteggi dell’ultima analisi restano separati dal monitoraggio corrente." };
    return { label: "RUNNER STOPPED", detail: "The Aegis runner is not currently active." };
  }
  function displayCycleState(value) {
    return value === "NO_CYCLE" ? "ATTESA PROSSIMA BARRA" : value;
  }
  function renderTopOpportunities(items, count, scanTimestamp) {
    if (!count) return '';
    if (!items.length) {
      return `<section class="section top-opportunities" data-section="top-opportunities"><div class="section-head"><div><span class="section-kicker">HISTORICAL — LAST COMPLETED SCAN</span><h2>${formatNumber(count)} opportunities</h2></div><span class="badge safe">${formatTimestamp(scanTimestamp)}</span></div><p class="health-note">Not current live opportunities</p><p class="health-note">The last completed scan exposes the count, but not the opportunity detail rows.</p></section>`;
    }
    return `<section class="section top-opportunities" data-section="top-opportunities"><div class="section-head"><div><span class="section-kicker">HISTORICAL — LAST COMPLETED SCAN</span><h2>${formatNumber(count)} opportunities</h2></div><span class="badge safe">${formatTimestamp(scanTimestamp)}</span></div><p class="health-note">Not current live opportunities</p><div class="list">${items.map((item) => `<div class="row"><span class="symbol">${escapeHtml(item.symbol)}</span><span class="reason">${escapeHtml(item.full_asset_name || item.name || item.symbol)}</span><span class="meta">Score ${formatNumber(item.opportunity_score ?? item.score)} · Rank ${formatNumber(item.rank)}</span><span class="badge safe">${escapeHtml((item.reasons || item.opportunity_factors || item.rejection_reasons || []).join?.(', ') || 'Reason unavailable')}</span></div>`).join('')}</div></section>`;
  }
  function renderLiveSystemStatus(status) {
    if (!status) {
      return `<section class="section live-system-status" data-section="live-system-status"><div class="section-head"><div><span class="section-kicker">03 / LIVE SYSTEM STATUS</span><h2>Live System Status</h2></div><span class="badge">UNAVAILABLE</span></div><p class="health-note">Runner and Demo runtime status are not exposed by the current read-only Home contract.</p></section>`;
    }
    const runner = status.runner || {};
    const cycle = status.cycle || {};
    const heartbeat = status.heartbeat || {};
    const demo = status.demo || {};
    const real = status.real || {};
    const activity = status.activity || {};
    const acquisition = status.acquisition || {};
    const news = status.news || {};
    const exitManagement = status.exit_management || {};
    const oneShot = status.overnight_activity?.one_shot || {};
    const rows = [];
    if (runner.state !== undefined) rows.push(`<div class="metric"><span class="label">Runner</span><span class="value">${formatNumber(runner.state)}</span></div>`);
    if (heartbeat.last_activity_at !== undefined) rows.push(`<div class="metric"><span class="label">Last heartbeat</span><span class="value timestamp">${formatTimestamp(heartbeat.last_activity_at)}</span></div>`);
    if (heartbeat.age_seconds !== undefined) rows.push(`<div class="metric"><span class="label">Heartbeat age</span><span class="value">${formatNumber(Math.round(heartbeat.age_seconds))}s</span></div>`);
    if (heartbeat.stale_threshold_seconds !== undefined) rows.push(`<div class="metric"><span class="label">Stale threshold</span><span class="value">${formatNumber(heartbeat.stale_threshold_seconds)}s</span></div>`);
    if (heartbeat.is_stale !== undefined) rows.push(`<div class="metric"><span class="label">Data freshness</span><span class="value">${heartbeat.is_stale ? "STALE" : "CURRENT"}</span></div>`);
    if (cycle.state !== undefined) rows.push(`<div class="metric"><span class="label">Cycle</span><span class="value">${formatNumber(displayCycleState(cycle.state))}</span></div>`);
    if (cycle.lastSuccessfulScanAt !== undefined) rows.push(`<div class="metric"><span class="label">Last successful scan</span><span class="value timestamp">${formatTimestamp(cycle.lastSuccessfulScanAt)}</span></div>`);
    if (demo.connectionStatus !== undefined) rows.push(`<div class="metric"><span class="label">Demo connection</span><span class="value">${formatNumber(demo.connectionStatus)}</span></div>`);
    if (demo.automaticPilotArmed !== undefined) rows.push(`<div class="metric"><span class="label">Demo pilot</span><span class="value">${formatNumber(demo.automaticPilotArmed)}</span></div>`);
    if (demo.executionEnabled !== undefined) rows.push(`<div class="metric"><span class="label">Demo execution</span><span class="value">${formatNumber(demo.executionEnabled)}</span></div>`);
    if (demo.lastSubmissionStatus !== undefined) rows.push(`<div class="metric"><span class="label">Last Demo submission</span><span class="value">${formatNumber(demo.lastSubmissionStatus)}</span></div>`);
    if (demo.brokerWriteCalls !== undefined) rows.push(`<div class="metric"><span class="label">Demo broker writes</span><span class="value">${formatNumber(demo.brokerWriteCalls)}</span></div>`);
    if (real.executionAvailable !== undefined) rows.push(`<div class="metric"><span class="label">Real execution</span><span class="value">${formatNumber(real.executionAvailable)}</span></div>`);
    if (real.brokerWriteCalls !== undefined) rows.push(`<div class="metric"><span class="label">Real broker writes</span><span class="value">${formatNumber(real.brokerWriteCalls)}</span></div>`);
    if (activity.code !== undefined) rows.push(`<div class="metric"><span class="label">Activity</span><span class="value">${formatNumber(activity.code)}</span></div>`);
    if (Array.isArray(activity.blockers) && activity.blockers.length) rows.push(`<div class="metric attention"><span class="label">Blocchi ultimo ciclo</span><span class="value">${escapeHtml(activity.blockers.join(', '))}</span></div>`);
    if (activity.last_error) rows.push(`<div class="metric attention"><span class="label">Ultimo errore</span><span class="value">${formatNumber(activity.last_error)}</span></div>`);
    if (exitManagement.observed_at !== undefined) {
      rows.push(`<div class="metric"><span class="label">Gestione uscita Demo</span><span class="value">${exitManagement.blocked ? 'BLOCCATA' : exitManagement.close_triggered ? 'CHIUSURA ATTIVATA' : 'ATTIVA'}</span></div>`);
      rows.push(`<div class="metric"><span class="label">Posizioni valutate / tenute</span><span class="value">${formatNumber(exitManagement.evaluated)} / ${formatNumber(exitManagement.held)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">Ultimo controllo uscita</span><span class="value timestamp">${formatTimestamp(exitManagement.observed_at)}</span></div>`);
    }
    if (Object.keys(acquisition).length) {
      if (acquisition.status !== undefined) rows.push(`<div class="metric"><span class="label">Market acquisition</span><span class="value">${formatNumber(acquisition.status)}</span></div>`);
      if (acquisition.requested !== undefined) rows.push(`<div class="metric"><span class="label">Acquisition attempted</span><span class="value">${formatNumber(acquisition.attempted)} / ${formatNumber(acquisition.requested)}</span></div>`);
      if (acquisition.not_attempted !== undefined) rows.push(`<div class="metric"><span class="label">Not attempted</span><span class="value">${formatNumber(acquisition.not_attempted)}</span></div>`);
      if (acquisition.in_backoff !== undefined) rows.push(`<div class="metric"><span class="label">Retry backoff</span><span class="value">${formatNumber(acquisition.in_backoff)}</span></div>`);
      if (acquisition.coverage_ratio !== undefined) rows.push(`<div class="metric"><span class="label">Coherent coverage</span><span class="value">${formatNumber(acquisition.coverage_ratio)} / min ${formatNumber(acquisition.minimum_coverage)}</span></div>`);
      const outcomes = acquisition.outcome_counts || {};
      if (outcomes.PROVIDER_UNAVAILABLE !== undefined) rows.push(`<div class="metric"><span class="label">Provider unavailable</span><span class="value">${formatNumber(outcomes.PROVIDER_UNAVAILABLE)}</span></div>`);
      if (acquisition.newest_completed_bar !== undefined) rows.push(`<div class="metric"><span class="label">Newest completed bar</span><span class="value timestamp">${formatTimestamp(acquisition.newest_completed_bar)}</span></div>`);
    }
    if (news.provider !== undefined || oneShot.news_provider !== undefined) {
      const source = Object.keys(news).length ? news : oneShot;
      rows.push(`<div class="metric"><span class="label">News provider</span><span class="value">${formatNumber(source.provider || source.news_provider)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News status</span><span class="value">${formatNumber(source.status || source.news_provider_status)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News requests</span><span class="value">${formatNumber(source.provider_request_count || source.news_provider_request_count)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News suppressed</span><span class="value">${formatNumber(source.requests_suppressed_after_rate_limit ?? source.news_requests_suppressed_after_rate_limit)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News cache hit / miss</span><span class="value">${formatNumber(source.cache_hits ?? source.news_cache_hits)} / ${formatNumber(source.cache_misses ?? source.news_cache_misses)}</span></div>`);
      const cross = source.provider_diagnostics || source.news_provider_diagnostics || {};
      if (cross.cross_source_status !== undefined) rows.push(`<div class="metric"><span class="label">Cross-check fonti</span><span class="value">${formatNumber(cross.cross_source_status)}</span></div>`);
      if (cross.corroborated_event_groups !== undefined) rows.push(`<div class="metric"><span class="label">Eventi corroborati</span><span class="value">${formatNumber(cross.corroborated_event_groups)}</span></div>`);
      if (cross.conflicting_event_groups !== undefined) rows.push(`<div class="metric"><span class="label">Conflitti tra fonti</span><span class="value">${formatNumber(cross.conflicting_event_groups)}</span></div>`);
    }
    return `<section class="section live-system-status" data-section="live-system-status"><div class="section-head"><div><span class="section-kicker">03 / LIVE SYSTEM STATUS</span><h2>Live System Status</h2></div></div>${rows.length ? `<div class="metrics">${rows.join("")}</div>` : `<p class="health-note">No live runtime fields are available.</p>`}</section>`;
  }
  function renderNewsIntelligence(news) {
    if (!news || (!news.events?.length && !Object.keys(news.asset_contexts || {}).length && !news.global_risk)) return '';
    const risk = news.global_risk || {};
    if (risk.freshness === 'NEWS_SOURCE_UNAVAILABLE' || news.status === 'PROVIDER_UNAVAILABLE') {
      return '<section class="section news-intelligence" data-section="news-intelligence"><h2>News e geopolitica</h2><p class="health-note">Fonti news non disponibili nell’ultimo aggiornamento. Rischio geopolitico e macro non valutabili: nessuno zero va interpretato come assenza di rischio.</p></section>';
    }
    const events = Array.isArray(news.events) ? news.events : [];
    const contexts = Object.entries(news.asset_contexts || {}).filter(([, value]) => value && Number(value.event_risk || 0) > 0);
    const newsLabel = value => ({ NEWS_FRESH: 'Aggiornate', NEWS_DELAYED: 'Non recenti', NEWS_STALE: 'Da aggiornare', NEWS_SOURCE_UNAVAILABLE: 'Fonte non disponibile', POSITIVE: 'Positivo', NEGATIVE: 'Negativo', NEUTRAL: 'Neutrale', GEOPOLITICS: 'Geopolitica', EARNINGS: 'Risultati aziendali', COMPANY_GUIDANCE: 'Previsioni aziendali', PRODUCT_LAUNCH: 'Nuovi prodotti', ENERGY: 'Energia', OTHER: 'Altro' }[value] || String(value || 'Non disponibile').replaceAll('_', ' '));
    const eventMarkup = events.length ? `<div class="news-event-list">${events.map((event) => {
      const linked = Array.isArray(event.linked_symbols) && event.linked_symbols.length ? event.linked_symbols.join(', ') : 'Global';
      const sources = Array.isArray(event.sources) && event.sources.length ? event.sources.join(', ') : event.source || 'Fonte non disponibile';
      const key = `${event.published_at || ''}:${event.headline || ''}`;
      return `<details class="news-event" data-news-disclosure="${escapeHtml(key)}"><summary><span class="news-event-head"><strong>${escapeHtml(newsLabel(event.category || 'OTHER'))}</strong><span>${escapeHtml(formatTimestamp(event.published_at))}</span></span><span class="news-headline">${escapeHtml(event.headline || 'Evento senza titolo')}</span><span class="news-expand-hint">Fonti e strumenti coinvolti</span></summary><div class="news-event-detail"><dl><div><dt>Sentiment</dt><dd>${escapeHtml(newsLabel(event.sentiment || 'NEUTRAL'))}</dd></div><div><dt>Impatto</dt><dd>${escapeHtml(event.impact_score ?? '—')}</dd></div><div><dt>Fonti</dt><dd>${escapeHtml(sources)}</dd></div><div><dt>Strumenti collegati</dt><dd>${escapeHtml(linked)}</dd></div></dl><p class="health-note">Collegamento informativo, non un’indicazione di acquisto o vendita.</p></div></details>`;
    }).join('')}</div>` : `<p class="empty">Nessun evento news persistito nel ciclo accettato.</p>`;
    const contextMarkup = contexts.length ? `<details class="news-group" data-news-disclosure="contexts"><summary>Contesto per strumento <span class="badge">${contexts.length}</span></summary><div class="news-context-grid">${contexts.map(([symbol, value]) => `<div class="news-context"><strong>${escapeHtml(symbol)}</strong><span>${escapeHtml(newsLabel(value.aggregate_sentiment || 'NEUTRAL'))} · rischio ${escapeHtml(value.event_risk ?? '0')}</span><small>${escapeHtml(value.asset_class || '')} · ${escapeHtml(newsLabel(value.freshness))} · ${formatNumber(value.material_event_count || 0)} eventi materiali</small></div>`).join('')}</div></details>` : '';
    const metrics = [
      ['Rischio geopolitico', risk.geopolitical_risk],
      ['Rischio macro', risk.macro_risk],
      ['Eventi ad alto impatto', risk.high_impact_event_count],
      ['Aggiornamento news', risk.freshness ? newsLabel(risk.freshness) : null],
    ].filter(([, value]) => value !== undefined && value !== null).map(([label, value]) => `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value">${formatNumber(value)}</span></div>`).join('');
    return `<section class="section news-intelligence" data-section="news-intelligence"><div class="section-head"><div><span class="section-kicker">03A / NEWS & GEOPOLITICA</span><h2>News e geopolitica · ${events.length} eventi</h2></div><span class="badge safe">Solo contesto</span></div><p class="health-note">Notizie disponibili al momento del ciclo. Apri un titolo per leggere fonti e strumenti collegati. Le news non autorizzano ordini da sole.</p>${metrics ? `<div class="metrics news-metrics">${metrics}</div>` : ''}<details class="news-group" data-news-disclosure="events"><summary>Notizie del ciclo <span class="badge">${events.length}</span></summary>${eventMarkup}</details>${contextMarkup}</section>`;
  }
  function renderOvernightActivity(activity) {
    if (!activity) return '';
    const rows = [
      ['Runner state', activity.runner_state],
      ['Last completed cycle', formatTimestamp(activity.last_completed_cycle_time)],
      ['Cycles since startup', formatNumber(activity.cycles_completed_since_startup)],
      ['Demo attempted', formatNumber(activity.demo_submissions_attempted)],
      ['Demo filled', formatNumber(activity.demo_submissions_filled)],
      ['Demo rejected', formatNumber(activity.demo_submissions_rejected)],
      ['Demo blocked', formatNumber(activity.demo_submissions_blocked)],
      ['RiskManager decision', formatNumber(activity.latest_risk_decision)],
      ['RiskManager reason', formatNumber(activity.latest_risk_reason)],
      ['Authorized capital', formatNumber(activity.authorized_capital_eur)],
      ['Managed exposure', formatNumber(activity.managed_exposure_eur)],
      ['Remaining capital', formatNumber(activity.remaining_capital_eur)],
      ['Last activity', formatTimestamp(activity.last_activity_at)],
    ];
    const history = Array.isArray(activity.top_opportunities_per_cycle) ? activity.top_opportunities_per_cycle : [];
    const historyMarkup = history.length ? `<div class="activity-history"><h3>TOP opportunities per cycle</h3>${history.map((item) => `<div class="activity-cycle"><span>${formatTimestamp(item.observed_at)}</span><strong>${formatNumber(item.top_opportunity_count)}</strong><span>${escapeHtml(displayCycleState(item.cycle_state || ''))}</span></div>`).join('')}</div>` : '';
    return `<section class="section overnight-activity" data-section="overnight-activity"><div class="section-head"><div><span class="section-kicker">04 / OVERNIGHT ACTIVITY</span><h2>Overnight activity</h2></div><span class="badge">Persisted</span></div><div class="metrics activity-metrics">${rows.map(([label, value]) => `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value compact">${typeof value === 'string' && value !== 'Unavailable' && (label.includes('cycle') || label.includes('activity')) ? escapeHtml(value) : formatNumber(value)}</span></div>`).join('')}</div>${historyMarkup}<p class="health-note">Values are read from persisted runtime events. Unavailable fields are not inferred.</p></section>`;
  }
  function render(snapshot) {
    currentSnapshot = snapshot;
    root.className = selectedMode === "MANUAL" ? "manual-mode" : "ai-mode";
    const { capital, scanner, watchlist, positions, safety, data_health: health } = snapshot;
    const topItems = Array.isArray(snapshot.last_scan_top_opportunities)
      ? snapshot.last_scan_top_opportunities
      : (Array.isArray(snapshot.top_opportunity_items) ? snapshot.top_opportunity_items : []);
    const noTrade = scanner.top_opportunities === 0;
    const degraded = health.backend_status !== "READY" || health.one_hour_status !== "READY";
    const liveAuthorizedCapital = snapshot.live_system_status?.capital?.authorized_capital_eur;
    const liveCapitalCurrency = snapshot.live_system_status ? "USD" : (capital.currency || "EUR");
    const liveRuntimeTimestamp = snapshot.live_system_status?.heartbeat?.last_activity_at;
    const userState = userFacingRuntimeState(snapshot);
    const headerStatus = document.getElementById("header-scanner-status");
    if (headerStatus) headerStatus.lastChild.textContent = ` ${userState.label}`;
    root.innerHTML = `${renderModePanel()}<div class="grid">
      <section class="section capital-section"><div><span class="section-kicker">01 / CAPITAL</span><span class="capital-amount">${formatNumber(liveAuthorizedCapital)} <small>${escapeHtml(liveCapitalCurrency)}</small></span></div><div class="capital-note"><p>AUTHORIZED AEGIS CAPITAL</p><strong>Live runtime limit · Read-only context</strong><p title="${escapeHtml(liveRuntimeTimestamp || snapshot.as_of)}">Runtime ${formatRelative(liveRuntimeTimestamp || snapshot.as_of)}</p></div></section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">02 / STATO DELLO SCANNER</span><h2>Stato dello scanner</h2></div><span class="badge">${escapeHtml(userState.label)}</span></div><p class="health-note">${escapeHtml(userState.detail)}</p><div class="metrics">
      ${[['Catalogo eToro completo', scanner.catalog_count], ['Dati pronti ora', scanner.catalog_ready_count ?? scanner.coherent_now], ['In verifica bootstrap', scanner.catalog_pending_count], ['Universo operativo validato', scanner.universe_count], ['Analizzati nell’ultima scansione', scanner.assets_scanned], ['Top watchlist mostrate', scanner.last_scan_watchlist_count], ['No-trade nell’ultima scansione', scanner.last_scan_no_trade_count], ['TOP nell’ultimo controllo', scanner.top_opportunities]].map(([label, value]) => `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value">${formatNumber(value)}</span></div>`).join('')}
      </div><p class="health-note">Ultima scansione: ${formatTimestamp(snapshot.as_of)}</p></section>
      ${renderTopOpportunities(topItems, topItems.length, snapshot.as_of)}
      ${renderLiveSystemStatus(snapshot.live_system_status)}
      ${renderNewsIntelligence(snapshot.news)}
      ${renderOvernightActivity(snapshot.live_system_status && snapshot.live_system_status.overnight_activity)}
      <section class="section conclusion"><span class="section-kicker">04 / AEGIS CONCLUSION</span><h2>${noTrade ? "No validated opportunity right now." : "Validated opportunities are available."}</h2><p>${noTrade ? "Aegis analyzed the current market snapshot and no asset currently satisfies all conditions required for promotion to a Top Opportunity." : "Aegis has identified assets that meet the current criteria for further read-only review."}</p>${noTrade ? `<p class="conclusion-detail">${formatNumber(scanner.watchlist_count)} assets remain under observation.</p>` : ""}</section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">05 / TOP WATCHLIST DEL CICLO</span><h2>Top watchlist del ciclo</h2></div><span class="meta">${formatNumber(scanner.last_scan_watchlist_count)} su ${formatNumber(scanner.assets_scanned)} valutati</span></div><p class="health-note">Questi sono solo i candidati rimasti in WATCHLIST. L’universo completo è il conteggio sopra: gli altri strumenti sono stati valutati e classificati NO_TRADE o esclusi.</p>${renderWatchlist(watchlist)}</section>
      <section class="section positions-summary" data-section="positions-summary"><div class="section-head"><div><span class="section-kicker">06 / POSIZIONI OSSERVATE</span><h2>Posizioni osservate</h2></div><span class="meta">${formatNumber(positions.open_count)} open</span></div>${renderPositions(positions.items)}</section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">07 / RISK / SAFETY</span><h2>Risk / Safety</h2></div><span class="badge ${safety.execution_mode === "READ_ONLY" ? "safe" : ""}">${formatNumber(safety.execution_mode)}</span></div><div class="metrics"><div class="metric"><span class="label">Portfolio exposure</span><span class="value">—</span></div><div class="metric"><span class="label">Open positions</span><span class="value">${formatNumber(positions.open_count)}</span></div><div class="metric"><span class="label">Risk state</span><span class="value">Unavailable</span></div><div class="metric"><span class="label">Broker writes</span><span class="value ${safety.broker_write_calls === 0 ? "green" : ""}">${formatNumber(safety.broker_write_calls)}</span></div></div></section>
      <section class="section ${degraded ? "state-panel degraded" : ""}"><div class="section-head"><h2>Stato dei dati di mercato</h2><span class="badge">${degraded ? "DA VERIFICARE" : "DATI PRONTI"}</span></div><div class="metrics"><div class="metric"><span class="label">Copertura dati 1H corrente</span><span class="value">${escapeHtml(health.one_hour_status)}</span></div><div class="metric"><span class="label">Stato del ciclo</span><span class="value">${escapeHtml(displayCycleState(health.scanner_cycle_status || 'UNKNOWN'))}</span></div><div class="metric"><span class="label">Ultima analisi completata</span><span class="value timestamp">${formatTimestamp(health.last_scan_at)}</span></div></div><p class="health-note">La disponibilità delle news è indicata separatamente nel pannello News e geopolitica.</p></section>
    </div>`;
    improveSections(snapshot);
    bindModePanel();
  }
  async function load({ initial = false, silent = false } = {}) {
    if (manualBusy) return;
    if (loadInFlight) return;
    loadInFlight = true;
    if (initial) renderLoading();
    const refreshButton = document.getElementById("refresh-data");
    if (refreshButton && !silent) { refreshButton.disabled = true; refreshButton.textContent = "Aggiornamento..."; }
    try {
      const response = await fetch(apiUrl, { method: "GET", headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status === "ERROR") throw new Error(payload.message || "Scanner state is unavailable.");
      render(payload);
      const refreshStatus = document.getElementById("auto-refresh-status");
      if (refreshStatus) refreshStatus.textContent = `Aggiornato alle ${new Date().toLocaleTimeString("it-IT")}`;
    } catch (error) {
      if (selectedMode === "MANUAL") {
        currentSnapshot = currentSnapshot || { capital: {}, scanner: { assets_scanned: 0, assets_comparable: 0, top_opportunities: 0, watchlist_count: 0, no_trade_count: 0 }, watchlist: [], positions: { open_count: 0, items: [] }, safety: { execution_mode: "READ_ONLY", broker_write_calls: 0 }, data_health: { backend_status: "LIVE_ONLY", one_hour_status: "NOT_USED" }, live_system_status: null };
        root.className = "manual-mode";
        root.innerHTML = renderModePanel() + '<section class="section live-only-note"><strong>Live Manuale disponibile</strong><p>Lo scanner AI storico non è disponibile; i dati eToro live restano separati e utilizzabili.</p></section>';
        bindModePanel();
      } else if (initial || !currentSnapshot) renderError(error.message || "Scanner state is unavailable.");
    }
    finally {
      const button = document.getElementById("refresh-data");
      if (button && !silent) { button.disabled = false; button.textContent = "Aggiorna dati"; }
      loadInFlight = false;
    }
  }
  document.getElementById("refresh-data")?.addEventListener("click", () => load());
  load({ initial: true });
  window.setInterval(() => {
    if (!document.hidden && !manualBusy) load({ silent: true });
  }, AUTO_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !manualBusy) load({ silent: true });
  });
})();
