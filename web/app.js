(() => {
  const root = document.getElementById("app");
  const apiUrl = "/api/home";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
  const formatNumber = (value) => value === null || value === undefined ? "Unavailable" : escapeHtml(value);
  const formatTimestamp = (value) => value ? new Date(value).toLocaleString() : "Unavailable";

  function renderLoading() {
    root.innerHTML = '<section class="state-panel" data-state="loading"><span class="spinner" aria-hidden="true"></span><p>Loading the latest scanner state...</p></section>';
  }
  function renderError(message) {
    root.innerHTML = `<section class="state-panel error" data-state="error"><p>${escapeHtml(message)}</p><button class="retry" type="button">Retry read-only scan</button></section>`;
    root.querySelector(".retry").addEventListener("click", load);
  }
  function renderEmpty(label) { return `<p class="empty">${escapeHtml(label)}</p>`; }
  function renderWatchlist(items) {
    if (!items.length) return renderEmpty("No qualifying watchlist assets right now.");
    return `<div class="list">${items.map((item) => `<div class="row"><span class="symbol">${escapeHtml(item.symbol)}</span><span class="reason">${escapeHtml(item.name)}</span><span class="meta">Score ${formatNumber(item.score)}</span><span class="badge">Rank ${formatNumber(item.rank)}</span></div>`).join("")}</div>`;
  }
  function renderPositions(items) {
    if (!items.length) return renderEmpty("No open positions.");
    return `<div class="list">${items.map((item) => `<div class="row"><span class="symbol">${escapeHtml(item.symbol)}</span><span class="reason">${escapeHtml(item.state)}</span><span class="meta">Value ${formatNumber(item.value)}</span><span class="badge safe">Observed</span></div>`).join("")}</div>`;
  }
  function render(snapshot) {
    const { capital, scanner, watchlist, positions, safety, data_health: health } = snapshot;
    const noTrade = scanner.top_opportunities === 0;
    const degraded = health.backend_status !== "READY" || health.one_hour_status !== "READY";
    root.innerHTML = `<div class="grid">
      <section class="section"><div class="section-head"><div><span class="section-kicker">01 / CAPITAL</span><h2>Capital</h2></div><span class="badge">${escapeHtml(capital.mode)}</span></div><div class="metrics"><div class="metric"><span class="label">Available context</span><span class="value">${formatNumber(capital.amount)} ${escapeHtml(capital.currency || "")}</span></div><div class="metric"><span class="label">As of</span><span class="value">${formatTimestamp(snapshot.as_of)}</span></div></div></section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">02 / SCANNER STATE</span><h2>Scanner State</h2></div><span class="badge safe">${escapeHtml(scanner.status)}</span></div><div class="metrics"><div class="metric"><span class="label">Assets scanned</span><span class="value">${formatNumber(scanner.assets_scanned)}</span></div><div class="metric"><span class="label">Comparable</span><span class="value">${formatNumber(scanner.assets_comparable)}</span></div><div class="metric"><span class="label">Watchlist</span><span class="value">${formatNumber(scanner.watchlist_count)}</span></div><div class="metric"><span class="label">No trade</span><span class="value">${formatNumber(scanner.no_trade_count)}</span></div></div></section>
      <section class="section conclusion"><span class="section-kicker">03 / AEGIS CONCLUSION</span><h2>${noTrade ? "NO TRADE REQUIRED" : "Current opportunities available"}</h2><p>${noTrade ? "No asset currently qualifies as a top opportunity. Aegis continues monitoring the watchlist instead of forcing a trade." : "The scanner has identified current opportunities for further read-only review."}</p></section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">04 / WATCHLIST</span><h2>Watchlist</h2></div><span class="meta">${formatNumber(scanner.watchlist_count)} assets</span></div>${renderWatchlist(watchlist)}</section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">05 / POSITIONS</span><h2>Positions</h2></div><span class="meta">${formatNumber(positions.open_count)} open</span></div>${renderPositions(positions.items)}</section>
      <section class="section"><div class="section-head"><div><span class="section-kicker">06 / RISK / SAFETY</span><h2>Risk / Safety</h2></div><span class="badge safe">${escapeHtml(safety.execution_mode)}</span></div><div class="metrics"><div class="metric"><span class="label">Broker write calls</span><span class="value green">${formatNumber(safety.broker_write_calls)}</span></div><div class="metric"><span class="label">Isolation</span><span class="value green">Verified</span></div></div></section>
      <section class="section ${degraded ? "state-panel degraded" : ""}"><div class="section-head"><div><span class="section-kicker">07 / DATA HEALTH</span><h2>Data Health</h2></div><span class="badge ${degraded ? "" : "safe"}">${escapeHtml(health.backend_status)}</span></div><div class="metrics"><div class="metric"><span class="label">1H data</span><span class="value">${escapeHtml(health.one_hour_status)}</span></div><div class="metric"><span class="label">Last scan</span><span class="value">${formatTimestamp(health.last_scan_at)}</span></div></div></section>
    </div>`;
  }
  async function load() {
    renderLoading();
    try {
      const response = await fetch(apiUrl, { method: "GET", headers: { Accept: "application/json" }, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status === "ERROR") throw new Error(payload.message || "Scanner state is unavailable.");
      render(payload);
    } catch (error) { renderError(error.message || "Scanner state is unavailable."); }
  }
  load();
})();
