/* The dashboard page: React without a build step (htm gives JSX-like templates). It only reads /api/*; nothing here can trade. */
(function () {
  "use strict";
  const { useState, useEffect, useRef, useMemo, useCallback } = React;
  const html = htm.bind(React.createElement);
  const REFRESH_MS = 60000;
  const RANGES = [["24 h", 1], ["7 d", 7], ["30 d", 30], ["All", null]];
  const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

  // --- formatting -------------------------------------------------------------------------------------------------
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const pct = (value, digits = 1, sign = false) => (finite(value) ? `${sign && value > 0 ? "+" : ""}${(value * 100).toFixed(digits)}%` : "–");
  const times = (value, digits = 2) => (finite(value) ? `${value.toFixed(digits)}×` : "–");
  const number = (value, digits = 2) => (finite(value) ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "–");
  const money = (value) => (finite(value) ? `${value < 0 ? "−" : ""}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "–");
  const signedMoney = (value) => (finite(value) ? `${value > 0 ? "+" : value < 0 ? "−" : ""}$${Math.abs(value).toFixed(2)}` : "–");
  const price = (value) => (finite(value) ? value.toLocaleString("en-US", { maximumSignificantDigits: 6 }) : "–");
  const coin = (instrument) => String(instrument || "").split(":").pop().replace("/USD", "");
  const clock = (stamp, withDate = true) => {
    const date = new Date(stamp);
    const day = date.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
    const hour = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return withDate ? `${day} ${hour}` : hour;
  };
  const ago = (seconds) => {
    if (!finite(seconds)) return "never";
    if (seconds < 90) return `${Math.round(seconds)} s ago`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h ago`;
    return `${Math.round(seconds / 86400)} d ago`;
  };

  // --- data -------------------------------------------------------------------------------------------------------
  async function get(path) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) throw new Error(`${path}: ${response.status}`);
    return response.json();
  }

  function useWidth() {
    const ref = useRef(null);
    const [width, setWidth] = useState(600);
    useEffect(() => {
      if (!ref.current) return undefined;
      const observer = new ResizeObserver((entries) => setWidth(Math.max(280, Math.floor(entries[0].contentRect.width))));
      observer.observe(ref.current);
      return () => observer.disconnect();
    }, []);
    return [ref, width];
  }

  function niceTicks(low, high, count) {
    if (!(high > low)) { const pad = Math.abs(low) * 0.01 || 1; low -= pad; high += pad; }
    const raw = (high - low) / count;
    const power = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw);
    const ticks = [];
    for (let tick = Math.ceil(low / step) * step; tick <= high + step * 1e-9; tick += step) ticks.push(Math.abs(tick) < step * 1e-9 ? 0 : tick);
    return ticks;
  }

  // --- status and small pieces ------------------------------------------------------------------------------------
  function Status({ level, label }) {
    const icon = { good: "✓", warning: "!", critical: "✕" }[level];
    return html`<span class="status"><span class="dot" style=${{ background: `var(--${level})` }}></span><span>${icon} ${label}</span></span>`;
  }

  function Tile({ label, value, note }) {
    return html`<div class="tile"><div class="label">${label}</div><div class="value">${value}</div>${note ? html`<div class="note">${note}</div>` : null}</div>`;
  }

  function Card({ title, sub, span, children, table }) {
    const [asTable, setAsTable] = useState(false);
    return html`<section class=${`card ${span || ""}`}>
      <div class="card-head"><h2>${title}</h2>${sub ? html`<span class="sub">${sub}</span>` : null}<span class="spacer"></span>
        ${table ? html`<button class="linkish" onClick=${() => setAsTable(!asTable)}>${asTable ? "Show chart" : "Show as table"}</button>` : null}</div>
      ${asTable && table ? table() : children}
    </section>`;
  }

  function SeriesTable({ time, series, format }) {
    const rows = time.map((stamp, index) => index).reverse().slice(0, 200);
    return html`<div class="scroll"><table><thead><tr><th>Time</th>${series.map((item) => html`<th key=${item.name}>${item.name}</th>`)}</tr></thead>
      <tbody>${rows.map((index) => html`<tr key=${index}><td>${clock(time[index])}</td>${series.map((item) => html`<td key=${item.name}>${format(item.values[index])}</td>`)}</tr>`)}</tbody></table></div>`;
  }

  // --- the line chart: one axis, a legend above, a crosshair that reads every series ------------------------------
  function LineChart({ time, series, format, height = 230, area = false, zero = false, guides = [] }) {
    const [ref, width] = useWidth();
    const [hover, setHover] = useState(null);
    const margin = { top: 8, right: 14, bottom: 22, left: 52 };
    const stamps = useMemo(() => time.map((stamp) => new Date(stamp).getTime()), [time]);
    const values = series.flatMap((item) => item.values).filter(finite).concat(guides.map((guide) => guide.value).filter(finite));
    if (stamps.length < 2 || !values.length) return html`<div ref=${ref} class="empty">Not enough history yet: a point is added at every decision and every hour.</div>`;
    let low = Math.min(...values), high = Math.max(...values);
    if (zero) { low = Math.min(low, 0); high = Math.max(high, 0); }
    const pad = (high - low) * 0.08 || Math.abs(high) * 0.002 || 1;
    const ticks = niceTicks(low - (zero && low === 0 ? 0 : pad), high + (zero && high === 0 ? 0 : pad), 4);
    const yLow = Math.min(ticks[0], low - (zero && low === 0 ? 0 : pad)), yHigh = Math.max(ticks[ticks.length - 1], high + (zero && high === 0 ? 0 : pad));
    const tickStep = ticks.length > 1 ? ticks[1] - ticks[0] : undefined;
    const innerW = width - margin.left - margin.right, innerH = height - margin.top - margin.bottom;
    const x = (stamp) => margin.left + ((stamp - stamps[0]) / (stamps[stamps.length - 1] - stamps[0] || 1)) * innerW;
    const y = (value) => margin.top + (1 - (value - yLow) / (yHigh - yLow || 1)) * innerH;
    const path = (item) => {
      let out = "", pen = false;
      item.values.forEach((value, index) => {
        if (!finite(value)) { pen = false; return; }
        out += `${pen ? "L" : "M"}${x(stamps[index]).toFixed(1)},${y(value).toFixed(1)}`;
        pen = true;
      });
      return out;
    };
    const span = stamps[stamps.length - 1] - stamps[0];
    const xCount = Math.max(2, Math.min(6, Math.floor(innerW / 110)));
    const xTicks = Array.from({ length: xCount }, (_, index) => stamps[0] + (span * index) / (xCount - 1));
    const move = (event) => {
      const box = event.currentTarget.getBoundingClientRect();
      const target = stamps[0] + ((event.clientX - box.left - margin.left) / innerW) * span;
      let best = 0;
      stamps.forEach((stamp, index) => { if (Math.abs(stamp - target) < Math.abs(stamps[best] - target)) best = index; });
      setHover(best);
    };
    const last = (item) => { for (let index = item.values.length - 1; index >= 0; index--) if (finite(item.values[index])) return item.values[index]; return null; };
    const dots = stamps.length <= 40;
    const tipLeft = hover === null ? 0 : x(stamps[hover]);
    return html`<div ref=${ref}>
      ${series.length > 1 ? html`<div class="legend">${series.map((item, index) => html`<span key=${item.name}><span class="key" style=${{ borderColor: SERIES[index] }}></span>${item.name}<b>${format(last(item))}</b></span>`)}</div>` : null}
      <div class="chart">
        <svg height=${height} viewBox=${`0 0 ${width} ${height}`} onPointerMove=${move} onPointerLeave=${() => setHover(null)} role="img" aria-label=${series.map((item) => item.name).join(", ")}>
          ${ticks.map((tick) => html`<g key=${tick}><line x1=${margin.left} x2=${width - margin.right} y1=${y(tick)} y2=${y(tick)} stroke=${tick === 0 && zero ? "var(--text-3)" : "var(--grid)"} stroke-width="1" />
            <text x=${margin.left - 8} y=${y(tick) + 4} text-anchor="end" font-size="11" fill="var(--text-2)">${format(tick, tickStep)}</text></g>`)}
          ${xTicks.map((stamp, index) => html`<text key=${index} x=${x(stamp)} y=${height - 5} text-anchor=${index === 0 ? "start" : index === xTicks.length - 1 ? "end" : "middle"} font-size="11" fill="var(--text-2)">${clock(stamp, span > 36 * 3600e3)}</text>`)}
          ${guides.filter((guide) => finite(guide.value)).map((guide) => html`<g key=${guide.label}><line x1=${margin.left} x2=${width - margin.right} y1=${y(guide.value)} y2=${y(guide.value)} stroke="var(--text-3)" stroke-width="1" stroke-dasharray="4 4" />
            <text x=${width - margin.right} y=${y(guide.value) - 4} text-anchor="end" font-size="11" fill="var(--text-2)">${guide.label}</text></g>`)}
          ${area ? html`<path d=${`${path(series[0])}L${x(stamps[stamps.length - 1]).toFixed(1)},${y(0).toFixed(1)}L${x(stamps[0]).toFixed(1)},${y(0).toFixed(1)}Z`} fill=${SERIES[0]} opacity="0.16" />` : null}
          ${series.map((item, index) => html`<path key=${item.name} d=${path(item)} fill="none" stroke=${SERIES[index]} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`)}
          ${dots ? series.map((item, index) => item.values.map((value, position) => (finite(value) ? html`<circle key=${`${item.name}${position}`} cx=${x(stamps[position])} cy=${y(value)} r="3" fill=${SERIES[index]} stroke="var(--surface)" stroke-width="1.5" />` : null))) : null}
          ${hover !== null ? html`<g><line x1=${tipLeft} x2=${tipLeft} y1=${margin.top} y2=${height - margin.bottom} stroke="var(--text-3)" stroke-width="1" />
            ${series.map((item, index) => (finite(item.values[hover]) ? html`<circle key=${item.name} cx=${tipLeft} cy=${y(item.values[hover])} r="4.5" fill=${SERIES[index]} stroke="var(--surface)" stroke-width="2" />` : null))}</g>` : null}
        </svg>
        ${hover !== null ? html`<div class="tip" style=${{ top: margin.top, ...(tipLeft > width / 2 ? { right: width - tipLeft + 12 } : { left: tipLeft + 12 }) }}>
          <div class="when">${clock(time[hover])}</div>
          ${series.map((item, index) => html`<div class="row" key=${item.name}><span class="key" style=${{ borderColor: SERIES[index] }}></span><b>${format(item.values[hover])}</b><span class="muted">${item.name}</span></div>`)}
        </div>` : null}
      </div>
    </div>`;
  }

  function Sparkline({ values }) {
    const points = values.filter(finite);
    if (points.length < 2) return html`<span class="muted">–</span>`;
    const low = Math.min(...points), high = Math.max(...points), w = 90, h = 22;
    const d = points.map((value, index) => `${index ? "L" : "M"}${((index / (points.length - 1)) * w).toFixed(1)},${(h - 2 - ((value - low) / (high - low || 1)) * (h - 4)).toFixed(1)}`).join("");
    return html`<svg width=${w} height=${h} style=${{ verticalAlign: "middle" }} aria-hidden="true"><path d=${d} fill="none" stroke="var(--series-1)" stroke-width="1.5" /></svg>`;
  }

  // A bar from a centre line: right for positive, left for negative, scaled to the column's largest value.
  function SignedBar({ value, scale }) {
    if (!finite(value) || !scale) return html`<span class="bar-cell"></span>`;
    const share = Math.min(1, Math.abs(value) / scale) * 50;
    return html`<span class="bar-cell"><i style=${value >= 0 ? { left: "50%", width: `${share}%` } : { right: "50%", width: `${share}%` }}></i></span>`;
  }

  function Meter({ label, value, limit, format, note }) {
    const share = finite(value) && finite(limit) && limit > 0 ? Math.abs(value) / limit : null;
    // A limit that is acting holds its measure at the limit, and prices move it a little between decisions: that is "at the limit", not a breach.
    const level = share === null ? null : share > 1.03 ? "critical" : share >= 0.8 ? "warning" : null;
    const word = share === null ? "" : share > 1.03 ? "over" : share >= 0.97 ? "at the limit" : "close";
    return html`<div class="meter">
      <div class="top"><span>${label}${note ? html` <span class="muted small">${note}</span>` : null}</span>
        <span><b>${format(value)}</b> <span class="muted">${finite(limit) ? `of ${format(limit)} allowed` : "no limit set"}</span>${level ? html` <${Status} level=${level} label=${word} />` : null}</span></div>
      <div class="track"><div class="fill" style=${{ width: `${Math.min(100, (share || 0) * 100)}%`, background: level ? `var(--${level})` : "var(--series-1)" }}></div></div>
    </div>`;
  }

  // --- the correlation heatmap: blue (move opposite) - gray (unrelated) - red (move together) ---------------------
  function mix(from, to, share) {
    const a = from.match(/\w\w/g).map((part) => parseInt(part, 16)), b = to.match(/\w\w/g).map((part) => parseInt(part, 16));
    return `rgb(${a.map((channel, index) => Math.round(channel + (b[index] - channel) * share)).join(",")})`;
  }

  function Heatmap({ names, matrix }) {
    const [tip, setTip] = useState(null);
    const style = getComputedStyle(document.documentElement);
    const hex = (name) => style.getPropertyValue(name).trim();
    const fill = (value) => mix(hex("--pole-mid"), hex(value >= 0 ? "--pole-warm" : "--pole-cool"), Math.min(1, Math.abs(value)));
    return html`<div class="scroll">
      <table class="heat"><thead><tr><th></th>${names.map((name) => html`<th key=${name}>${name}</th>`)}</tr></thead>
      <tbody>${names.map((row, i) => html`<tr key=${row}><th class="row">${row}</th>${names.map((column, j) => html`<td key=${column} class="cell" tabindex="0"
        style=${{ background: fill(matrix[i][j]), color: Math.abs(matrix[i][j]) > 0.55 ? "#ffffff" : "var(--text)" }}
        onPointerEnter=${() => setTip(`${row} and ${column}: ${matrix[i][j].toFixed(2)}`)} onFocus=${() => setTip(`${row} and ${column}: ${matrix[i][j].toFixed(2)}`)}
        onPointerLeave=${() => setTip(null)}>${matrix[i][j].toFixed(2)}</td>`)}</tr>`)}</tbody></table>
      <div class="legend" style=${{ marginTop: 8 }}>
        <span><span class="dot" style=${{ background: "var(--pole-cool)", borderRadius: 2 }}></span> −1 move opposite</span>
        <span><span class="dot" style=${{ background: "var(--pole-mid)", borderRadius: 2, border: "1px solid var(--line)" }}></span> 0 unrelated</span>
        <span><span class="dot" style=${{ background: "var(--pole-warm)", borderRadius: 2 }}></span> +1 move together</span>
        <span class="muted">${tip || ""}</span>
      </div></div>`;
  }

  // --- sections ---------------------------------------------------------------------------------------------------
  function groupStrategies(snapshot, history) {
    const units = {};
    Object.entries(snapshot.sleeves || {}).forEach(([id, row]) => {
      const name = id.includes("__") ? id.split("__")[0] : id;
      const wanted = (row.allocated_weight || 0) * (row.own_weight || 0);
      const unit = units[name] || (units[name] = { name, rule: row.strategy, members: 0, long: 0, short: 0, pnl: 0, instruments: new Set(), lastBar: null, lastAction: null, disabled: false });
      unit.members += 1;
      unit.pnl += row.pnl || 0;
      if (wanted > 0) unit.long += wanted; else unit.short += -wanted;
      if (wanted !== 0) unit.instruments.add(coin(row.instrument));
      if (row.last_action_bar && (!unit.lastBar || row.last_action_bar > unit.lastBar)) { unit.lastBar = row.last_action_bar; unit.lastAction = row.last_action; }
      unit.disabled = unit.disabled || !!row.disabled;
    });
    const risk = (snapshot.exposure || {}).sleeve_risk_share || {};
    return Object.values(units).map((unit) => ({ ...unit, risk: risk[unit.name], series: ((history || {}).strategies || {})[unit.name] || [] })).sort((a, b) => (b.long + b.short) - (a.long + a.short));
  }

  function Strategies({ snapshot, history }) {
    const units = groupStrategies(snapshot, history);
    if (!units.length) return html`<div class="empty">No strategies in this snapshot.</div>`;
    const scale = Math.max(...units.map((unit) => Math.abs(unit.pnl)), 1e-9);
    return html`<div class="scroll"><table><thead><tr><th>Strategy</th><th class="text">Rule</th><th class="text">Holds</th><th>Wants long</th><th>Wants short</th><th>Share of risk</th><th>Profit</th><th>Since start</th><th class="text">Last change</th></tr></thead>
      <tbody>${units.map((unit) => html`<tr key=${unit.name}><td>${unit.name}${unit.disabled ? html`<span class="tag">paused</span>` : null}</td><td class="text muted">${unit.rule}${unit.members > 1 ? ` (${unit.members} coins)` : ""}</td>
        <td class="text">${unit.instruments.size ? (unit.instruments.size > 4 ? `${unit.instruments.size} coins` : [...unit.instruments].join(", ")) : html`<span class="muted">flat</span>`}</td>
        <td>${unit.long ? pct(unit.long) : "–"}</td><td>${unit.short ? pct(unit.short) : "–"}</td><td>${pct(unit.risk, 0)}</td>
        <td>${signedMoney(unit.pnl)}<${SignedBar} value=${unit.pnl} scale=${scale} /></td><td><${Sparkline} values=${unit.series} /></td>
        <td class="text muted">${unit.lastBar ? `${unit.lastAction || "change"}, ${clock(unit.lastBar)}` : "–"}</td></tr>`)}</tbody></table>
      ${finite(snapshot.residual_pnl) ? html`<p class="small">Not credited to any strategy (fees, slippage, funding and rounding): <b>${signedMoney(snapshot.residual_pnl)}</b></p>` : null}
      <p class="muted small">“Wants” is what each strategy asks for, as a share of equity, before opposite requests are netted and the limits are applied. Positions below are what the book actually holds.</p></div>`;
  }

  function Positions({ snapshot }) {
    const [all, setAll] = useState(false);
    const equity = snapshot.equity || 0;
    const rows = Object.entries(snapshot.instruments || {}).map(([name, row]) => ({ name, ...row, value: (row.units || 0) * (row.price || 0) }))
      .filter((row) => all || row.units).sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
    const scale = Math.max(...rows.map((row) => Math.abs(row.weight || 0)), 1e-9);
    const share = (snapshot.exposure || {}).risk_share || {};
    return html`<div>
      ${rows.length ? html`<div class="scroll"><table><thead><tr><th>Coin</th><th>Side</th><th>Units</th><th>Price</th><th>Value</th><th>Share of equity</th><th>Share of risk</th><th>Realised</th><th>Fees</th><th>Funding</th></tr></thead>
        <tbody>${rows.map((row) => html`<tr key=${row.name}><td>${coin(row.name)}</td><td>${row.units > 0 ? "long" : row.units < 0 ? "short" : "–"}</td><td>${row.units ? price(row.units) : "–"}</td><td>${price(row.price)}</td>
          <td>${row.units ? money(row.value) : "–"}</td><td>${row.units ? pct(row.weight, 1, true) : "–"}<${SignedBar} value=${row.weight} scale=${scale} /></td><td>${pct(share[row.name], 0)}</td>
          <td>${signedMoney(row.realized_pnl)}</td><td>${money(row.fees)}</td><td>${signedMoney(row.funding)}</td></tr>`)}</tbody></table></div>`
        : html`<div class="empty">The book holds nothing right now${equity ? "" : " (no equity reported)"}.</div>`}
      <button class="linkish" onClick=${() => setAll(!all)}>${all ? "Only coins held" : "Show every coin the book may trade"}</button>
    </div>`;
  }

  function Limits({ snapshot }) {
    const exposure = snapshot.exposure || {}, limits = snapshot.limits || {};
    const dayLoss = snapshot.day_start_equity ? Math.max(0, 1 - snapshot.equity / snapshot.day_start_equity) : null;
    const groups = Object.entries(limits.groups || {});
    return html`<div>
      <${Meter} label="Gross exposure" note="longs plus shorts" value=${snapshot.gross} limit=${limits.max_gross_exposure} format=${times} />
      <${Meter} label="Net exposure" note="longs minus shorts" value=${snapshot.net} limit=${limits.max_net_exposure} format=${times} />
      <${Meter} label="Bitcoin beta" note="how much of a BTC move the book takes" value=${exposure.beta_exposure} limit=${limits.max_beta_exposure} format=${times} />
      <${Meter} label="Volatility" note="yearly, assuming coins move together" value=${exposure.stressed_volatility} limit=${limits.max_portfolio_vol} format=${(value) => pct(value, 0)} />
      <${Meter} label="Drawdown" note="below the highest equity" value=${snapshot.drawdown} limit=${limits.max_drawdown} format=${(value) => pct(value, 1)} />
      <${Meter} label="Loss today" value=${dayLoss} limit=${limits.daily_loss_limit} format=${(value) => pct(value, 1)} />
      ${groups.map(([name, caps]) => html`<${Meter} key=${name} label=${`Group “${name}” gross`} value=${((exposure.groups || {})[name] || {}).gross} limit=${caps.max_gross} format=${times} />`)}
      ${(snapshot.risk_actions || []).length ? html`<p class="small"><${Status} level="warning" label=${`Limits acted at the last decision: ${snapshot.risk_actions.map((action) => (typeof action === "string" ? action : action.rule || JSON.stringify(action))).join(", ")}`} /></p>`
        : html`<p class="muted small">No limit changed the targets at the last decision.</p>`}
    </div>`;
  }

  function Fills({ fills }) {
    if (!fills.length) return html`<div class="empty">No fills yet. A book trades only when a strategy's signal changes or a position drifts outside its band.</div>`;
    return html`<div class="scroll"><table><thead><tr><th>Time</th><th>Coin</th><th>Side</th><th>Units</th><th>Price</th><th>Value</th><th>Fee</th><th class="text">Why</th><th class="text">Strategies</th></tr></thead>
      <tbody>${fills.map((fill, index) => html`<tr key=${index}><td>${clock(fill.time)}</td><td>${coin(fill.instrument)}</td><td>${fill.side}</td><td>${price(fill.units)}</td><td>${price(fill.price)}</td>
        <td>${money((fill.units || 0) * (fill.price || 0))}</td><td>${finite(fill.fee) ? `$${fill.fee.toFixed(4)}` : "–"}</td><td class="text">${fill.reason || ""}${fill.liquidity === "maker" ? html`<span class="tag">maker</span>` : null}</td><td class="text muted">${(fill.strategies || []).join(", ")}</td></tr>`)}</tbody></table></div>`;
  }

  function Alerts({ alerts }) {
    if (!alerts.length) return html`<div class="empty">No alerts.</div>`;
    return html`<div class="scroll">${alerts.map((alert, index) => html`<div class="alert" key=${index}>
      <${Status} level=${alert.kind === "cleared" ? "good" : alert.level === "ERROR" || alert.level === "CRITICAL" ? "critical" : "warning"} label=${alert.kind === "cleared" ? "Cleared" : "Alert"} />
      <span class="muted small"> · ${clock(alert.time)}${alert.book ? ` · ${alert.book}` : ""}</span><div>${alert.message}</div></div>`)}</div>`;
  }

  function System({ system }) {
    const [allCosts, setAllCosts] = useState(false);
    if (!system) return html`<div class="empty">Loading…</div>`;
    const costs = [...(system.costs || [])].sort((a, b) => (b.ratio || 0) - (a.ratio || 0));
    const over = costs.filter((row) => row.ratio > 1).length;
    const research = system.research || {};
    return html`<div class="grid">
      <${Card} title="Background jobs" sub="judged by when each last wrote something" span="c6">
        <table><thead><tr><th>Job</th><th class="text">What it does</th><th>Last wrote</th><th>State</th></tr></thead>
        <tbody>${system.health.map((job) => html`<tr key=${job.job}><td>${job.job}</td><td class="text muted">${job.what}</td><td>${ago(job.seconds_since)}</td>
          <td><${Status} level=${job.ok ? "good" : "critical"} label=${job.ok ? "Running" : "Stale"} /></td></tr>`)}</tbody></table>
        <p class="small muted">Prediction-market test (H4 and H5): ${research.done ? "the confirmatory run is done; write the reports."
          : `${research.snapshots_cached} option snapshots and ${research.events_cached} events stored; the one confirmatory run is on ${research.confirmatory_run_on}, in ${research.days_until} days.`}</p>
      <//>
      <${Card} title="Kraken's real cost against the assumed slippage" sub=${costs.length ? `${costs[0].samples} samples; per side, at $1,000 an order, in basis points` : ""} span="c6">
        ${costs.length ? html`<div><p class="small">${over ? html`<${Status} level="warning" label=${`${over} of ${costs.length} coins cost more than the backtest assumes`} />`
            : html`<${Status} level="good" label=${`All ${costs.length} coins cost less than the backtest assumes`} />`}</p>
          <div class="scroll"><table><thead><tr><th>Coin</th><th>Half-spread</th><th>90th pct</th><th>Measured</th><th>Assumed</th><th>Measured ÷ assumed</th></tr></thead>
          <tbody>${(allCosts ? costs : costs.slice(0, 10)).map((row) => html`<tr key=${row.symbol}><td>${row.symbol.replace("PF_", "").replace("USD", "")}</td><td>${number(row.half_spread_median, 1)}</td><td>${number(row.half_spread_p90, 1)}</td>
            <td>${number(row.measured_bps, 1)}</td><td>${number(row.assumed_bps, 0)}</td><td>${number(row.ratio, 2)}${row.ratio > 1 ? " !" : ""}</td></tr>`)}</tbody></table></div>
          <button class="linkish" onClick=${() => setAllCosts(!allCosts)}>${allCosts ? "Ten most expensive" : `All ${costs.length} coins`}</button></div>`
          : html`<div class="empty">Nothing recorded yet.</div>`}
      <//>
    </div>`;
  }

  function Book({ name, range }) {
    const [state, setState] = useState({ snapshot: null, history: null, fills: [], alerts: [], error: null });
    useEffect(() => {
      let alive = true;
      const load = () => Promise.all([get(`/api/book/${encodeURIComponent(name)}`), get(`/api/book/${encodeURIComponent(name)}/history`), get(`/api/book/${encodeURIComponent(name)}/fills`), get(`/api/alerts?book=${encodeURIComponent(name)}`)])
        .then(([snapshot, history, fills, alerts]) => alive && setState({ snapshot, history, fills, alerts, error: null }))
        .catch((error) => alive && setState((previous) => ({ ...previous, error: String(error) })));
      setState({ snapshot: null, history: null, fills: [], alerts: [], error: null });
      load();
      const timer = setInterval(load, REFRESH_MS);
      return () => { alive = false; clearInterval(timer); };
    }, [name]);
    const { snapshot, history, fills, alerts, error } = state;
    const view = useMemo(() => {
      if (!history || !history.time.length) return null;
      const end = new Date(history.time[history.time.length - 1]).getTime();
      const from = range === null ? 0 : history.time.findIndex((stamp) => new Date(stamp).getTime() >= end - range * 86400e3);
      const cut = (values) => values.slice(Math.max(0, from));
      const equity = cut(history.equity), bench = cut(history.benchmark);
      const firstEquity = equity.find(finite), firstBench = bench.find(finite);
      return { time: cut(history.time), equity, drawdown: cut(history.drawdown).map((value) => (finite(value) ? -value : null)), gross: cut(history.gross), net: cut(history.net), beta: cut(history.beta),
               bookIndex: equity.map((value) => (finite(value) && firstEquity ? (value / firstEquity) * 100 : null)), benchIndex: bench.map((value) => (finite(value) && firstBench ? (value / firstBench) * 100 : null)) };
    }, [history, range]);
    if (error && !snapshot) return html`<div class="error">Could not load “${name}”: ${error}</div>`;
    if (!snapshot || !view) return html`<p class="muted">Loading ${name}…</p>`;
    const exposure = snapshot.exposure || {}, limits = snapshot.limits || {}, correlation = snapshot.strategy_correlation;
    const change = snapshot.initial_equity ? snapshot.equity / snapshot.initial_equity - 1 : null;
    const held = Object.values(snapshot.instruments || {}).filter((row) => row.units).length;
    // On an axis the second argument is the distance between ticks: labels get just the decimals that step needs.
    const decimals = (step, usual) => {
      if (step === undefined) return usual;
      for (let digits = 0; digits < 5; digits++) if (Math.abs(step * 10 ** digits - Math.round(step * 10 ** digits)) < 1e-6) return digits;
      return 5;
    };
    const index = (value, step) => (finite(value) ? value.toFixed(decimals(step, 2)) : "–");
    const percent = (value, step) => pct(value, step === undefined ? 2 : decimals(step * 100, 2));
    const multiple = (value, step) => times(value, decimals(step, 2));
    return html`<div>
      ${error ? html`<div class="error">The last refresh failed (${error}); showing the previous data.</div>` : null}
      <div class="tiles">
        <${Tile} label="Equity" value=${money(snapshot.equity)} note=${`started with ${money(snapshot.initial_equity)}`} />
        <${Tile} label="Since start" value=${pct(change, 2, true)} note=${signedMoney(snapshot.equity - snapshot.initial_equity)} />
        <${Tile} label="Drawdown" value=${pct(snapshot.drawdown, 2)} note=${`stops at ${pct(limits.max_drawdown, 0)}`} />
        <${Tile} label="Gross exposure" value=${times(snapshot.gross)} note=${`net ${times(snapshot.net)}`} />
        <${Tile} label="Bitcoin beta" value=${times(exposure.beta_exposure)} note="a 1% BTC move ≈ this × 1%" />
        <${Tile} label="Volatility" value=${pct(exposure.volatility, 0)} note=${`stressed ${pct(exposure.stressed_volatility, 0)} a year`} />
        <${Tile} label="Positions" value=${String(held)} note=${snapshot.pending_orders ? `${snapshot.pending_orders} order${snapshot.pending_orders > 1 ? "s" : ""} resting on the exchange` : `${fills.length} fills so far`} />
        <${Tile} label="Independent bets" value=${finite(exposure.effective_bets) ? exposure.effective_bets.toFixed(1) : "–"} note="higher = more diversified" />
      </div>
      <div class="grid">
        <${Card} title="The book against Bitcoin" sub="both start at 100" span="c8"
          table=${() => html`<${SeriesTable} time=${view.time} series=${[{ name: "Book", values: view.bookIndex }, { name: "Bitcoin", values: view.benchIndex }]} format=${index} />`}>
          <${LineChart} time=${view.time} series=${[{ name: "Book", values: view.bookIndex }, { name: "Bitcoin", values: view.benchIndex }]} format=${index} height=${260} />
        <//>
        <${Card} title="Risk against its limits" span="c4"><${Limits} snapshot=${snapshot} /><//>
        <${Card} title="Drawdown" sub="below the highest equity so far" span="c6"
          table=${() => html`<${SeriesTable} time=${view.time} series=${[{ name: "Drawdown", values: view.drawdown }]} format=${(value) => pct(value, 2)} />`}>
          <${LineChart} time=${view.time} series=${[{ name: "Drawdown", values: view.drawdown }]} format=${percent} area=${true} zero=${true} height=${200} />
        <//>
        <${Card} title="Exposure" sub="as a multiple of equity" span="c6"
          table=${() => html`<${SeriesTable} time=${view.time} series=${[{ name: "Gross", values: view.gross }, { name: "Net", values: view.net }, { name: "Bitcoin beta", values: view.beta }]} format=${times} />`}>
          <${LineChart} time=${view.time} series=${[{ name: "Gross", values: view.gross }, { name: "Net", values: view.net }, { name: "Bitcoin beta", values: view.beta }]} format=${multiple} zero=${true} height=${200} />
        <//>
        <${Card} title="Strategies" sub="each one's request and its profit"><${Strategies} snapshot=${snapshot} history=${history} /><//>
        <${Card} title="Positions" sub=${`as of ${clock(snapshot.timestamp)}`} span=${correlation && correlation.matrix ? "c8" : ""}><${Positions} snapshot=${snapshot} /><//>
        ${correlation && correlation.matrix ? html`<${Card} title="How alike the strategies are" sub=${`return correlation, last ${correlation.bars} bars; average ${correlation.average_correlation.toFixed(2)}`} span="c4">
          <${Heatmap} names=${correlation.names} matrix=${correlation.matrix} /><//>` : null}
        <${Card} title="Fills" sub=${snapshot.execution === "maker_first" ? "newest first; orders rest at the touch first (maker), then go to market" : "newest first"} span="c8"><${Fills} fills=${fills} /><//>
        <${Card} title="Alerts" span="c4"><${Alerts} alerts=${alerts} /><//>
      </div>
    </div>`;
  }

  function App() {
    const [books, setBooks] = useState(null);
    const [system, setSystem] = useState(null);
    const [error, setError] = useState(null);
    const [selected, setSelected] = useState(() => decodeURIComponent(location.hash.slice(1)) || null);
    const [range, setRange] = useState(null);
    const [loaded, setLoaded] = useState(null);
    const load = useCallback(() => Promise.all([get("/api/books"), get("/api/system")])
      .then(([list, state]) => { setBooks(list); setSystem(state); setError(null); setLoaded(new Date()); })
      .catch((problem) => setError(String(problem))), []);
    useEffect(() => { load(); const timer = setInterval(load, REFRESH_MS); return () => clearInterval(timer); }, [load]);
    const current = books && (books.find((book) => book.name === selected) || books.find((book) => book.reporting) || books[0]);
    const pick = (name) => { setSelected(name); history.replaceState(null, "", `#${encodeURIComponent(name)}`); };
    return html`<div>
      <header>
        <h1>CryptoQuant books</h1>
        <div class="books">${(books || []).map((book) => html`<button key=${book.name} class="book" aria-pressed=${current && current.name === book.name} onClick=${() => pick(book.name)}>
          <b>${book.name}<span class=${`tag ${book.mode === "live" ? "live" : ""}`}>${book.mode === "live" ? "real money" : "paper"}</span></b>
          <span class="pill">${money(book.equity)} · ${pct(book.change, 2, true)}</span><br />
          <span class="pill"><span class="dot" style=${{ background: book.reporting ? "var(--good)" : "var(--text-3)" }}></span>${book.reporting ? "running" : "stopped"}, ${ago(book.age_seconds)}</span>
        </button>`)}</div>
        <span class="spacer"></span>
        <div class="seg" role="group" aria-label="Time range">${RANGES.map(([label, days]) => html`<button key=${label} aria-pressed=${range === days} onClick=${() => setRange(days)}>${label}</button>`)}</div>
        <span class="small muted">${loaded ? `updated ${clock(loaded, false)}, refreshes each minute` : ""}</span>
      </header>
      ${error ? html`<div class="error">Can't reach the dashboard server: ${error}</div>` : null}
      ${books && !books.length ? html`<div class="empty">No book has written a snapshot yet. Start one with main.py --portfolio-runtime paper.</div>` : null}
      ${current ? html`<${Book} key=${current.name} name=${current.name} range=${range} />` : null}
      <div style=${{ height: 12 }}></div>
      <${System} system=${system} />
      <p class="small muted" style=${{ marginTop: 14 }}>Read-only: this page shows what the books stored in the database; it cannot place or cancel orders. Times are in this computer's time zone.</p>
    </div>`;
  }

  // A render error shows on the page instead of leaving it blank.
  class Boundary extends React.Component {
    constructor(props) { super(props); this.state = { error: null }; }
    static getDerivedStateFromError(error) { return { error }; }
    componentDidCatch(error) { console.error("dashboard render error:", error && error.stack ? error.stack : error); }
    render() { return this.state.error ? html`<div class="error">The page hit an error: ${String(this.state.error)}. Reload it; if it stays, the server log has the details.</div>` : this.props.children; }
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${Boundary}><${App} /><//>`);
})();
