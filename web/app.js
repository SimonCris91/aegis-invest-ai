(() => {
  const root = document.getElementById("app");
  const catalogSearchRoot = document.getElementById("catalog-search-root");
  const apiUrl = "/api/home";
  const AUTO_REFRESH_MS = 15000;
  const expandedSections = new Map();
  root.style.overflowAnchor = 'none';
  function registerReadingAnchors() {
    root.querySelectorAll('.pnl-history-row').forEach(row => {
      // Closed trades and filled orders are immutable; counts/order may change.
      row.dataset.viewAnchor = `${row.className}:${row.textContent}`;
    });
  }
  // Capture actual DOM state, not delayed toggle events, before every rebuild.
  function readingKeys() {
    const occurrences = new Map();
    return [...root.querySelectorAll('details')].map(node => {
      const section = node.closest('[data-section], .capital-section');
      const seed = node.id || node.dataset.newsDisclosure ||
        `${section?.dataset.section || section?.className || 'dashboard'}:${node.className}:${node.querySelector(':scope > summary strong')?.textContent || ''}`;
      const ordinal = occurrences.get(seed) || 0;
      occurrences.set(seed, ordinal + 1);
      return { node, key: `${seed}:${ordinal}` };
    });
  }
  function captureReadingView() {
    registerReadingAnchors();
    const disclosures = readingKeys();
    const anchors = [
      ...root.querySelectorAll('[data-view-anchor]'),
      ...disclosures.map(({ node, key }) => ({ node, key: `details:${key}` })),
    ].map(item => item.node ? item : { node: item, key: item.dataset.viewAnchor });
    const visible = anchors.map(({ node, key }) => ({ key, top: node.getBoundingClientRect().top }))
      .filter(item => item.top >= 0 && item.top < window.innerHeight)
      .sort((a, b) => a.top - b.top)[0];
    return { x: window.scrollX, y: window.scrollY, visible,
      disclosures: new Map(disclosures.map(({ node, key }) => [key, node.open])) };
  }
  function restoreReadingView(view) {
    if (!view) return;
    registerReadingAnchors();
    const disclosures = readingKeys();
    disclosures.forEach(({ node, key }) => {
      if (view.disclosures.has(key)) node.open = view.disclosures.get(key);
    });
    const anchor = view.visible && (
      [...root.querySelectorAll('[data-view-anchor]')].find(node => node.dataset.viewAnchor === view.visible.key) ||
      disclosures.find(item => `details:${item.key}` === view.visible.key)?.node
    );
    const top = anchor ? window.scrollY + anchor.getBoundingClientRect().top - view.visible.top : view.y;
    // Disclosures must be restored before scrolling, otherwise the browser clamps y.
    window.scrollTo({ left: view.x, top, behavior: 'instant' });
  }
  let currentBenchmarkSnapshot = null;
  function improveSections(snapshot) {
    root.querySelectorAll('.section-kicker').forEach(kicker => {
      kicker.textContent = kicker.textContent.replace(/^\d+[A-Z]?\s*\/\s*/, '');
    });
    const sections = root.querySelectorAll('.grid > .section');
    sections.forEach((section, index) => {
      const heading = section.querySelector('h2');
      if (!heading) return;
      const historical = section.classList.contains('top-opportunities') || heading.textContent === 'Aegis is watching';
      if (heading.textContent === 'Aegis is watching') heading.textContent = 'Watchlist storica';
      const collapsible = historical || section.matches('.mode-panel, .news-intelligence, .overnight-activity, .live-system-status, .positions-summary, .broker-positions, .watchlist-section, [data-section="risk-safety"], [data-section="market-data-health"], [data-section="scanner-summary"]');
      if (!collapsible) return;
      const key = section.dataset.section || `section-${index}`;
      const details = document.createElement('details');
      details.className = 'section-disclosure';
      details.open = expandedSections.get(key) ?? false;
      const summary = document.createElement('summary');
      const summaryLabel = document.createElement('span');
      summaryLabel.textContent = heading.textContent;
      summary.append(summaryLabel);
      const summaryMeta = section.querySelector('.section-head .badge, .section-head .meta');
      if (summaryMeta?.textContent?.trim()) {
        const summaryContext = document.createElement('small');
        summaryContext.textContent = summaryMeta.textContent.trim();
        summary.append(summaryContext);
      }
      const summaryUpdated = document.createElement('small');
      summaryUpdated.className = 'section-summary-updated';
      summaryUpdated.textContent = `Dati ${formatTimestamp(sectionTimestampFor(snapshot, section))}`;
      summary.append(summaryUpdated);
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
    const listSections = [...root.querySelectorAll('.grid > .section')].filter(section => section.matches(
      '.mode-panel, .top-opportunities, .news-intelligence, .overnight-activity, .live-system-status, .watchlist-section, .positions-summary, .broker-positions, .benchmark-panel, [data-section="risk-safety"], [data-section="market-data-health"], [data-section="scanner-summary"], [data-section="monitored-positions"]'
    ));
    if (listSections.length) {
      const menuSection = document.createElement('section');
      menuSection.className = 'section dashboard-lists-menu';
      const menu = document.createElement('details');
      menu.className = 'dashboard-menu-disclosure';
      menu.open = expandedSections.get('dashboard-lists-menu') ?? false;
      const menuSummary = document.createElement('summary');
      menuSummary.innerHTML = '<span><strong>Pannelli e liste operative</strong><small>Scanner, news, watchlist, posizioni e controlli</small></span><em>Apri dettagli</em>';
      const menuBody = document.createElement('div');
      menuBody.className = 'dashboard-menu-items';
      listSections.forEach(section => menuBody.append(section));
      menu.append(menuSummary, menuBody);
      menuSection.append(menu);
      setSectionUpdatedAt(menuSection, snapshot.as_of, 'Pannelli operativi');
      const conclusion = root.querySelector('.grid > .conclusion');
      if (conclusion) conclusion.after(menuSection);
      else root.querySelector('.grid')?.append(menuSection);
      menu.addEventListener('toggle', () => {
        if (menu.isConnected) expandedSections.set('dashboard-lists-menu', menu.open);
      });
    }
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
  let demoCurrency = "USD";
  let manualToken = "";
  let manualBusy = false;
  let manualEditing = false;
  let manualDraft = null;
  let draftVersion = 0;
  let loadInFlight = false;
  let catalogSearchState = {
    query: '', results: [], matchedCount: 0, hasMore: false, loading: false,
    error: '', catalogAsOf: null, readinessAsOf: null, hasSearched: false
  };
  let catalogSearchVisible = false;

  function setCatalogSearchVisibility(visible = catalogSearchVisible, focus = false) {
    catalogSearchVisible = Boolean(visible);
    if (catalogSearchRoot) catalogSearchRoot.hidden = !catalogSearchVisible;
    const button = document.getElementById('toggle-catalog-search');
    if (button) {
      button.setAttribute('aria-expanded', String(catalogSearchVisible));
      button.textContent = catalogSearchVisible ? 'Chiudi cerca' : 'Cerca';
    }
    if (catalogSearchVisible && focus) catalogSearchRoot?.querySelector('#catalog-search-query')?.focus();
  }

  function applyPositionFilter(filter = 'all') {
    const section = root.querySelector('[data-section="broker-positions"]');
    if (!section) return;
    const normalized = ['loss', 'gain', 'all'].includes(filter) ? filter : 'all';
    section.querySelectorAll('.broker-position').forEach(row => {
      row.hidden = normalized !== 'all' && row.dataset.pnlState !== normalized;
    });
    section.querySelector('.position-filter-note')?.remove();
    if (normalized === 'all') return;
    const list = section.querySelector('.observed-list');
    if (!list) return;
    const note = document.createElement('p');
    note.className = 'health-note position-filter-note';
    note.innerHTML = `Filtro rapido: ${normalized === 'loss' ? 'posizioni in perdita' : 'posizioni in guadagno'} · <button type="button" data-clear-position-filter>Mostra tutte</button>`;
    list.before(note);
  }

  function openDashboardSection(sectionKey, filter = 'all') {
    const section = root.querySelector(`[data-section="${sectionKey}"]`);
    if (!section) return;
    const disclosure = section.querySelector(':scope > .section-disclosure');
    if (disclosure) {
      disclosure.open = true;
      expandedSections.set(sectionKey, true);
    }
    if (sectionKey === 'broker-positions') applyPositionFilter(filter);
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function enhanceQuickLinks() {
    const capital = root.querySelector('.capital-section');
    const capitalLinks = {
      'Posizioni aperte': ['broker-positions', 'all'],
      'Posizioni in perdita': ['broker-positions', 'loss'],
      'Posizioni in guadagno': ['broker-positions', 'gain'],
      'P/L posizioni in perdita': ['broker-positions', 'loss'],
      'P/L posizioni in guadagno': ['broker-positions', 'gain'],
    };
    capital?.querySelectorAll('.metric').forEach(metric => {
      const label = metric.querySelector('.label')?.textContent?.trim();
      const link = capitalLinks[label];
      if (!link) return;
      metric.classList.add('metric-link');
      metric.dataset.openSection = link[0];
      metric.dataset.positionFilter = link[1];
      metric.setAttribute('role', 'button');
      metric.tabIndex = 0;
      metric.setAttribute('aria-label', `${label}: apri il dettaglio`);
    });
    const scanner = [...root.querySelectorAll('.section')].find(section => section.querySelector('h2')?.textContent?.trim() === 'Stato dello scanner');
    const scannerLinks = {
      'Top watchlist mostrate': ['watchlist-section', 'all'],
      'Candidati TOP nel runtime (non ordini)': ['top-opportunities', 'all'],
    };
    scanner?.querySelectorAll('.metric').forEach(metric => {
      const label = metric.querySelector('.label')?.textContent?.trim();
      const link = scannerLinks[label];
      if (!link) return;
      metric.classList.add('metric-link');
      metric.dataset.openSection = link[0];
      metric.dataset.positionFilter = link[1];
      metric.setAttribute('role', 'button');
      metric.tabIndex = 0;
      metric.setAttribute('aria-label', `${label}: apri il dettaglio`);
    });
  }

  function renderDashboardShortcuts(snapshot) {
    const scanner = snapshot.scanner || {};
    const positions = snapshot.positions || {};
    return `<section class="section dashboard-shortcuts" data-section="dashboard-shortcuts"><div class="section-head"><div><span class="section-kicker">ACCESSO RAPIDO</span><h2>Informazioni del cruscotto</h2></div><span class="meta">Apri solo ciò che ti serve</span></div><div class="shortcut-grid"><button type="button" class="shortcut-button" data-open-section="broker-positions" data-position-filter="all"><strong>Posizioni aperte</strong><span>${formatNumber(positions.open_count)} · Demo live</span></button><button type="button" class="shortcut-button" data-open-section="broker-positions" data-position-filter="loss"><strong>Posizioni in perdita</strong><span>Apri il dettaglio P/L negativo</span></button><button type="button" class="shortcut-button" data-open-section="broker-positions" data-position-filter="gain"><strong>Posizioni in guadagno</strong><span>Apri il dettaglio P/L positivo</span></button><button type="button" class="shortcut-button" data-open-section="top-opportunities"><strong>Candidati TOP</strong><span>${formatNumber(scanner.top_opportunities)} · ranking storico</span></button><button type="button" class="shortcut-button" data-open-section="watchlist-section"><strong>Watchlist</strong><span>${formatNumber(scanner.last_scan_watchlist_count)} nell’ultima scansione</span></button><button type="button" class="shortcut-button" data-open-section="news-intelligence"><strong>News e geopolitica</strong><span>Contesto del ciclo</span></button><button type="button" class="shortcut-button" data-open-section="overnight-activity"><strong>Attività Aegis</strong><span>Registro e cicli</span></button><button type="button" class="shortcut-button" data-open-section="risk-safety"><strong>Risk / Safety</strong><span>Controlli e broker writes</span></button><button type="button" class="shortcut-button" data-open-section="market-data-health"><strong>Dati di mercato</strong><span>Copertura e stato ciclo</span></button><button type="button" class="shortcut-button" data-open-section="operating-mode"><strong>Modalità</strong><span>AI o vista manuale</span></button><button type="button" class="shortcut-button" data-open-search><strong>Cerca strumento</strong><span>Catalogo eToro completo</span></button></div></section>`;
  }

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
    // Selecting BUY/SELL must not move the user away from the row they chose.
    // Keep automatic dashboard refreshes from rebuilding this ticket mid-edit.
    manualEditing = true;
    draftVersion++;
    manualDraft = null;
    const requestedInstrumentId = button.dataset.orderInstrumentId;
    const selectedPosition = demoPositions.find(position =>
      position.direction === 'LONG' && (
        (requestedInstrumentId && String(position.instrument_id) === String(requestedInstrumentId))
        || (!requestedInstrumentId && String(position.symbol) === String(button.dataset.orderSymbol))
      )
    );
    const item = liveInstrumentRows.find(row =>
      (requestedInstrumentId && String(row.instrument_id) === String(requestedInstrumentId))
      || (!requestedInstrumentId && row.symbol === button.dataset.orderSymbol)
    ) || (selectedPosition ? {
      symbol: selectedPosition.symbol || button.dataset.orderSymbol,
      instrument_id: selectedPosition.instrument_id,
      bid: selectedPosition.current_price,
      ask: selectedPosition.current_price,
    } : null);
    if (!item) {
      manualEditing = false;
      return;
    }
    const selectedRow = button.closest('.portfolio-table-row');
    let panel = document.getElementById('manual-order-ticket');
    // The portfolio list is rebuilt from live data.  Recreate the ticket when
    // that rebuild removed the previously selected ticket, rather than turning
    // a valid BUY/SELL click into a silent no-op.
    if (!panel) {
      panel = document.createElement('section');
      panel.id = 'manual-order-ticket';
      panel.className = 'manual-order-ticket';
    }
    if (selectedRow) {
      selectedRow.insertAdjacentElement('afterend', panel);
    } else {
      const panelHost = document.getElementById('live-instruments-panel') || button.closest('.etoro-portfolio');
      if (!panelHost) {
        manualEditing = false;
        return;
      }
      panelHost.append(panel);
    }
    const side = button.dataset.orderSide;
    const positions = demoPositions.filter(p => String(p.instrument_id) === String(item.instrument_id) && p.direction === 'LONG');
    const liveAmount = side === 'BUY' ? item.ask : null;
    const numericAmount = Number(liveAmount);
    const amountText = liveAmount !== null && Number.isFinite(numericAmount) ? (side === 'SELL' ? numericAmount.toFixed(2) : String(liveAmount)) : '';
    const quantityText = side === 'BUY' ? '1.000000' : 'Seleziona posizione';
    panel.innerHTML = `<strong>${escapeHtml(side)} ${escapeHtml(item.symbol)} · conto DEMO</strong>
      ${side === 'SELL' ? `<label>Posizione da vendere<select id="manual-position"><option value="">Seleziona posizione</option>${positions.map(p => `<option value="${escapeHtml(p.position_id)}">${escapeHtml(p.position_id)} · ${escapeHtml(p.units)} unità</option>`).join('')}</select></label><label class="partial-close"><input id="manual-close-partial" type="checkbox"> Chiudi solo una parte</label><label id="manual-partial-units-wrap" hidden>Unità da chiudere<input id="manual-partial-units" type="number" min="0.000001" step="0.000001" inputmode="decimal" disabled></label><div id="manual-close-remaining" class="trade-quantity" hidden><span>Unità residue stimate</span><strong>—</strong></div><small>La chiusura totale è predefinita. La chiusura parziale deve rispettare il minimo eToro.</small>` : ''}
      <label>Importo live Demo (USD)<input id="manual-order-amount" type="number" min="0.01" step="0.01" inputmode="decimal" value="${escapeHtml(amountText)}" readonly></label>
      <div class="trade-quantity"><span>Quantità live</span><strong id="manual-order-quantity">${escapeHtml(quantityText || 'Seleziona posizione')}</strong></div>
      ${side === 'SELL' ? '<div id="manual-sale-estimate" class="trade-estimate">Seleziona la posizione per calcolare il P/L stimato.</div>' : ''}
      <small>${side === 'BUY' ? 'Importo automatico: Ask live per una unità. Non modificabile.' : 'Importo automatico: valore live della posizione selezionata. Non modificabile.'}</small>
      <button type="button" class="order-submit" ${amountText ? '' : 'disabled'}>Prepara ordine di ${side === 'BUY' ? 'acquisto' : 'vendita'} Demo</button><div id="manual-order-message" role="status"></div>`;
    const invalidate = () => {
      const hadDraft = Boolean(manualDraft);
      draftVersion++;
      manualDraft = null;
      const message = panel.querySelector('#manual-order-message');
      if (message) message.textContent = hadDraft ? 'Dati modificati: prepara un nuovo riepilogo.' : '';
    };
    const updateSellDraft = () => {
      const select = panel.querySelector('#manual-position');
      const position = positions.find(p => String(p.position_id) === String(select?.value));
      const partial = panel.querySelector('#manual-close-partial')?.checked === true;
      const unitsInput = panel.querySelector('#manual-partial-units');
      const units = partial
        ? (unitsInput?.value ? Number(unitsInput.value) : null)
        : position ? Number(position.units) : null;
      const positionUnits = position ? Number(position.units) : NaN;
      const validUnits = Boolean(position && Number.isFinite(units) && units > 0
        && Number.isFinite(positionUnits) && positionUnits > 0 && units <= positionUnits);
      const bid = Number(item.bid);
      const validBid = Number.isFinite(bid) && bid > 0;
      const value = validUnits && validBid ? bid * units : null;
      const averageRaw = position?.average_entry_price;
      const average = Number(averageRaw);
      const validAverage = averageRaw !== null && averageRaw !== undefined && averageRaw !== ''
        && Number.isFinite(average) && average > 0;
      const pnl = value !== null && validAverage ? value - average * units : null;
      const pnlPercent = validAverage && validBid ? (bid / average - 1) * 100 : null;
      const input = panel.querySelector('#manual-order-amount');
      const prepare = panel.querySelector('.order-submit');
      if (input) input.value = value !== null && Number.isFinite(value) ? value.toFixed(2) : '';
      if (prepare) {
        prepare.disabled = !validUnits;
        prepare.textContent = validBid
          ? 'Prepara riepilogo vendita Demo'
          : 'Verifica prezzo e riepiloga vendita Demo';
      }
      const estimate = panel.querySelector('#manual-sale-estimate');
      const quantity = panel.querySelector('#manual-order-quantity');
      const remaining = panel.querySelector('#manual-close-remaining');
      if (quantity) quantity.textContent = !position
        ? 'Seleziona posizione'
        : partial && !unitsInput?.value
          ? 'Inserisci le unità da chiudere'
          : validUnits ? `${units.toFixed(6)} unità` : 'Quantità non valida';
      if (remaining) {
        remaining.hidden = !(partial && validUnits);
        if (partial && validUnits) remaining.querySelector('strong').textContent = `${Math.max(0, positionUnits - units).toFixed(6)} unità`;
      }
      if (estimate) {
        if (!position) estimate.textContent = 'Seleziona una posizione valida per calcolare la vendita.';
        else if (partial && !unitsInput?.value) estimate.textContent = 'Inserisci le unità da chiudere; la chiusura parziale non precompila tutta la posizione.';
        else if (!validUnits) estimate.textContent = 'Quantità non valida o superiore alle unità della posizione; vendita bloccata.';
        else if (!validBid) estimate.textContent = 'Bid live non disponibile nella lista. Puoi richiedere il riepilogo: eToro verificherà una quotazione realtime prima di mostrarlo.';
        else if (pnl === null || pnlPercent === null) estimate.textContent = 'Prezzo live disponibile, ma P/L non calcolabile: manca un prezzo medio di carico valido.';
        else estimate.innerHTML = `Media acquisto: ${average.toFixed(2)} USD · P/L stimato: <strong class="${pnl >= 0 ? 'positive' : 'negative'}">${pnl.toFixed(2)} USD (${pnlPercent.toFixed(2)}%)</strong>`;
      }
      invalidate();
    };
    if (panel.querySelector('select')) panel.querySelector('select').onchange = updateSellDraft;
    if (panel.querySelector('#manual-close-partial')) panel.querySelector('#manual-close-partial').onchange = (event) => {
      const wrap = panel.querySelector('#manual-partial-units-wrap');
      const input = panel.querySelector('#manual-partial-units');
      if (wrap) wrap.hidden = !event.target.checked;
      if (input) { input.disabled = !event.target.checked; if (!event.target.checked) input.value = ''; }
      updateSellDraft();
    };
    if (panel.querySelector('#manual-partial-units')) panel.querySelector('#manual-partial-units').oninput = updateSellDraft;
    if (side === 'SELL') updateSellDraft();
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
          const saleAmount = panel.querySelector('#manual-order-amount').value;
          const result = await manualRequest('/api/etoro/demo/preview', {mode:'MANUAL', instrument_id:item.instrument_id,
            symbol:item.symbol, side,
            // The live server requires an amount field, but replaces SELL notional with its verified Bid × units.
            amount: side === 'SELL' && !saleAmount ? '0.01' : saleAmount,
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
          manualEditing = false;
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
  const formatCurrency = (value, currency = "USD") => {
    const parsed = numericValue(value);
    return parsed === null ? "—" : `${parsed.toLocaleString("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${escapeHtml(currency)}`;
  };
  const formatTimestamp = (value) => value ? new Date(value).toLocaleString("it-IT") : "Non disponibile";
  function sectionTimestampFor(snapshot, section) {
    const live = snapshot?.live_system_status || {};
    const news = snapshot?.news || {};
    const health = snapshot?.data_health || {};
    const liveDemo = snapshot?.live_demo_snapshot || {};
    const sectionKey = section?.dataset.section || '';
    const candidates = [];
    const push = (...values) => values.forEach(value => {
      if (value !== undefined && value !== null && value !== '') candidates.push(value);
    });

    // A live sub-panel can set its own source timestamp without changing the
    // immutable scan snapshot used by the rest of the dashboard.
    push(section?.dataset.updatedAt);
    if (section?.classList.contains('news-intelligence')) {
      push(news.scan_completed_at, news.as_of, live.news?.scan_completed_at);
    } else if (section?.classList.contains('live-system-status')) {
      push(live.observed_at, live.heartbeat?.last_activity_at);
    } else if (section?.classList.contains('broker-positions') || section?.classList.contains('etoro-portfolio')) {
      push(liveDemo.as_of, liveDemo.observed_at);
    } else if (section?.classList.contains('manual-orders-panel') || section?.classList.contains('live-instruments')) {
      push(section?.dataset.updatedAt, liveDemo.as_of, liveDemo.observed_at);
    } else if (sectionKey === 'market-data-health' || sectionKey === 'scanner-summary') {
      push(health.last_scan_at, snapshot?.as_of);
    } else if (sectionKey === 'risk-safety') {
      push(live.observed_at, live.heartbeat?.last_activity_at, snapshot?.as_of);
    } else if (sectionKey === 'overnight-activity') {
      push(live.overnight_activity?.observed_at, live.observed_at, snapshot?.as_of);
    } else {
      push(snapshot?.as_of);
    }
    return candidates[0] || new Date().toISOString();
  }
  function setSectionUpdatedAt(section, timestamp, source = 'Dati del pannello') {
    if (!section) return;
    let marker = section.querySelector(':scope > .section-updated-at');
    if (!marker) {
      marker = document.createElement('p');
      marker.className = 'section-updated-at';
      marker.setAttribute('role', 'status');
      const anchor = section.querySelector(':scope > .section-head, :scope > .portfolio-toolbar');
      if (anchor) anchor.insertAdjacentElement('afterend', marker);
      else section.insertBefore(marker, section.firstChild);
    }
    marker.dataset.timestamp = String(timestamp || '');
    marker.textContent = `${source} aggiornati: ${formatTimestamp(timestamp)}`;
    marker.title = 'Timestamp della fonte dati usata da questa sezione';
  }
  function decorateSectionTimestamps(snapshot) {
    const targets = [
      ...root.querySelectorAll('.grid > .section, .mode-panel, .etoro-portfolio, .manual-orders-panel, .live-instruments'),
    ];
    const seen = new Set();
    targets.forEach(section => {
      if (seen.has(section)) return;
      seen.add(section);
      const source = section.classList.contains('manual-orders-panel')
        ? 'Stato ordini'
        : section.classList.contains('live-instruments')
          ? 'Strumenti live'
          : section.classList.contains('etoro-portfolio') || section.classList.contains('broker-positions')
            ? 'Portafoglio'
            : 'Dati sezione';
      setSectionUpdatedAt(section, sectionTimestampFor(snapshot, section), source);
    });
  }
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

  const catalogStatusLabels = {
    BOOTSTRAPPED: 'Dati validati per lo scanner',
    ERROR_RETRYABLE: 'Acquisizione da riprovare',
    NO_DATA: 'Storico dati non disponibile',
    UNSUPPORTED: 'Non supportato dalla pipeline dati attuale',
    UNSUPPORTED_INTERNAL: 'Strumento interno eToro',
    NOT_VERIFIED: 'Stato non verificato'
  };
  function renderCatalogSearchResults() {
    const state = catalogSearchState;
    if (!state.hasSearched && !state.loading) return '';
    const warning = state.error ? `<p class="catalog-search-message error" role="status">${escapeHtml(state.error)}</p>` : '';
    const loading = state.loading ? `<p class="catalog-search-message" role="status">Ricerca nel catalogo completo in corso…</p>` : '';
    if (!state.results.length) {
      return `${warning}${loading}${state.hasSearched && !state.loading && !state.error ? '<p class="catalog-search-message">Nessuno strumento trovato. Prova con ticker, nome o ID.</p>' : ''}`;
    }
    const cards = state.results.map(item => {
      const status = String(item.bootstrap_status || 'NOT_VERIFIED').toUpperCase();
      const statusLabel = catalogStatusLabels[status] || catalogStatusLabels.NOT_VERIFIED;
      const reason = item.bootstrap_reason ? `<small>Dettaglio: ${escapeHtml(item.bootstrap_reason)}</small>` : '';
      return `<article class="catalog-result"><div class="catalog-result-identity"><strong>${escapeHtml(item.symbol || 'Ticker non disponibile')}</strong><span>${escapeHtml(item.name || 'Nome non disponibile')}</span><small>ID eToro ${escapeHtml(item.instrument_id || '—')} · ${escapeHtml(item.asset_class || 'OTHER')}</small>${reason}</div><span class="badge ${status === 'BOOTSTRAPPED' ? 'safe' : ''}">${escapeHtml(statusLabel)}</span></article>`;
    }).join('');
    const asOf = state.catalogAsOf ? `<small class="catalog-search-asof">Catalogo aggiornato al ${formatTimestamp(state.catalogAsOf)}${state.readinessAsOf ? ` · stato bootstrap ${formatTimestamp(state.readinessAsOf)}` : ''}</small>` : '';
    const more = state.hasMore ? `<button id="catalog-search-more" class="catalog-search-button secondary" type="button" ${state.loading ? 'disabled' : ''}>Carica altri risultati</button>` : '';
    return `${warning}${loading}<div class="catalog-search-summary">Trovati ${formatNumber(state.matchedCount)} strumenti · visualizzati ${formatNumber(state.results.length)}${asOf}</div><div class="catalog-search-results">${cards}</div>${more}`;
  }
  function renderCatalogSearchPanel(catalogCount, readyCount, catalogAsOf, bootstrapAsOf) {
    const catalogFreshness = catalogAsOf ? formatTimestamp(catalogAsOf) : 'non verificato';
    const bootstrapFreshness = bootstrapAsOf ? formatTimestamp(bootstrapAsOf) : 'da riallineare';
    return `<section class="section catalog-search-section"><div class="section-head"><div><span class="section-kicker">CATALOGO COMPLETO</span><h2>Cerca fra tutti gli strumenti eToro</h2></div><span class="meta">${formatNumber(catalogCount)} catalogati</span></div><p class="catalog-search-freshness">Catalogo aggiornato: ${escapeHtml(catalogFreshness)} · Bootstrap riconciliato: ${escapeHtml(bootstrapFreshness)}</p><p class="health-note">Il catalogo completo è più ampio dell’universo operativo: Aegis valuta solo gli strumenti con bootstrap e dati verificati (${formatNumber(readyCount)} pronti ora). Cerca per ticker, nome o ID anche quelli non ancora validati. Il risultato non garantisce negoziabilità o quotazione aggiornata e non inserisce l’asset nello scanner né invia ordini.</p><form id="catalog-search-form" class="catalog-search-form"><label for="catalog-search-query">Strumento da cercare</label><div class="catalog-search-controls"><input id="catalog-search-query" type="search" maxlength="80" minlength="2" autocomplete="off" placeholder="Es. Bitcoin, AAPL o ID eToro" value="${escapeHtml(catalogSearchState.query)}"><button class="catalog-search-button" type="submit" ${catalogSearchState.loading ? 'disabled' : ''}>${catalogSearchState.loading ? 'Ricerca…' : 'Cerca nel catalogo'}</button></div></form><div id="catalog-search-results" aria-live="polite">${renderCatalogSearchResults()}</div></section>`;
  }
  async function searchEtoroCatalog(query, { append = false } = {}) {
    const normalizedQuery = query.trim();
    if (normalizedQuery.length < 2 || normalizedQuery.length > 80) {
      catalogSearchState = { ...catalogSearchState, query: normalizedQuery, hasSearched: true, error: 'Inserisci da 2 a 80 caratteri per cercare.', loading: false };
      updateCatalogSearchResults();
      return;
    }
    const offset = append ? catalogSearchState.results.length : 0;
    catalogSearchState = {
      ...catalogSearchState,
      query: normalizedQuery,
      results: append ? catalogSearchState.results : [],
      matchedCount: append ? catalogSearchState.matchedCount : 0,
      hasMore: append ? catalogSearchState.hasMore : false,
      hasSearched: true,
      loading: true,
      error: ''
    };
    updateCatalogSearchResults();
    const queryAtStart = normalizedQuery;
    try {
      const params = new URLSearchParams({ q: normalizedQuery, limit: '30', offset: String(offset) });
      const response = await fetch(`/api/etoro/catalog/search?${params.toString()}`, {
        method: 'GET', headers: { Accept: 'application/json' }, cache: 'no-store'
      });
      const payload = await response.json();
      if (!response.ok || payload.status !== 'OK') throw new Error(payload.message || 'Catalogo non disponibile.');
      if (catalogSearchState.query !== queryAtStart) return;
      const rows = Array.isArray(payload.results) ? payload.results : [];
      catalogSearchState = {
        ...catalogSearchState,
        results: append ? [...catalogSearchState.results, ...rows] : rows,
        matchedCount: payload.matched_count || 0,
        hasMore: payload.has_more === true,
        catalogAsOf: payload.catalog_as_of || null,
        readinessAsOf: payload.readiness_as_of || null,
        loading: false,
        error: ''
      };
    } catch (error) {
      if (catalogSearchState.query !== queryAtStart) return;
      catalogSearchState = { ...catalogSearchState, loading: false, error: error.message || 'Ricerca non disponibile.' };
    }
    updateCatalogSearchResults();
  }
  function updateCatalogSearchResults() {
    const results = catalogSearchRoot?.querySelector('#catalog-search-results');
    if (results) results.innerHTML = renderCatalogSearchResults();
    const submit = catalogSearchRoot?.querySelector('#catalog-search-form button[type="submit"]');
    if (submit) {
      submit.disabled = catalogSearchState.loading;
      submit.textContent = catalogSearchState.loading ? 'Ricerca…' : 'Cerca nel catalogo';
    }
  }

  function renderLoading() {
    root.innerHTML = '<section class="state-panel" data-state="loading"><span class="spinner" aria-hidden="true"></span><p>Caricamento dello stato operativo...</p></section>';
  }
  function renderModePanel() {
    const manual = selectedMode === "MANUAL";
    return `<section class="section mode-panel" data-section="operating-mode">
      <div class="section-head"><div><span class="section-kicker">MODALITÀ</span><h2>Come vuoi operare?</h2></div><span class="badge ${manual ? "" : "safe"}">${manual ? "MANUALE" : "AEGIS AI"}</span></div>
      <div class="mode-switch" role="group" aria-label="Operating mode">
        <button type="button" class="mode-choice ${!manual ? "active" : ""}" data-mode="AI">Aegis Invest AI<span>Analisi, rischio e dimensionamento</span></button>
        <button type="button" class="mode-choice ${manual ? "active" : ""}" data-mode="MANUAL">Vista manuale eToro<span>Scegli personalmente lo strumento</span></button>
      </div>
      <div class="mode-description ${manual ? "manual" : "ai"}">
        <strong>${manual ? "Modalità manuale selezionata" : "Modalità Aegis AI selezionata"}</strong>
        <span>${manual ? "Gli ordini manuali restano separati: non entrano in P/L, chiusure, win rate o statistiche di Aegis." : "Aegis prepara opportunità, rischio e dimensionamento. Da questa schermata non viene inviato alcun ordine automaticamente."}</span>
      </div>
      ${manual ? `<div class="manual-preview"><div><span class="label">Conto Demo eToro</span><strong id="live-demo-status">Caricamento conto live...</strong></div><span class="badge" id="live-demo-badge">LETTURA LIVE</span><p id="live-demo-detail">Lettura di saldo e posizioni da eToro. Nessun controllo d’ordine è attivo.</p></div>
        <section class="etoro-portfolio" aria-labelledby="manual-portfolio-title">
          <details id="portfolio-positions-disclosure" class="portfolio-disclosure"><summary><span>Portafoglio e posizioni</span><small>Vista Demo · Live eToro</small></summary>
          <div class="portfolio-toolbar"><div><span class="section-kicker">PORTAFOGLIO DEMO</span><h3 id="manual-portfolio-title">Portafoglio</h3></div><span class="portfolio-live"><i></i> Live eToro</span></div>
          <nav class="portfolio-tabs" aria-label="Sezioni portafoglio"><button type="button" class="active" data-portfolio-tab="portfolio-positions-disclosure" aria-controls="portfolio-positions-disclosure" aria-current="page">Posizioni</button><button type="button" data-portfolio-tab="portfolio-orders-disclosure" aria-controls="portfolio-orders-disclosure">Ordini</button><button type="button" data-portfolio-tab="live-instruments-panel" aria-controls="live-instruments-panel">Operazioni manuali</button><button type="button" data-portfolio-tab="live-instruments-panel" aria-controls="live-instruments-panel">Crypto</button></nav>
          <div class="portfolio-summary" id="manual-portfolio-summary"><div><span>Cash disponibile</span><strong>—</strong></div><div><span>Valore portafoglio</span><strong>—</strong></div><div><span>P/L conto Demo (totale)</span><strong>—</strong></div><div><span>Posizioni aperte</span><strong>—</strong></div><div><span>Posizioni in perdita</span><strong>—</strong></div><div><span>Posizioni in guadagno</span><strong>—</strong></div><div><span>P/L posizioni in perdita</span><strong class="negative">—</strong></div><div><span>P/L posizioni in guadagno</span><strong class="positive">—</strong></div></div>
          <div class="portfolio-table"><div class="portfolio-table-head"><span>Asset</span><span>Prezzo live</span><span>Unità</span><span>Media apertura</span><span>P/L</span><span>Valore netto</span><span>Azioni</span></div><div id="manual-portfolio-list" class="portfolio-table-body"><div class="portfolio-empty">Caricamento posizioni live…</div></div></div></details>
          <details id="portfolio-orders-disclosure" class="portfolio-disclosure orders-disclosure"><summary><span>Ordini e verifiche</span><small id="manual-orders-summary">Controllo stato ordini Demo</small></summary><section class="manual-orders-panel" aria-labelledby="manual-orders-title"><div class="section-head"><div><span class="section-kicker">STATO ORDINI DEMO</span><h3 id="manual-orders-title">Ordini e asset in verifica</h3></div><span class="badge" id="manual-orders-badge">Caricamento…</span></div><div id="manual-orders-list" class="manual-orders-list"><div class="portfolio-empty">Controllo gli ordini locali…</div></div></section></details>
        </section>` : ""}
    </section>`;
  }
  function bindModePanel() {
    root.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
      if (manualBusy) return;
      selectedMode = button.dataset.mode;
      if (selectedMode !== "MANUAL") manualEditing = false;
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
      if (!response.ok || !['LOCAL_ORDER_LEDGER', 'LOCAL_AND_AEGIS_ORDER_LEDGER'].includes(payload.status)) throw new Error('unavailable');
      setSectionUpdatedAt(document.querySelector('.manual-orders-panel'), payload.observed_at || payload.as_of || new Date().toISOString(), 'Stato ordini');
      const orders = Array.isArray(payload.orders) ? payload.orders : [];
      const pendingStates = new Set(['SENDING', 'UNKNOWN', 'SUBMITTED', 'UNCONFIRMED', 'PENDING', 'PARTIALLY_FILLED']);
      const pending = orders.filter(order => pendingStates.has(order.status));
      const aegisPending = orders.filter(order => order.source === 'AEGIS_RUNTIME' && pendingStates.has(order.status));
      const lookupNotFound = order => Number(order.broker_lookup_http_status) === 404;
      const staleCutoff = Date.now() - 24 * 60 * 60 * 1000;
      const stalePending = pending.filter(order => {
        const created = Date.parse(order.created_at || '');
        return Number.isFinite(created) && created < staleCutoff;
      });
      const staleLookupMissing = stalePending.filter(lookupNotFound);
      const staleOther = stalePending.filter(order => !lookupNotFound(order));
      if (badge) badge.textContent = aegisPending.length ? `${aegisPending.length} Aegis non confermati` : (staleLookupMissing.length && staleLookupMissing.length === pending.length ? `${staleLookupMissing.length} storici · esito ignoto` : (pending.length ? `${pending.length} da verificare` : 'Nessuno da verificare'));
      const ordersSummary = document.getElementById('manual-orders-summary');
      if (ordersSummary) ordersSummary.textContent = aegisPending.length ? `${aegisPending.length} Aegis non confermati · ${formatCurrency(payload.aegis_unresolved_capital_eur, 'EUR')} bloccati` : (staleLookupMissing.length && staleLookupMissing.length === pending.length ? `${staleLookupMissing.length} ordini storici · eToro HTTP 404 · esito non confermato` : (pending.length ? `${pending.length} da verificare · storico separato` : 'Nessun ordine in sospeso'));
      if (!orders.length) { list.innerHTML = '<div class="portfolio-empty">Nessun ordine Demo registrato.</div>'; return; }
      const sortedOrders = [...orders].sort((a, b) => (a.source === 'AEGIS_RUNTIME' ? -1 : 1) - (b.source === 'AEGIS_RUNTIME' ? -1 : 1) || String(b.created_at || '').localeCompare(String(a.created_at || '')));
      list.innerHTML = `${aegisPending.length ? `<div class="orders-notice"><strong>${aegisPending.length} ordini Aegis non confermati</strong><span>eToro non ha restituito un esito esplicito per questi ID. Non sono considerati eseguiti né rifiutati e non verranno reinviati automaticamente.</span></div>` : ''}${staleLookupMissing.length ? `<div class="orders-notice"><strong>${staleLookupMissing.length} ordini storici: ID non trovato (HTTP 404)</strong><span>eToro non rende disponibile lo stato di questi ordini. Restano non confermati; non significa né eseguito né rifiutato. Non reinviare. La posizione originaria va confrontata con il portafoglio Demo.</span></div>` : ''}${staleOther.length ? `<div class="orders-notice"><strong>${staleOther.length} ordine/i precedente/i</strong><span>Non sono nuovi ordini: sono richieste rimaste nello storico eToro. Verifica l’esito solo se ti serve riconciliare il passato.</span></div>` : ''}${sortedOrders.map(order => {
        const isPending = pendingStates.has(order.status);
        const isAegis = order.source === 'AEGIS_RUNTIME';
        const created = Date.parse(order.created_at || '');
        const isStale = isPending && Number.isFinite(created) && created < staleCutoff;
        const isLookupNotFound = lookupNotFound(order);
        const label = order.side === 'SELL' ? 'VENDITA' : 'ACQUISTO';
        const details = [order.amount ? `${formatNumber(order.amount)} ${escapeHtml(order.currency || 'USD')}` : '', order.units ? `${formatNumber(order.units)} unità` : '', order.broker_order_id ? `ID eToro ${escapeHtml(order.broker_order_id)}` : ''].filter(Boolean).join(' · ');
        const action = isPending && !isAegis ? `<button type="button" class="order-status" data-order-id="${escapeHtml(order.preview_id)}">Verifica esito</button>` : '';
        const status = String(order.status || 'UNKNOWN').toUpperCase();
        const symbol = String(order.symbol || '').toUpperCase();
        const name = assetNames[symbol] || symbol || 'Asset non specificato';
        const visualStatus = isLookupNotFound ? 'ID NON TROVATO · ESITO IGNOTO' : (isStale ? 'STORICO · ESITO DA VERIFICARE' : manualStatusLabel(status));
        const source = isAegis
          ? '<small class="order-source">AEGIS · riconciliazione broker</small>'
          : '<small class="order-source">MANUALE · escluso dalle statistiche Aegis</small>';
        const message = order.message ? `<small class="order-message">${escapeHtml(order.message)}</small>` : '';
        const orderTime = order.created_at ? ` · ${formatTimestamp(order.created_at)}` : (isAegis ? ' · data storica non registrata' : '');
        return `<div class="manual-order-row ${isPending ? 'pending' : ''} ${isStale ? 'stale' : ''}"><div><strong>${label} ${escapeHtml(symbol || '—')}</strong><small>${escapeHtml(name)} · ${details || 'Riepilogo locale'}${orderTime}</small>${source}${message}</div><span class="badge ${isPending ? '' : 'safe'}">${escapeHtml(isAegis ? 'NON CONFERMATO · NON REINVIABILE' : visualStatus)}</span>${action}</div>`;
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
      setSectionUpdatedAt(document.querySelector('.live-instruments'), payload.observed_at || new Date().toISOString(), 'Strumenti live');
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
      setSectionUpdatedAt(document.querySelector('.etoro-portfolio'), payload.as_of || payload.observed_at || new Date().toISOString(), 'Portafoglio');
      setSectionUpdatedAt(document.querySelector('.mode-panel'), payload.as_of || payload.observed_at || new Date().toISOString(), 'Conto Demo');
      demoPositions = Array.isArray(payload.positions) ? payload.positions : [];
      demoCurrency = payload.currency || "USD";
      const positionPnl = summarizePositionPnl(demoPositions);
      if (status) status.textContent = `${formatNumber(payload.cash)} ${escapeHtml(payload.currency)} cash`;
      if (detail) detail.textContent = `${Array.isArray(payload.positions) ? payload.positions.length : 0} open positions · live as of ${formatTimestamp(payload.as_of)} · read-only refresh (0 broker writes)`;
      const summary = document.getElementById('manual-portfolio-summary');
      const breakdownValue = value => positionPnl.complete ? formatCurrency(value, demoCurrency) : 'Non disponibile';
      const breakdownCount = value => positionPnl.complete ? formatNumber(value) : 'Non disponibile';
      if (summary) summary.innerHTML = `<div><span>Cash disponibile</span><strong>${formatCurrency(payload.cash, demoCurrency)}</strong></div><div><span>Valore portafoglio</span><strong>${formatCurrency(payload.total_value, demoCurrency)}</strong></div><div><span>P/L conto Demo (totale)</span><strong class="${numericValue(payload.current_pnl) >= 0 ? 'positive' : 'negative'}">${formatCurrency(payload.current_pnl, demoCurrency)}</strong></div><div><span>Posizioni aperte</span><strong>${formatNumber(positionPnl.count)}</strong></div><div><span>Posizioni in perdita</span><strong>${breakdownCount(positionPnl.loss_count)}</strong></div><div><span>Posizioni in guadagno</span><strong>${breakdownCount(positionPnl.gain_count)}</strong></div><div><span>P/L posizioni in perdita</span><strong class="${positionPnl.complete && positionPnl.loss < 0 ? 'negative' : ''}">${breakdownValue(positionPnl.loss)}</strong></div><div><span>P/L posizioni in guadagno</span><strong class="${positionPnl.complete && positionPnl.gain > 0 ? 'positive' : ''}">${breakdownValue(positionPnl.gain)}</strong></div>`;
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
      const optionalNumber = (value) => value === null || value === undefined || value === '' ? null : numericValue(value);
      const price = optionalNumber(position.current_price) ?? optionalNumber(item?.bid);
      const units = optionalNumber(position.units);
      const average = optionalNumber(position.average_entry_price);
      const value = optionalNumber(position.current_value) ?? (price !== null && units !== null ? price * units : null);
      const pnl = optionalNumber(position.unrealized_pnl) ?? (price !== null && units !== null && average !== null ? (price - average) * units : null);
      const pnlClass = pnl === null ? '' : pnl >= 0 ? 'positive' : 'negative';
      const symbol = position.symbol || item?.symbol || instrumentNamesById[String(position.instrument_id)] || `ID ${position.instrument_id}`;
      const name = position.description || position.name || item?.description || item?.name || assetNames[String(symbol).toUpperCase()] || 'Nome non disponibile';
      return `<div class="portfolio-table-row"><span class="portfolio-asset"><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(name)} · ${escapeHtml(position.direction)}</small></span><span data-label="Prezzo live">${formatNumber(price)}</span><span data-label="Unità">${formatNumber(position.units)}</span><span data-label="Media apertura">${formatNumber(position.average_entry_price)}</span><span data-label="P/L" class="${pnlClass}">${pnl === null ? '—' : formatCurrency(pnl, demoCurrency)}</span><span data-label="Valore netto">${value === null ? '—' : formatCurrency(value, demoCurrency)}</span><span class="portfolio-row-actions" data-label="Azioni"><button type="button" class="order-button buy" data-order-side="BUY" data-order-symbol="${escapeHtml(symbol)}" data-order-instrument-id="${escapeHtml(position.instrument_id)}">ACQUISTA</button><button type="button" class="order-button sell" data-order-side="SELL" data-order-symbol="${escapeHtml(symbol)}" data-order-instrument-id="${escapeHtml(position.instrument_id)}">VENDI</button></span></div>`;
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
  const assetTypeLabels = {
    EQUITY: 'Azione',
    ETF: 'ETF',
    CRYPTO: 'Crypto',
    FOREX: 'Forex',
    INDEX: 'Indice',
    COMMODITY: 'Commodity',
    OTHER: 'Altro',
  };
  function displayAssetType(item) {
    const raw = String(item?.asset_class || '').toUpperCase();
    return assetTypeLabels[raw] || (raw ? raw : 'Da verificare');
  }
  function displayAssetName(item) {
    const symbol = String(item?.symbol || '').toUpperCase();
    const description = item?.description || item?.full_asset_name || item?.full_name || item?.name;
    const base = description && description !== item.symbol ? description : (assetNames[symbol] || symbol || 'Nome non disponibile');
    const type = displayAssetType(item);
    return base.includes(`· ${type}`) ? base : `${base} · ${type}`;
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
        ['Descrizione strumento', displayAssetName(item)], ['Classe', item.asset_class], ['Score', item.score],
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
  function renderPositions(items, monitoredCount = currentSnapshot?.positions?.open_count) {
    if (!items.length && monitoredCount == null) return renderEmpty("MONITORAGGIO AEGIS NON DISPONIBILE", "Il riepilogo del ciclo non riporta posizioni seguite da Aegis. Le posizioni effettive del conto Demo sono mostrate separatamente.");
    if (!items.length) return renderEmpty("NESSUN ASSET NEL MONITORAGGIO AEGIS", "L’ultimo ciclo non ha posizioni da seguire. Questo non significa che il portafoglio Demo sia vuoto.");
    return `<div class="list observed-list">${items.map((item) => {
      const details = observationDetailRows(item, [
        ['Descrizione strumento', displayAssetName(item)], ['Classe', item.asset_class], ['Stato posizione', item.state],
        ['Valore / prezzo corrente', item.value], ['Score', item.score], ['Confidence', item.confidence],
        ['Azione', item.action], ['Timeframe', item.timeframe], ['Ciclo osservato', formatTimestamp(item.scan_cycle_timestamp)],
        ['Barra osservata', formatTimestamp(item.bar_timestamp)], ['Qualità dati', item.data_quality],
        ['Provenienza dati', item.provider_provenance], ['Freshness', item.freshness],
        ['Sessione mercato', item.market_session_state], ['Confrontabile per ingresso', item.eligible_for_entry_comparison],
        ['Motivo eleggibilità', item.eligibility_reason_code], ['Mercato', item.current_market_state],
        ['Fattori di rischio', item.risk_flags],
      ]);
      return `<details class="observation-disclosure"><summary><span class="observation-summary"><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(displayAssetName(item))}</small></span><span class="meta">${escapeHtml(item.state)}</span><span class="badge safe">Osservata</span></summary><dl class="observation-details">${details || '<div><dt>Dettagli</dt><dd>Non disponibili per questa osservazione.</dd></div>'}</dl></details>`;
    }).join("")}</div>`;
  }
  function renderBrokerPositions(liveDemoSnapshot) {
    const live = liveDemoSnapshot?.status === "LIVE_READ_ONLY";
    const positions = live && Array.isArray(liveDemoSnapshot.positions) ? liveDemoSnapshot.positions : [];
    const currency = live ? (liveDemoSnapshot.currency || "USD") : "USD";
    const asOf = live ? formatTimestamp(liveDemoSnapshot.as_of) : null;
    let content;
    if (!live) {
      const reason = liveDemoSnapshot?.error || liveDemoSnapshot?.status || "lettura non completata";
      content = renderEmpty("PORTAFOGLIO DEMO NON VERIFICABILE", "La lettura live eToro non è riuscita (" + reason + "). Il riepilogo Aegis non viene usato come sostituto.");
    } else if (!positions.length) {
      content = renderEmpty("NESSUNA POSIZIONE APERTA", "Lettura live eToro completata: il conto Demo non riporta posizioni aperte.");
    } else {
      content = '<div class="list observed-list">' + positions.map((item) => {
        const pnl = numericValue(item.unrealized_pnl);
        const pnlClass = pnl === null ? "" : pnl >= 0 ? "positive" : "negative";
        const details = observationDetailRows(item, [
          ["Descrizione strumento", item.description || item.name || item.symbol],
          ["Direzione", item.direction],
          ["Unità", item.units],
          ["Prezzo medio apertura", item.average_entry_price == null ? null : formatCurrency(item.average_entry_price, currency)],
          ["Prezzo corrente eToro", item.current_price == null ? null : formatCurrency(item.current_price, currency)],
          ["Valore netto", item.current_value == null ? null : formatCurrency(item.current_value, currency)],
          ["Importo investito", item.invested_amount == null ? null : formatCurrency(item.invested_amount, currency)],
          ["P/L non realizzato", item.unrealized_pnl == null ? null : formatCurrency(item.unrealized_pnl, currency)],
          ["ID posizione broker", item.position_id],
        ]);
        const value = item.current_value == null ? "Valore n/d" : formatCurrency(item.current_value, currency);
        const pnlText = pnl === null ? "P/L n/d" : "P/L " + formatCurrency(pnl, currency);
        const symbol = item.symbol || ("ID " + item.instrument_id);
        const name = item.description || item.name || item.symbol || "Nome non disponibile";
        return '<details class="observation-disclosure broker-position" data-pnl-state="' + (pnl === null ? 'unknown' : pnl < 0 ? 'loss' : pnl > 0 ? 'gain' : 'flat') + '"><summary><span class="observation-summary"><strong>' +
          escapeHtml(symbol) + '</strong><small>' + escapeHtml(name) +
          '</small></span><span class="meta">' + escapeHtml(value) + ' · <span class="' + pnlClass + '">' +
          escapeHtml(pnlText) + '</span></span><span class="badge safe">' +
          escapeHtml(item.direction || "APERTA") + ' · ' + formatNumber(item.units) +
          ' unità</span></summary><dl class="observation-details">' +
          (details || '<div><dt>Dettagli</dt><dd>Non disponibili dal broker.</dd></div>') +
          '</dl></details>';
      }).join("") + "</div>";
    }
    const badge = live ? positions.length + " · LIVE" : "DA VERIFICARE";
    const note = live
      ? "Dati eToro aggiornati al " + asOf + ". Separati dal monitoraggio strategico Aegis; non sono considerate riconciliate finché il registro Aegis non le abbina."
      : "Quando il broker non risponde, non mostriamo dati storici come se fossero attuali.";
    const positionPnlSummary = summarizePositionPnl(positions);
    const accountPnl = live ? numericValue(liveDemoSnapshot.current_pnl) : null;
    const positionPnlTotal = positions.length > 0 && positionPnlSummary.complete
      ? positionPnlSummary.loss + positionPnlSummary.gain
      : null;
    const pnlDifference = accountPnl !== null && positionPnlTotal !== null
      ? Math.abs(positionPnlTotal - accountPnl)
      : null;
    const reconciliation = liveDemoSnapshot?.pnl_reconciliation || {};
    const pnlWarning = pnlDifference !== null && pnlDifference > 0.01
      ? '<p class="health-note attention">P/L conto e P/L delle posizioni aperte hanno ambiti diversi: scarto ' +
        formatCurrency(pnlDifference, currency) + '. Il conto può includere risultati realizzati, costi o rettifiche; non è un errore di somma.</p>'
      : "";
    return '<section class="section broker-positions" data-section="broker-positions"><div class="section-head"><div><span class="section-kicker">PORTAFOGLIO BROKER · SOLA LETTURA</span><h2>Posizioni aperte eToro Demo</h2></div><span class="badge ' +
      (live ? "safe" : "") + '">' + badge + "</span></div>" + content +
      '<p class="health-note">' + escapeHtml(note) + "</p>" + pnlWarning + "</section>";
  }

  function renderPerformanceReview(snapshot) {
    const review = snapshot?.performance_review;
    if (!review) return '';
    const currency = review.currency || snapshot.currency;
    const amount = value => value == null ? 'Non verificato' : formatCurrency(value, currency);
    const coverage = `${formatNumber(review.verified_closed_count)} / ${formatNumber(review.closed_count)}`;
    const history = Array.isArray(review.realized_history) ? review.realized_history : [];
    const contested = Array.isArray(review.contested_history) ? review.contested_history : [];
    const orders = Array.isArray(review.order_history) ? review.order_history : [];
    const auditStatus = review.closure_audit_status === 'ALL_CERTIFIED'
      ? 'TUTTE CERTIFICATE'
      : review.closure_audit_status === 'PARTIAL_CERTIFICATION'
        ? 'CERTIFICAZIONE PARZIALE'
        : 'NON VERIFICATO';
    const contestedMarkup = contested.length
      ? `<details class="pnl-history contested-history"><summary>Chiusure contestate/non verificabili · ${formatNumber(contested.length)}</summary><div class="pnl-history-list">${contested.map((row) => `<div class="pnl-history-row closure-history-row"><div><strong>${escapeHtml(row.symbol || '—')}</strong><small>${escapeHtml(displayAssetType(row))}</small></div><div><strong>Stato</strong><small class="negative">Non certificata</small><small>${escapeHtml((row.exclusion_reasons || []).join(' · '))}</small></div><div><strong>Data</strong><small>${escapeHtml(formatTimestamp(row.closed_at))}</small><span class="${numericValue(row.realized_pnl) >= 0 ? 'positive' : 'negative'}">P/L registrato: ${escapeHtml(amount(row.realized_pnl))}</span></div><small class="closure-history-note">${escapeHtml(row.reason || 'Richiede revisione manuale')}</small></div>`).join('')}</div></details>`
      : '<p class="health-note">Nessuna chiusura contestata o non verificabile nel registro corrente.</p>';
    const auditMarkup = `<div class="closure-audit"><div class="closure-audit-head"><div><span class="section-kicker">AUDIT CHIUSURE</span><h4>Chiusure certificate e P/L ricalcolato</h4></div><span class="badge ${review.closure_audit_status === 'ALL_CERTIFIED' ? 'safe' : 'attention'}">${auditStatus}</span></div><div class="metrics closure-audit-metrics"><div class="metric"><span class="label">Certificate</span><span class="value">${formatNumber(review.certified_closed_count ?? review.verified_closed_count)}</span></div><div class="metric"><span class="label">Contestate / non verificabili</span><span class="value">${formatNumber(review.contested_closed_count ?? 0)}</span></div><div class="metric"><span class="label">P/L certificato</span><span class="value">${amount(review.certified_realized_pnl ?? review.realized_pnl)}</span></div><div class="metric"><span class="label">P/L ricalcolato · sole certificate</span><span class="value">${amount(review.adjusted_realized_pnl)}</span></div></div><p class="health-note">${escapeHtml(review.closure_audit_note || 'Il ricalcolo usa solo evidenze riconciliate con il broker.')}</p>${contestedMarkup}</div>`;
    const historyMarkup = history.length
      ? `<details class="pnl-history"><summary>Storico P/L · ${formatNumber(history.length)} chiusure verificate</summary><div class="pnl-history-list">${history.map((row) => {
        const pnl = numericValue(row.realized_pnl);
        const returnPct = numericValue(row.realized_return_pct);
        const pnlClass = pnl === null ? '' : pnl >= 0 ? 'positive' : 'negative';
        const pnlText = pnl === null ? 'Non verificato' : formatCurrency(pnl, currency);
        const returnText = returnPct === null ? 'rendimento n/d' : `rendimento ${formatNumber(returnPct * 100)}%`;
        const purchaseDate = row.opened_at ? formatTimestamp(row.opened_at) : 'Data non disponibile';
        const purchaseNote = row.opened_at_basis === 'SUBMISSION' ? ' · invio ordine' : row.opened_at_basis === 'CONFIRMATION' && row.opened_at ? ' · conferma broker' : '';
        const saleDate = row.closed_at ? formatTimestamp(row.closed_at) : 'Data non disponibile';
        const saleNote = row.closed_at_basis === 'CONFIRMATION' ? ' · conferma broker' : '';
        return `<div class="pnl-history-row closure-history-row"><div><strong>${escapeHtml(row.symbol || '—')}</strong><small>${escapeHtml(displayAssetType(row))}</small></div><div><strong>Acquisto</strong><small>${escapeHtml(purchaseDate + purchaseNote)}</small><small>Importo eseguito: ${escapeHtml(amount(row.purchase_amount))}</small></div><div><strong>Vendita</strong><small>${escapeHtml(saleDate + saleNote)}</small><span class="${pnlClass}">P/L: ${escapeHtml(pnlText)}</span></div><small class="closure-history-note">${escapeHtml(returnText)} · ${escapeHtml(row.reason || 'Chiusura verificata')}</small></div>`;
      }).join('')}</div></details>`
      : '<p class="health-note">Nessuna chiusura verificata disponibile.</p>';
    const ordersMarkup = orders.length
      ? `<details class="pnl-history order-history"><summary>Ordini effettuati · ${formatNumber(orders.length)} eseguiti</summary><div class="pnl-history-list">${orders.map((row) => {
        const orderAmount = numericValue(row.amount);
        const amountText = orderAmount === null ? 'importo n/d' : formatCurrency(orderAmount, row.currency || currency);
        return `<div class="pnl-history-row"><div><strong>${escapeHtml(row.symbol || '—')}</strong><small>${escapeHtml(displayAssetType(row))} · ordine ${escapeHtml(row.order_id || 'n/d')}</small></div><span class="positive">${escapeHtml(row.status || 'FILLED')}</span><small>${escapeHtml(amountText)} · ${escapeHtml(formatTimestamp(row.executed_at))}</small></div>`;
      }).join('')}</div></details>`
      : '<p class="health-note">Nessun ordine eseguito verificato disponibile.</p>';
    return `<div class="performance-review"><h3>Risultati Aegis · Demo</h3>
      <div class="metrics">
        <div class="metric"><span class="label">P/L realizzato · chiusure verificate ${escapeHtml(coverage)}</span><span class="value">${amount(review.realized_pnl)}</span></div>
        <div class="metric"><span class="label">P/L non realizzato · posizioni Aegis</span><span class="value">${amount(review.unrealized_pnl)}</span></div>
        <div class="metric"><span class="label">P/L complessivo · posizioni riconciliate</span><span class="value">${amount(review.combined_pnl)}</span></div>
        <div class="metric"><span class="label">Valutazione chiusure verificate</span><span class="value">${review.realized_trade_count || 0} · ${review.evaluation === 'POSITIVE_BUT_PRELIMINARY' ? 'positiva preliminare' : review.evaluation === 'POSITIVE_PRELIMINARY' ? 'positiva' : 'campione insufficiente'}</span></div>
        <div class="metric"><span class="label">Win rate · profit factor realizzati</span><span class="value">${review.realized_win_rate == null ? 'Non verificato' : `${formatNumber(review.realized_win_rate)}% · ${review.realized_profit_factor == null ? 'n/d' : formatNumber(review.realized_profit_factor)}`}</span></div>
      </div>
      ${auditMarkup}
      ${historyMarkup}
      ${ordersMarkup}
      <p class="health-note">${review.status === 'AVAILABLE' ? 'Importi attribuiti alle posizioni del registro Aegis.' : 'Riconciliazione incompleta: i totali non verificati non vengono stimati.'} Rendimento dall’avvio, perdita massima storica e confronto con il mercato: non ancora validati. Il capitale autorizzato non è una base di rendimento.</p>
    </div>`;
  }

  const externalBenchmarkProfiles = [
    { name: 'TradingInvest890', owner: 'Massimiliano Spallanzani', detail: '92 chiusure · 294 giorni medi · leva 1,1× · 99% long', source: 'Fonte esterna · da verificare', href: 'https://www.etoro.com/people/TradingInvest890/stats' },
    { name: 'ThomasPJ', owner: 'Thomas Parry Jones', detail: 'Profilo eToro multi-asset · dati da acquisire', source: 'API eToro · da verificare', href: 'https://www.etoro.com/people/thomaspj/stats' },
    { name: 'RainbirdFX', owner: 'Profilo eToro multi-strategy', detail: 'Profilo eToro · dati da acquisire', source: 'API eToro · da verificare', href: 'https://www.etoro.com/people/rainbirdfx/stats' },
    { name: 'SimoneRizzetto88', owner: 'Profilo eToro', detail: 'Candidato al gruppo di confronto', source: 'Da verificare', href: 'https://www.etoro.com/people/SimoneRizzetto88/stats' },
    { name: 'Sergius95', owner: 'Profilo eToro', detail: 'Candidato al gruppo di confronto', source: 'Da verificare', href: 'https://www.etoro.com/people/Sergius95/stats' },
    { name: 'iBore99', owner: 'Profilo eToro', detail: 'Candidato al gruppo di confronto', source: 'Da verificare', href: 'https://www.etoro.com/people/iBore99/stats' },
    { name: 'pino428', owner: 'Profilo eToro', detail: 'Candidato al gruppo di confronto', source: 'Da verificare', href: 'https://www.etoro.com/people/pino428/stats' },
    { name: 'Kevin_Pando', owner: 'Profilo eToro', detail: 'Candidato al gruppo di confronto', source: 'Da verificare', href: 'https://www.etoro.com/people/Kevin_Pando/stats' },
  ];

  function renderBenchmarkPanel(snapshot) {
    const review = snapshot?.performance_review || {};
    const verified = review.verified_closed_count ?? 0;
    const aegisDetail = `${formatNumber(verified)} chiusure verificate · campione iniziale`;
    const benchmark = currentBenchmarkSnapshot || snapshot?.benchmark || {};
    const liveProfiles = Array.isArray(benchmark.profiles) ? benchmark.profiles : [];
    const profiles = liveProfiles.length ? liveProfiles : externalBenchmarkProfiles.map(row => ({
      username: row.name, owner: row.owner, href: row.href, status: 'NOT_VERIFIED', source: 'DA_VERIFICARE', detail: row.detail,
    }));
    const rows = [{ username: 'AEGIS Demo', owner: 'Sistema in valutazione', detail: aegisDetail, source: 'Dati interni verificati', status: 'AVAILABLE', href: '' }, ...profiles];
    const status = benchmark.status;
    const intro = status === 'AVAILABLE'
      ? 'Dati pubblici letti tramite API eToro in sola lettura. Il confronto non influenza selezione, RiskManager o ordini.'
      : 'Le fonti esterne non sono ancora verificate tramite API eToro. Nessun valore esterno viene usato per Aegis.';
    const detailFor = row => {
      if (row.username === 'AEGIS Demo') return row.detail;
      if (row.status !== 'AVAILABLE') return row.message || row.detail || 'Dati eToro non disponibili';
      const gain = row.latest_monthly_gain?.gain;
      const gainText = gain == null ? 'rendimento mensile n/d' : `ultimo mese ${formatNumber(gain)}%`;
      const copiers = row.copiers == null ? 'copiatori n/d' : `${formatNumber(row.copiers)} copiatori`;
      const risk = row.risk_score == null ? 'rischio n/d' : `rischio ${formatNumber(row.risk_score)}`;
      return `${gainText} · ${copiers} · ${risk}`;
    };
    const sourceLabel = row => row.username === 'AEGIS Demo'
      ? row.source
      : row.status === 'AVAILABLE' ? 'Dati eToro verificati' : 'Da verificare';
    return `<section class="section benchmark-panel" data-section="benchmark-panel"><div class="section-head"><div><span class="section-kicker">BENCHMARK</span><h2>Aegis contro investitori eToro</h2></div><span class="badge">Solo confronto</span></div><p class="health-note">${escapeHtml(intro)}</p><div class="benchmark-table">${rows.map(row => `<div class="benchmark-row"><div class="benchmark-identity"><strong>${escapeHtml(row.username || row.name)}</strong><small>${escapeHtml(row.owner || 'Profilo eToro')}</small></div><span class="benchmark-detail">${escapeHtml(detailFor(row))}</span><span class="badge ${row.username === 'AEGIS Demo' ? 'safe' : row.status === 'AVAILABLE' ? 'safe' : ''}">${escapeHtml(sourceLabel(row))}</span>${row.href ? `<a class="benchmark-link" href="${row.href}" target="_blank" rel="noreferrer">Apri profilo</a>` : '<span class="benchmark-link benchmark-link-muted">Profilo interno</span>'}</div>`).join('')}</div><p class="health-note">Metriche da confrontare: rendimento sullo stesso periodo, drawdown, rischio, consistenza mensile, numero e durata delle operazioni, leva e composizione multi-asset.</p></section>`;
  }

  function summarizePositionPnl(positions) {
    const rows = Array.isArray(positions) ? positions : [];
    const values = rows.map(item => {
      const raw = item?.unrealized_pnl;
      return raw === null || raw === undefined || raw === '' ? null : numericValue(raw);
    });
    const complete = values.every(value => value !== null);
    const losses = complete ? values.filter(value => value < 0) : [];
    const gains = complete ? values.filter(value => value > 0) : [];
    return {
      count: rows.length,
      complete,
      loss_count: complete ? losses.length : null,
      gain_count: complete ? gains.length : null,
      loss: complete ? losses.reduce((total, value) => total + value, 0) : null,
      gain: complete ? gains.reduce((total, value) => total + value, 0) : null,
    };
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
    if (liveTopCount > 0 && cycle.state !== "NO_CYCLE") return { label: "CANDIDATI TOP", detail: "Sono candidati del ranking, non ordini approvati: RiskManager, preflight e controlli broker devono ancora passare." };
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
      return `<section class="section top-opportunities" data-section="top-opportunities"><div class="section-head"><div><span class="section-kicker">HISTORICAL — LAST COMPLETED SCAN</span><h2>${formatNumber(count)} candidati TOP</h2></div><span class="badge safe">${formatTimestamp(scanTimestamp)}</span></div><p class="health-note">Archivio, non opportunità live e non ordini approvati.</p><p class="health-note">L’ultima scansione espone il conteggio, ma non i dettagli dei candidati.</p></section>`;
    }
    return `<section class="section top-opportunities" data-section="top-opportunities"><div class="section-head"><div><span class="section-kicker">HISTORICAL — LAST COMPLETED SCAN</span><h2>${formatNumber(count)} candidati TOP</h2></div><span class="badge safe">${formatTimestamp(scanTimestamp)}</span></div><p class="health-note">Archivio del ranking, non opportunità live e non ordini approvati.</p><div class="list observed-list">${items.map((item) => {
      const details = observationDetailRows(item, [
        ['Descrizione strumento', displayAssetName(item)], ['Classe', item.asset_class],
        ['Score', item.opportunity_score ?? item.score], ['Rank', item.rank], ['Confidence', item.confidence],
        ['Azione', item.action], ['Timeframe', item.timeframe], ['Mercato', item.current_market_state],
        ['Qualità dati', item.data_quality], ['Freshness', item.freshness], ['Provenienza dati', item.provider_provenance],
        ['Sentiment news', item.news_sentiment], ['Eventi materiali', item.material_event_count],
        ['Fattori / motivi', item.reasons || item.opportunity_factors || item.rejection_reasons],
        ['Rischi news', item.news_risk_flags], ['Fattori di rischio', item.risk_flags],
      ]);
      return `<details class="observation-disclosure top-opportunity"><summary><span class="observation-summary"><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(displayAssetName(item))}</small></span><span class="meta">Score ${formatNumber(item.opportunity_score ?? item.score)} · Rank ${formatNumber(item.rank)}</span><span class="badge safe">Dettagli</span></summary><dl class="observation-details">${details || '<div><dt>Dettagli</dt><dd>Non disponibili per questo candidato.</dd></div>'}</dl></details>`;
    }).join('')}</div></section>`;
  }
  function renderLiveSystemStatus(status) {
    if (!status) {
      return `<section class="section live-system-status" data-section="live-system-status"><div class="section-head"><div><span class="section-kicker">LIVE SYSTEM STATUS</span><h2>Live System Status</h2></div><span class="badge">UNAVAILABLE</span></div><p class="health-note">Runner and Demo runtime status are not exposed by the current read-only Home contract.</p></section>`;
    }
    const runner = status.runner || {};
    const cycle = status.cycle || {};
    const scanner = status.scanner || {};
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
    if (cycle.last_successful_scan_at !== undefined) rows.push(`<div class="metric"><span class="label">Last successful scan</span><span class="value timestamp">${formatTimestamp(cycle.last_successful_scan_at)}</span></div>`);
    if (demo.connection_status !== undefined) rows.push(`<div class="metric"><span class="label">Demo connection</span><span class="value">${formatNumber(demo.connection_status)}</span></div>`);
    if (demo.automatic_pilot_armed !== undefined) rows.push(`<div class="metric"><span class="label">Demo pilot</span><span class="value">${formatNumber(demo.automatic_pilot_armed)}</span></div>`);
    if (demo.execution_enabled !== undefined) rows.push(`<div class="metric"><span class="label">Demo execution</span><span class="value">${formatNumber(demo.execution_enabled)}</span></div>`);
    if (demo.last_submission_status !== undefined) rows.push(`<div class="metric"><span class="label">Last Demo submission</span><span class="value">${formatNumber(demo.last_submission_status)}</span></div>`);
    if (demo.broker_write_calls !== undefined) rows.push(`<div class="metric"><span class="label">Demo broker writes (runner total)</span><span class="value">${formatNumber(demo.broker_write_calls)}</span></div>`);
    if (demo.last_poll_broker_write_calls !== undefined) rows.push(`<div class="metric"><span class="label">Demo broker writes (ultimo polling)</span><span class="value">${formatNumber(demo.last_poll_broker_write_calls)}</span></div>`);
    if (real.execution_available !== undefined) rows.push(`<div class="metric"><span class="label">Real execution</span><span class="value">${real.execution_available ? "AVAILABLE" : "DISABLED BY POLICY"}</span></div>`);
    if (real.broker_write_calls !== undefined) rows.push(`<div class="metric"><span class="label">Real broker writes</span><span class="value">${formatNumber(real.broker_write_calls)}</span></div>`);
    if (activity.code !== undefined) rows.push(`<div class="metric"><span class="label">Activity</span><span class="value">${formatNumber(activity.code)}</span></div>`);
    const classCounts = scanner.asset_class_counts || {};
    const classLabels = { EQUITY: 'Azioni', ETF: 'ETF', CRYPTO: 'Crypto' };
    const formatClassCounts = counts => ['EQUITY', 'ETF', 'CRYPTO']
      .map(assetClass => `${classLabels[assetClass]} ${formatNumber(counts[assetClass] ?? 0)}`)
      .join(' · ');
    for (const [key, label] of [
      ['evaluated', 'Scanner valutati per asset class'],
      ['buy_signals', 'Segnali BUY per asset class'],
      ['top', 'Candidati TOP per asset class'],
      ['watchlist', 'Watchlist per asset class'],
      ['no_trade', 'No-trade per asset class'],
      ['rejected', 'Esclusi per asset class'],
    ]) {
      if (classCounts[key] && typeof classCounts[key] === 'object') {
        rows.push(`<div class="metric"><span class="label">${label}</span><span class="value">${escapeHtml(formatClassCounts(classCounts[key]))}</span></div>`);
      }
    }
    const materialDiagnostics = activity.package_material_diagnostics || {};
    const sizingDiagnostics = activity.package_sizing_diagnostics || {};
    const cashReserveLabelFor = diagnostic => {
      const fraction = numericValue(diagnostic?.cash_reserve_fraction);
      return fraction === null || fraction < 0 || fraction >= 1
        ? 'configurata'
        : `${(fraction * 100).toLocaleString('it-IT', { maximumFractionDigits: 1 })}%`;
    };
    const reserveLabels = [...new Set(Object.values(sizingDiagnostics)
      .map(cashReserveLabelFor))];
    const cashReserveLabel = reserveLabels.length === 1
      ? reserveLabels[0]
      : reserveLabels.length > 1 ? 'applicata per strumento' : '7,5%';
    const materialEntries = Object.entries(materialDiagnostics)
      .filter(([symbol, reason]) => !symbol.startsWith('_') && typeof reason === 'string');
    const allCandidatesBlockedByBuyingPower = materialEntries.length > 0
      && materialEntries.every(([, reason]) => reason === 'DEMO_BUYING_POWER_UNAVAILABLE');
    const buyingPowerDiagnostic = Object.values(sizingDiagnostics).find(diagnostic =>
      Array.isArray(diagnostic?.zero_caps)
      && diagnostic.zero_caps.includes('cash_reserve_cap')
      && numericValue(diagnostic.cash_reserve_shortfall) > 0
    );
    const blockerLabels = {
      FRESH_NEWS_REQUIRED: 'Notizie fresche non disponibili',
      MULTI_REGION_COVERAGE_REQUIRED: 'Copertura di più aree di mercato insufficiente',
      VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE: allCandidatesBlockedByBuyingPower
        ? `Cash Demo sotto la riserva minima ${cashReserveLabel}: importo sicuro pari a zero`
        : 'Prezzo o dati broker verificati mancanti',
      DEMO_BUYING_POWER_UNAVAILABLE: buyingPowerDiagnostic
        ? `Cash Demo sotto la riserva minima ${cashReserveLabelFor(buyingPowerDiagnostic)}: mancano ${formatCurrency(buyingPowerDiagnostic.cash_reserve_shortfall, buyingPowerDiagnostic.account_currency)} per rispettarla; importo sicuro pari a zero`
        : 'I limiti applicati non consentono un importo sicuro positivo',
    };
    if (Array.isArray(activity.blockers) && activity.blockers.length) {
      const blockers = activity.blockers.map(code => blockerLabels[code] || String(code).replaceAll('_', ' '));
      rows.push(`<div class="metric attention"><span class="label">Perché l’ordine è fermo</span><span class="value">${escapeHtml(blockers.join(' · '))}</span></div>`);
    }
    if (materialDiagnostics && typeof materialDiagnostics === 'object' && Object.keys(materialDiagnostics).length) {
      const details = Object.entries(materialDiagnostics).map(([symbol, reason]) => {
        const explanation = reason === 'DEMO_BUYING_POWER_UNAVAILABLE'
          ? `cash insufficiente per la riserva minima ${cashReserveLabelFor(sizingDiagnostics[symbol])}`
          : String(reason);
        return `${symbol}: ${explanation}`;
      }).join(' · ');
      rows.push(`<div class="metric attention"><span class="label">Perché non è partito l’ordine Demo</span><span class="value">${escapeHtml(details)}</span></div>`);
    }
    if (sizingDiagnostics && typeof sizingDiagnostics === 'object' && Object.keys(sizingDiagnostics).length) {
      const capLabels = {
        managed_exposure_limit: 'tetto esposizione',
        remaining_managed_exposure: 'esposizione residua',
        single_order_limit: 'limite per ordine',
        account_cash: 'liquidità broker',
        risk_trade_cap: 'limite rischio per ordine',
        cash_reserve_cap: 'riserva minima',
      };
      const details = Object.entries(sizingDiagnostics).map(([symbol, diagnostic]) => {
        const zeroCaps = Array.isArray(diagnostic?.zero_caps)
          ? diagnostic.zero_caps.map(code => capLabels[code] || code).join(', ')
          : '';
        const reason = zeroCaps
          ? `limite a zero: ${zeroCaps}${diagnostic?.cash_reserve_shortfall !== undefined && diagnostic?.account_currency
            ? ` · riserva ${cashReserveLabelFor(diagnostic)} (${formatCurrency(diagnostic.cash_reserve_required, diagnostic.account_currency)}); mancano ${formatCurrency(diagnostic.cash_reserve_shortfall, diagnostic.account_currency)}`
            : ''}`
          : diagnostic?.rounded_below_account_cent
            ? 'importo sotto il centesimo della valuta conto'
            : 'sizing non disponibile';
        return `${symbol}: ${reason}`;
      }).join(' · ');
      rows.push(`<div class="metric attention"><span class="label">Dettaglio sizing (nessuna soglia di sicurezza rimossa)</span><span class="value">${escapeHtml(details)}</span></div>`);
    }
    const candidateDiagnostics = Array.isArray(activity.candidate_execution_diagnostics)
      ? activity.candidate_execution_diagnostics
      : [];
    if (candidateDiagnostics.length) {
      const details = candidateDiagnostics.map(candidate => {
        const quote = candidate.quote || {};
        const news = candidate.news || {};
        const submission = candidate.submission || {};
        const quoteAge = quote.age_seconds === undefined
          ? ''
          : ` ${formatNumber(quote.age_seconds)}s/${formatNumber(quote.max_age_seconds)}s`;
        const riskReasons = [
          ...(submission.risk_violation_codes || []),
          ...(submission.preflight_reasons || []),
        ].map(reason => String(reason).replaceAll('_', ' '));
        const gate = submission.status
          ? ` · ${submission.status}${riskReasons.length ? `: ${riskReasons.join(', ')}` : ''}`
          : '';
        return `${candidate.symbol} (${candidate.asset_class}): package ${candidate.package_status}; quote ${quote.status || 'n/d'}${quoteAge}; news ${news.freshness || 'n/d'} (${formatNumber(news.material_event_count)} materiali)${gate}`;
      }).join(' · ');
      rows.push(`<div class="metric attention"><span class="label">Diagnostica candidati · gate invariati</span><span class="value">${escapeHtml(details)}</span></div>`);
    }
    if (activity.last_error) rows.push(`<div class="metric attention"><span class="label">Ultimo errore</span><span class="value">${formatNumber(activity.last_error)}</span></div>`);
    if (exitManagement.observed_at !== undefined) {
      const exitState = exitManagement.blocked
        ? 'VERIFICA NECESSARIA'
        : exitManagement.pending_confirmation
          ? 'IN ATTESA DI CONFERMA BROKER'
          : exitManagement.close_triggered
            ? 'RICHIESTA · NON ANCORA CONFERMATA'
            : 'ATTIVA';
      rows.push(`<div class="metric"><span class="label">Gestione uscita Demo</span><span class="value">${exitState}</span></div>`);
      rows.push(`<div class="metric"><span class="label">Posizioni valutate / tenute</span><span class="value">${formatNumber(exitManagement.evaluated)} / ${formatNumber(exitManagement.held)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">Questo controllo: chiusure richieste / posizioni non più aperte / da verificare</span><span class="value">${formatNumber(exitManagement.close_triggered)} / ${formatNumber(exitManagement.closed_confirmed)} / ${formatNumber(exitManagement.pending_confirmation)}</span></div>`);
      if (exitManagement.closed_total !== undefined) rows.push(`<div class="metric"><span class="label">Posizioni non più aperte nel registro Aegis (totale)</span><span class="value">${formatNumber(exitManagement.closed_total)}</span></div>`);
      if (exitManagement.close_retry_calls !== undefined) rows.push(`<div class="metric"><span class="label">Retry correttivi chiusura</span><span class="value">${formatNumber(exitManagement.close_retry_calls)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">Ultimo controllo uscita</span><span class="value timestamp">${formatTimestamp(exitManagement.observed_at)}</span></div>`);
      if (Array.isArray(exitManagement.errors) && exitManagement.errors.length) {
        const ambiguousSymbols = [...new Set(exitManagement.errors
          .filter(error => String(error).endsWith(':POSITION_ID_AMBIGUOUS'))
          .map(error => String(error).split(':')[0]))];
        const exitMessage = ambiguousSymbols.length
          ? `Più posizioni sullo stesso strumento (${ambiguousSymbols.join(', ')}); chiusura automatica sospesa per non applicare il P/L aggregato alla posizione sbagliata.`
          : exitManagement.errors.map(error => {
              const text = String(error);
              if (text.endsWith(':CLOSE_HTTP_400')) return `${text.split(':')[0]}: il broker ha rifiutato la richiesta di chiusura (HTTP 400)`;
              if (text.endsWith(':CLOSE_REJECTED_400')) return `${text.split(':')[0]}: chiusura rifiutata anche dopo il retry correttivo; nessun altro reinvio automatico`;
              if (text.endsWith(':CLOSE_ORDER_REJECTED')) return `${text.split(':')[0]}: ordine di chiusura rifiutato dal broker`;
              if (text.endsWith(':CLOSE_ORDER_CANCELLED')) return `${text.split(':')[0]}: ordine di chiusura annullato dal broker`;
              if (text.endsWith(':CLOSE_RETRY_SKIPPED_POLICY_NO_LONGER_CLOSE')) return `${text.split(':')[0]}: retry sospeso perché la regola d’uscita non è più attiva`;
              return text.replaceAll('_', ' ');
            }).join(' · ');
        rows.push(`<div class="metric attention"><span class="label">Verifica uscite</span><span class="value">${escapeHtml(exitMessage)}</span></div>`);
      }
      if (Array.isArray(exitManagement.response_diagnostics) && exitManagement.response_diagnostics.length) {
        const brokerDetails = exitManagement.response_diagnostics.map(item => {
          const code = item.exit_broker_error_code ? ` ${item.exit_broker_error_code}` : '';
          const message = item.exit_broker_error_message ? ` — ${item.exit_broker_error_message}` : '';
          return `${item.symbol || 'Posizione'} HTTP ${item.http_status}${code}${message}`;
        }).join(' · ');
        rows.push(`<div class="metric attention"><span class="label">Dettaglio rifiuto broker</span><span class="value">${escapeHtml(brokerDetails)}</span></div>`);
      }
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
      rows.push(`<div class="metric"><span class="label">News requests</span><span class="value">${formatNumber(source.provider_request_count ?? source.news_provider_request_count)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News suppressed</span><span class="value">${formatNumber(source.requests_suppressed_after_rate_limit ?? source.news_requests_suppressed_after_rate_limit)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News cache hit / miss</span><span class="value">${formatNumber(source.cache_hits ?? source.news_cache_hits)} / ${formatNumber(source.cache_misses ?? source.news_cache_misses)}</span></div>`);
      rows.push(`<div class="metric"><span class="label">News ricevute / fresche / materiali</span><span class="value">${formatNumber(source.events_received ?? source.news_events_received)} / ${formatNumber(source.events_fresh ?? source.news_events_fresh)} / ${formatNumber(source.events_material ?? source.news_events_material)}</span></div>`);
      const cross = source.provider_diagnostics || source.news_provider_diagnostics || {};
      if (cross.cross_source_status !== undefined) rows.push(`<div class="metric"><span class="label">Cross-check fonti</span><span class="value">${formatNumber(cross.cross_source_status)}</span></div>`);
      if (cross.corroborated_event_groups !== undefined) rows.push(`<div class="metric"><span class="label">Eventi corroborati</span><span class="value">${formatNumber(cross.corroborated_event_groups)}</span></div>`);
      if (cross.conflicting_event_groups !== undefined) rows.push(`<div class="metric"><span class="label">Conflitti tra fonti</span><span class="value">${formatNumber(cross.conflicting_event_groups)}</span></div>`);
    }
      return `<section class="section live-system-status" data-section="live-system-status"><div class="section-head"><div><span class="section-kicker">LIVE SYSTEM STATUS</span><h2>Live System Status</h2></div></div>${rows.length ? `<div class="metrics">${rows.join("")}</div>` : `<p class="health-note">No live runtime fields are available.</p>`}</section>`;
  }
  function renderNewsIntelligence(news) {
    if (!news || (!news.events?.length && !Object.keys(news.asset_contexts || {}).length && !news.global_risk && !news.status)) return '';
    const risk = news.global_risk || {};
    if (risk.freshness === 'NEWS_SOURCE_UNAVAILABLE' || news.status === 'PROVIDER_UNAVAILABLE') {
      return '<section class="section news-intelligence" data-section="news-intelligence"><h2>News e geopolitica</h2><p class="health-note">Fonti news non disponibili nell’ultimo aggiornamento. Rischio geopolitico e macro non valutabili: nessuno zero va interpretato come assenza di rischio.</p></section>';
    }
    const events = Array.isArray(news.events) ? news.events : [];
    const contexts = Object.entries(news.asset_contexts || {}).filter(([, value]) => value && Number(value.event_risk || 0) > 0);
    const newsLabel = value => ({ NEWS_FRESH: 'Aggiornate', NEWS_DELAYED: 'Non recenti', NEWS_STALE: 'Da aggiornare', NEWS_SOURCE_UNAVAILABLE: 'Fonte non disponibile', POSITIVE: 'Positivo', NEGATIVE: 'Negativo', NEUTRAL: 'Neutrale', GEOPOLITICS: 'Geopolitica', EARNINGS: 'Risultati aziendali', COMPANY_GUIDANCE: 'Previsioni aziendali', PRODUCT_LAUNCH: 'Nuovi prodotti', ENERGY: 'Energia', OTHER: 'Altro' }[value] || String(value || 'Non disponibile').replaceAll('_', ' '));
    const freshCount = news.events_fresh ?? 0;
    const receivedCount = news.events_received ?? 0;
    const materialCount = news.events_material ?? 0;
    const eventMarkup = events.length ? `<div class="news-event-list">${events.map((event) => {
      const linked = Array.isArray(event.linked_symbols) && event.linked_symbols.length ? event.linked_symbols.join(', ') : 'Global';
      const sources = Array.isArray(event.sources) && event.sources.length ? event.sources.join(', ') : event.source || 'Fonte non disponibile';
      const key = `${event.published_at || ''}:${event.headline || ''}`;
      return `<details class="news-event" data-news-disclosure="${escapeHtml(key)}"><summary><span class="news-event-head"><strong>${escapeHtml(newsLabel(event.category || 'OTHER'))}</strong><span>${escapeHtml(formatTimestamp(event.published_at))}</span></span><span class="news-headline">${escapeHtml(event.headline || 'Evento senza titolo')}</span><span class="news-expand-hint">Fonti e strumenti coinvolti</span></summary><div class="news-event-detail"><dl><div><dt>Sentiment</dt><dd>${escapeHtml(newsLabel(event.sentiment || 'NEUTRAL'))}</dd></div><div><dt>Impatto</dt><dd>${escapeHtml(event.impact_score ?? '—')}</dd></div><div><dt>Fonti</dt><dd>${escapeHtml(sources)}</dd></div><div><dt>Strumenti collegati</dt><dd>${escapeHtml(linked)}</dd></div></dl><p class="health-note">Collegamento informativo, non un’indicazione di acquisto o vendita.</p></div></details>`;
    }).join('')}</div>` : `<p class="empty">Nessun titolo nel riepilogo dettagliato. Il provider ha riportato ${formatNumber(freshCount)} notizie fresche su ${formatNumber(receivedCount)} ricevute, incluse ${formatNumber(materialCount)} materiali.</p>`;
    const contextMarkup = contexts.length ? `<details class="news-group" data-news-disclosure="contexts"><summary>Contesto per strumento <span class="badge">${contexts.length}</span></summary><div class="news-context-grid">${contexts.map(([symbol, value]) => `<div class="news-context"><strong>${escapeHtml(symbol)}</strong><span>${escapeHtml(newsLabel(value.aggregate_sentiment || 'NEUTRAL'))} · rischio ${escapeHtml(value.event_risk ?? '0')}</span><small>${escapeHtml(value.asset_class || '')} · ${escapeHtml(newsLabel(value.freshness))} · ${formatNumber(value.material_event_count || 0)} eventi materiali</small></div>`).join('')}</div></details>` : '';
    const metrics = [
      ['Rischio geopolitico', risk.geopolitical_risk],
      ['Rischio macro', risk.macro_risk],
      ['Eventi ad alto impatto', risk.high_impact_event_count],
      ['Aggiornamento news', risk.freshness ? newsLabel(risk.freshness) : null],
      ['Notizie ricevute / fresche / materiali', `${formatNumber(receivedCount)} / ${formatNumber(freshCount)} / ${formatNumber(materialCount)}`],
    ].filter(([, value]) => value !== undefined && value !== null).map(([label, value]) => `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value">${formatNumber(value)}</span></div>`).join('');
    const providerBadge = news.status === 'AVAILABLE' ? 'DISPONIBILI' : news.status === 'PARTIAL' ? 'PARZIALI' : (news.status || 'DA VERIFICARE');
    return `<section class="section news-intelligence" data-section="news-intelligence"><div class="section-head"><div><span class="section-kicker">NEWS & GEOPOLITICA</span><h2>News e geopolitica · ${events.length} titoli dettagliati</h2></div><span class="badge">${escapeHtml(providerBadge)}</span></div><p class="health-note">Le news sono contesto, non autorizzano ordini da sole. I titoli dettagliati mostrati sono distinti dal conteggio totale delle notizie ricevute.</p>${metrics ? `<div class="metrics news-metrics">${metrics}</div>` : ''}<details class="news-group" data-news-disclosure="events"><summary>Notizie del ciclo <span class="badge">${events.length}</span></summary>${eventMarkup}</details>${contextMarkup}</section>`;
  }
  function renderOvernightActivity(activity) {
    if (!activity) return '';
    const capStatus = activity.exposure_cap_status;
    const newOrderGate = capStatus === 'WITHIN_LIMIT'
      ? 'Sotto il tetto; restano obbligatori i controlli di rischio e broker'
      : capStatus === 'EXCEEDED'
        ? 'BLOCCATI: tetto totale superato'
        : 'BLOCCATI: esposizione/valuta non verificabile';
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
      [`Capitale autorizzato (${activity.authorized_capital_currency || 'EUR'})`, formatCurrency(activity.authorized_capital ?? activity.authorized_capital_eur, activity.authorized_capital_currency || 'EUR')],
      [`Esposizione Aegis (${activity.managed_exposure_currency || 'EUR'})`, formatCurrency(activity.managed_exposure ?? activity.managed_exposure_eur, activity.managed_exposure_currency || 'EUR')],
      [`Tetto totale Aegis (${activity.managed_exposure_currency || 'EUR'})`, formatCurrency(activity.managed_exposure_limit ?? activity.managed_exposure_limit_eur, activity.managed_exposure_currency || 'EUR')],
      ['Stato tetto esposizione', activity.exposure_cap_status],
      ['Nuovi ordini Aegis', newOrderGate],
      ['Ordini Demo non riconciliati', formatNumber(activity.unverified_legacy_demo_order_count)],
      [`Importo non riconciliato (${activity.unverified_legacy_demo_order_currency || 'valuta n/d'})`, formatCurrency(activity.unverified_legacy_demo_order_nominal_total, activity.unverified_legacy_demo_order_currency || 'USD')],
      [`Capitale residuo (${activity.remaining_capital_currency || activity.authorized_capital_currency || 'EUR'})`, formatCurrency(activity.remaining_capital ?? activity.remaining_capital_eur, activity.remaining_capital_currency || activity.authorized_capital_currency || 'EUR')],
      ['Last activity', formatTimestamp(activity.last_activity_at)],
    ];
    const history = Array.isArray(activity.top_opportunities_per_cycle) ? activity.top_opportunities_per_cycle : [];
    const historyMarkup = history.length ? `<div class="activity-history"><h3>TOP opportunities per cycle</h3>${history.map((item) => `<div class="activity-cycle"><span>${formatTimestamp(item.observed_at)}</span><strong>${formatNumber(item.top_opportunity_count)}</strong><span>${escapeHtml(displayCycleState(item.cycle_state || ''))}</span></div>`).join('')}</div>` : '';
    return `<section class="section overnight-activity" data-section="overnight-activity"><div class="section-head"><div><span class="section-kicker">ATTIVITÀ AEGIS</span><h2>Attività Aegis</h2></div><span class="badge">Registro locale</span></div><div class="metrics activity-metrics">${rows.map(([label, value]) => `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value compact">${typeof value === 'string' && value !== 'Unavailable' && (label.includes('cycle') || label.includes('activity')) ? escapeHtml(value) : formatNumber(value)}</span></div>`).join('')}</div>${historyMarkup}<p class="health-note">L’esposizione del registro considera soltanto posizioni Aegis riconciliate e usa la valuta del conto Demo. Le posizioni correnti e il P/L visualizzati sopra provengono dalla lettura live del broker.</p></section>`;
  }
  function render(snapshot) {
    const readingView = currentSnapshot && selectedMode !== 'MANUAL' ? captureReadingView() : null;
    const liveDemo = snapshot.live_demo_snapshot?.status === 'LIVE_READ_ONLY'
      ? snapshot.live_demo_snapshot
      : null;
    const liveDemoUnavailable = Boolean(snapshot.live_demo_snapshot)
      && snapshot.live_demo_snapshot.status !== 'LIVE_READ_ONLY';
    const monitoredPositions = snapshot.positions;
    if (liveDemo) {
      const livePositions = Array.isArray(liveDemo.positions) ? liveDemo.positions : [];
      snapshot.positions = {
        open_count: livePositions.length,
        items: livePositions.map((position) => ({
          symbol: position.symbol || `ID ${position.instrument_id}`,
          name: position.name || position.description || position.symbol || `ID ${position.instrument_id}`,
          description: position.description || position.name || position.symbol || `ID ${position.instrument_id}`,
          state: position.direction || 'LONG',
          instrument_id: position.instrument_id,
          units: position.units,
          average_entry_price: position.average_entry_price,
        })),
      };
      snapshot.safety = {
        ...snapshot.safety,
        execution_mode: 'DEMO',
        broker_write_calls: snapshot.live_system_status?.demo?.broker_write_calls
          ?? safety.broker_write_calls,
      };
    }
    currentSnapshot = snapshot;
    root.className = selectedMode === "MANUAL" ? "manual-mode" : "ai-mode";
    const { capital, scanner, watchlist, positions, safety, data_health: health } = snapshot;
    const topItems = Array.isArray(snapshot.last_scan_top_opportunities)
      ? snapshot.last_scan_top_opportunities
      : (Array.isArray(snapshot.top_opportunity_items) ? snapshot.top_opportunity_items : []);
    const noTrade = scanner.top_opportunities === 0;
    const degraded = health.backend_status !== "READY" || health.one_hour_status !== "READY";
    const liveAuthorizedCapital = snapshot.live_system_status?.capital?.authorized_capital ?? snapshot.live_system_status?.capital?.authorized_capital_eur ?? capital.amount;
    const liveCapitalCurrency = snapshot.live_system_status?.capital?.currency || capital.currency || "EUR";
    const managedExposureLimit = snapshot.live_system_status?.capital?.managed_exposure_limit ?? snapshot.live_system_status?.capital?.managed_exposure_limit_eur;
    const managedExposureCurrency = snapshot.live_system_status?.capital?.managed_exposure_currency || liveCapitalCurrency;
    const liveRuntimeTimestamp = snapshot.live_system_status?.heartbeat?.last_activity_at;
    const liveDemoCurrency = liveDemo?.currency || "USD";
    const liveDemoReadError = snapshot.live_demo_snapshot?.diagnostics?.transport_detail
      || snapshot.live_demo_snapshot?.error
      || snapshot.live_demo_snapshot?.status;
    const livePnlValue = numericValue(liveDemo?.current_pnl);
    const livePnlClass = liveDemoUnavailable || livePnlValue === null
      ? '' : livePnlValue >= 0 ? 'positive' : 'negative';
    const livePositionPnl = liveDemo ? summarizePositionPnl(liveDemo.positions) : null;
    const positionPnlMetric = value => liveDemoUnavailable ? 'Non disponibile'
      : !liveDemo ? '—'
      : livePositionPnl.complete ? formatCurrency(value, liveDemoCurrency) : 'Non disponibile';
    const brokerMetric = value => liveDemo ? formatCurrency(value, liveDemoCurrency)
      : liveDemoUnavailable ? 'Non disponibile' : '—';
    const userState = userFacingRuntimeState(snapshot);
    const scannerMetric = (label, value, tone = '') => `<div class="metric scanner-metric ${tone ? `scanner-metric-${tone}` : ''}"><span class="label">${escapeHtml(label)}</span><span class="value">${formatNumber(value)}</span></div>`;
    const scannerUniverseMetrics = [
      ['Catalogo eToro completo', scanner.catalog_count, 'primary'],
      ['Dati pronti ora', scanner.catalog_ready_count ?? scanner.coherent_now, 'primary'],
      ['Universo operativo validato', scanner.universe_count, 'primary'],
      ['Analizzati nell’ultima scansione', scanner.assets_scanned, 'primary'],
    ];
    const scannerResultMetrics = [
      ['In verifica bootstrap', scanner.catalog_pending_count, 'muted'],
      ['Top watchlist mostrate', scanner.last_scan_watchlist_count, 'watchlist'],
      ['No-trade nell’ultima scansione', scanner.last_scan_no_trade_count, 'attention'],
      ['Candidati TOP nel runtime (non ordini)', scanner.top_opportunities, 'top'],
    ];
    const headerStatus = document.getElementById("header-scanner-status");
    if (headerStatus) headerStatus.lastChild.textContent = ` ${userState.label}`;
    root.innerHTML = `${renderModePanel()}<div class="grid">
      <section class="section capital-section"><div><span class="section-kicker">01 / CAPITAL · ${escapeHtml(liveCapitalCurrency)}</span><span class="capital-amount">${formatCurrency(liveAuthorizedCapital, liveCapitalCurrency)}</span></div><div class="capital-note"><p>CAPITALE AUTORIZZATO CONFIGURATO</p><strong>${liveDemo ? "Contesto conto Demo eToro · sola lettura" : "Limite runtime · sola lettura"}</strong><p>Non è l’importo di un singolo ordine. Esposizione Aegis massima: ${formatCurrency(managedExposureLimit, managedExposureCurrency)} complessivi.</p><p title="${escapeHtml(liveRuntimeTimestamp || snapshot.as_of)}">Runtime ${formatRelative(liveRuntimeTimestamp || snapshot.as_of)}</p>${liveDemoUnavailable ? `<p class="health-note attention">Portafoglio eToro non verificabile: ${escapeHtml(liveDemoReadError || 'errore di lettura')}. Valuta, disponibilità, posizioni e P/L non sono confermati.</p>` : ''}</div><div class="capital-live-metrics metrics"><div class="metric"><span class="label">Valore portafoglio Demo</span><span class="value">${brokerMetric(liveDemo?.total_value)}</span></div><div class="metric"><span class="label">Cash disponibile</span><span class="value">${brokerMetric(liveDemo?.cash)}</span></div><div class="metric"><span class="label">P/L corrente</span><span class="value ${livePnlClass}">${brokerMetric(liveDemo?.current_pnl)}</span></div><div class="metric"><span class="label">Posizioni aperte</span><span class="value">${liveDemo ? formatNumber(positions.open_count) : liveDemoUnavailable ? 'Non disponibile' : formatNumber(positions.open_count)}</span></div><div class="metric"><span class="label">Posizioni in perdita</span><span class="value">${liveDemo && livePositionPnl.complete ? formatNumber(livePositionPnl.loss_count) : liveDemoUnavailable || liveDemo ? 'Non disponibile' : '—'}</span></div><div class="metric"><span class="label">Posizioni in guadagno</span><span class="value">${liveDemo && livePositionPnl.complete ? formatNumber(livePositionPnl.gain_count) : liveDemoUnavailable || liveDemo ? 'Non disponibile' : '—'}</span></div><div class="metric"><span class="label">P/L posizioni in perdita</span><span class="value ${livePositionPnl?.complete && livePositionPnl.loss < 0 ? 'negative' : ''}">${positionPnlMetric(livePositionPnl?.loss)}</span></div><div class="metric"><span class="label">P/L posizioni in guadagno</span><span class="value ${livePositionPnl?.complete && livePositionPnl.gain > 0 ? 'positive' : ''}">${positionPnlMetric(livePositionPnl?.gain)}</span></div></div>${liveDemo && !livePositionPnl.complete ? '<p class="health-note attention">eToro non ha restituito il P/L per tutte le posizioni: i totali di perdita e guadagno non sono disponibili.</p>' : ''}</section>
      <section class="section scanner-section"><div class="section-head"><div><span class="section-kicker">02 / STATO DELLO SCANNER</span><h2>Stato dello scanner</h2></div><span class="badge">${escapeHtml(userState.label)}</span></div><p class="health-note scanner-intro">${escapeHtml(userState.detail)}</p><div class="scanner-dashboard"><div class="scanner-metric-group"><span class="scanner-group-label">UNIVERSO E COPERTURA</span><div class="metrics scanner-metrics scanner-metrics-primary">${scannerUniverseMetrics.map(([label, value, tone]) => scannerMetric(label, value, tone)).join('')}</div></div><div class="scanner-metric-group"><span class="scanner-group-label">RISULTATI DELL’ULTIMA SCANSIONE</span><div class="metrics scanner-metrics scanner-metrics-secondary">${scannerResultMetrics.map(([label, value, tone]) => scannerMetric(label, value, tone)).join('')}</div></div></div><p class="health-note scanner-timestamp">Ultima scansione: ${formatTimestamp(snapshot.as_of)}</p></section>
      ${renderTopOpportunities(topItems, topItems.length, snapshot.as_of)}
      ${renderLiveSystemStatus(snapshot.live_system_status)}
      ${renderNewsIntelligence(snapshot.news)}
      ${renderOvernightActivity(snapshot.live_system_status && snapshot.live_system_status.overnight_activity)}
      <section class="section conclusion"><span class="section-kicker">ANALISI AEGIS</span><h2>Conclusioni Aegis</h2><p>${noTrade ? "L’ultima scansione accettata non mostra candidati TOP nel runtime corrente." : "Il conteggio TOP è un risultato del ranking: non conferma un ordine. Servono ancora RiskManager, preflight e conferma esplicita dello stato broker."}</p>${noTrade ? `<p class="conclusion-detail">${formatNumber(scanner.watchlist_count)} strumenti restano in osservazione.</p>` : ""}</section>
      ${renderBenchmarkPanel(snapshot)}
      <section class="section watchlist-section" data-section="watchlist-section"><div class="section-head"><div><span class="section-kicker">05 / TOP WATCHLIST DEL CICLO</span><h2>Top watchlist del ciclo</h2></div><span class="meta">${formatNumber(scanner.last_scan_watchlist_count)} su ${formatNumber(scanner.assets_scanned)} valutati</span></div><p class="health-note">Questi sono solo i candidati rimasti in WATCHLIST. L’universo completo è il conteggio sopra: gli altri strumenti sono stati valutati e classificati NO_TRADE o esclusi.</p>${renderWatchlist(watchlist)}</section>
      <section class="section positions-summary" data-section="positions-summary"><div class="section-head"><div><span class="section-kicker">06 / POSIZIONI OSSERVATE</span><h2>Posizioni osservate</h2></div><span class="meta">${formatNumber(positions.open_count)} open</span></div>${renderPositions(positions.items)}</section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">07 / RISK / SAFETY</span><h2>Risk / Safety</h2></div><span class="badge ${safety.execution_mode === "READ_ONLY" ? "safe" : ""}">${formatNumber(safety.execution_mode)}</span></div><div class="metrics"><div class="metric"><span class="label">Portfolio exposure</span><span class="value">${liveDemo && numericValue(liveDemo.total_value) !== null && numericValue(liveDemo.cash) !== null ? `${(numericValue(liveDemo.total_value) - numericValue(liveDemo.cash)).toFixed(2)} ${escapeHtml(liveDemo.currency || '')}` : '—'}</span></div><div class="metric"><span class="label">Open positions</span><span class="value">${formatNumber(positions.open_count)}</span></div><div class="metric"><span class="label">Risk state</span><span class="value">${formatNumber(liveDemo ? (snapshot.live_system_status?.activity?.code || 'LIVE_READ_ONLY') : 'Unavailable')}</span></div><div class="metric"><span class="label">Broker writes</span><span class="value ${safety.broker_write_calls === 0 ? "green" : ""}">${formatNumber(safety.broker_write_calls)}</span></div></div></section>
      <section class="section ${degraded ? "state-panel degraded" : ""}"><div class="section-head"><h2>Stato dei dati di mercato</h2><span class="badge">${degraded ? "DA VERIFICARE" : "DATI PRONTI"}</span></div><div class="metrics"><div class="metric"><span class="label">Copertura dati 1H corrente</span><span class="value">${escapeHtml(health.one_hour_status)}</span></div><div class="metric"><span class="label">Stato del ciclo</span><span class="value">${escapeHtml(displayCycleState(health.scanner_cycle_status || 'UNKNOWN'))}</span></div><div class="metric"><span class="label">Ultima analisi completata</span><span class="value timestamp">${formatTimestamp(health.last_scan_at)}</span></div></div><p class="health-note">La disponibilità delle news è indicata separatamente nel pannello News e geopolitica.</p></section>
    </div>`;
    if (catalogSearchRoot) {
      catalogSearchRoot.innerHTML = renderCatalogSearchPanel(
        scanner.catalog_count,
        scanner.catalog_ready_count ?? scanner.coherent_now,
        scanner.catalog_as_of,
        scanner.bootstrap_as_of
      );
      setSectionUpdatedAt(catalogSearchRoot.querySelector('.catalog-search-section'), scanner.catalog_as_of || snapshot.as_of, 'Catalogo');
      setCatalogSearchVisibility(catalogSearchVisible);
    }
    const dashboardGrid = root.querySelector('.grid');
    const modePanel = root.querySelector('.mode-panel');
    const capitalSection = dashboardGrid?.querySelector(':scope > .capital-section');
    if (capitalSection) capitalSection.insertAdjacentHTML('beforeend', renderPerformanceReview(liveDemo));
    const conclusionSection = dashboardGrid?.querySelector(':scope > .conclusion');
    if (dashboardGrid) {
      dashboardGrid.prepend(...[capitalSection, conclusionSection, modePanel].filter(Boolean));
      dashboardGrid.insertAdjacentHTML('beforeend', renderBrokerPositions(snapshot.live_demo_snapshot));
    }
    const monitoredSection = root.querySelector('.positions-summary');
    if (monitoredSection) {
      const monitoredItems = Array.isArray(monitoredPositions?.items) ? monitoredPositions.items : [];
      const monitoredCount = monitoredPositions?.open_count;
      const countLabel = monitoredCount == null ? "dato non disponibile nel ciclo" : formatNumber(monitoredCount) + " nel ciclo";
      monitoredSection.innerHTML = '<div class="section-head"><div><span class="section-kicker">MONITORAGGIO AEGIS</span><h2>Asset in monitoraggio Aegis</h2></div><span class="meta">' +
        countLabel + '</span></div>' + renderPositions(monitoredItems, monitoredCount);
    }
    [...root.querySelectorAll('.section')].find(section => section.querySelector('h2')?.textContent?.trim() === 'Risk / Safety')?.setAttribute('data-section', 'risk-safety');
    [...root.querySelectorAll('.section')].find(section => section.querySelector('h2')?.textContent?.trim() === 'Stato dei dati di mercato')?.setAttribute('data-section', 'market-data-health');
    [...root.querySelectorAll('.section')].find(section => section.querySelector('h2')?.textContent?.trim() === 'Stato dello scanner')?.setAttribute('data-section', 'scanner-summary');
    decorateSectionTimestamps(snapshot);
    enhanceQuickLinks();
    improveSections(snapshot);
    bindModePanel();
    restoreReadingView(readingView);
  }
  async function load({ initial = false, silent = false } = {}) {
    if (manualBusy || manualEditing) return;
    // Manual trading is an interaction surface, not a live dashboard. Once it
    // is on screen, background refreshes update its own eToro reads without
    // replacing the document and changing the user's scroll position.
    if (selectedMode === "MANUAL" && currentSnapshot && !initial) {
      loadLiveDemo();
      loadLiveOrders();
      if (!liveInstrumentsLoaded) loadLiveInstruments();
      return;
    }
    if (loadInFlight) return;
    loadInFlight = true;
    if (initial) renderLoading();
    const refreshButton = document.getElementById("refresh-data");
    if (refreshButton && !silent) { refreshButton.disabled = true; refreshButton.textContent = "Aggiornamento..."; }
    try {
      const response = await fetch(apiUrl, { method: "GET", headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status === "ERROR") throw new Error(payload.message || "Scanner state is unavailable.");
      try {
        const liveResponse = await fetch(liveDemoUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
        const livePayload = await liveResponse.json();
        payload.live_demo_snapshot = liveResponse.ok
          ? livePayload
          : { ...livePayload, status: livePayload.status || "LIVE_READ_UNAVAILABLE" };
      } catch (_) {
        payload.live_demo_snapshot = { status: "LIVE_READ_UNAVAILABLE", error: "CONNESSIONE_NON_DISPONIBILE" };
      }
      // A refresh may have started just before the user selected BUY/SELL.
      // Keep its data, but never replace the manual ticket or move the page
      // while the user is choosing a position or confirming an order.
      if (selectedMode === "MANUAL" && manualEditing) {
        currentSnapshot = payload;
        return;
      }
      render(payload);
      loadBenchmark();
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
  async function loadBenchmark() {
    try {
      const response = await fetch('/api/benchmarks/etoro', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      const payload = await response.json();
      currentBenchmarkSnapshot = response.ok ? payload : { status: 'UNAVAILABLE', profiles: [] };
      if (currentSnapshot && selectedMode !== "MANUAL" && !manualEditing) render(currentSnapshot);
    } catch (_) {
      currentBenchmarkSnapshot = { status: 'UNAVAILABLE', profiles: [] };
      if (currentSnapshot && selectedMode !== "MANUAL" && !manualEditing) render(currentSnapshot);
    }
  }
  document.getElementById("refresh-data")?.addEventListener("click", () => load());
  document.getElementById('toggle-catalog-search')?.addEventListener('click', () => {
    setCatalogSearchVisibility(!catalogSearchVisible, !catalogSearchVisible);
  });
  root.addEventListener('click', event => {
    if (event.target.closest('[data-open-search]')) {
      setCatalogSearchVisibility(true, true);
      return;
    }
    const quickLink = event.target.closest('[data-open-section]');
    if (quickLink) {
      openDashboardSection(quickLink.dataset.openSection, quickLink.dataset.positionFilter || 'all');
      return;
    }
    if (event.target.closest('[data-clear-position-filter]')) applyPositionFilter('all');
  });
  root.addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key)) return;
    if (event.target.closest('[data-open-search]')) {
      event.preventDefault();
      setCatalogSearchVisibility(true, true);
      return;
    }
    const quickLink = event.target.closest('[data-open-section]');
    if (!quickLink) return;
    event.preventDefault();
    openDashboardSection(quickLink.dataset.openSection, quickLink.dataset.positionFilter || 'all');
  });
  catalogSearchRoot?.addEventListener('submit', event => {
    if (event.target?.id !== 'catalog-search-form') return;
    event.preventDefault();
    const query = catalogSearchRoot.querySelector('#catalog-search-query')?.value || '';
    searchEtoroCatalog(query);
  });
  catalogSearchRoot?.addEventListener('click', event => {
    if (!event.target?.closest('#catalog-search-more')) return;
    searchEtoroCatalog(catalogSearchState.query, { append: true });
  });
  load({ initial: true });
  window.setInterval(() => {
    if (!document.hidden && selectedMode !== "MANUAL" && !manualBusy) load({ silent: true });
  }, AUTO_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && selectedMode !== "MANUAL" && !manualBusy) load({ silent: true });
  });
})();
