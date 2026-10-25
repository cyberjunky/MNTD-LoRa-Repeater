/* MNTD LoRa Repeater GUI - vanilla JS, no dependencies (works on an offline LAN).
   Talks to the FastAPI backend (/api/*) and the WebSocket live feed (/ws). */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : k === "html" ? (n.innerHTML = v) : n.setAttribute(k, v);
    for (const k of kids) n.append(k);
    return n;
  };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtTs = (t) => t ? new Date(t * 1000).toLocaleString(undefined, { hour12: false }) : "";
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleTimeString(undefined, { hour12: false }) : "";
  const ago = (t) => { if (!t) return "never"; const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${(s / 60) | 0}m ago` : s < 86400 ? `${(s / 3600) | 0}h ago` : `${(s / 86400) | 0}d ago`; };
  const n1 = (v, d = 1) => (v == null || isNaN(v)) ? "–" : Number(v).toFixed(d);
  const hex = (n) => n == null ? "–" : "!" + (n >>> 0).toString(16).padStart(8, "0");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const api = (p) => fetch(p).then((r) => { if (!r.ok) throw new Error(`${p}: ${r.status}`); return r.json(); });

  // ---------------------------------------------------------------- theme / tabs
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch {}
  $("#theme").onclick = () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch {}
    refreshAll();
  };
  let tab = "overview";
  try { tab = localStorage.getItem("tab") || tab; } catch {}
  const showTab = (name) => {
    tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
    try { localStorage.setItem("tab", name); } catch {}
    refreshTab(name);
  };
  $("#tabs").onclick = (e) => e.target.dataset.tab && showTab(e.target.dataset.tab);
  const hours = () => Number($("#range").value);
  $("#range").onchange = () => refreshAll();

  // ---------------------------------------------------------------- tooltip
  const tip = $("#tooltip");
  const showTip = (html, x, y) => { tip.innerHTML = html; tip.hidden = false; const r = tip.getBoundingClientRect(); tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px"; tip.style.top = Math.max(8, y - r.height - 10) + "px"; };
  const hideTip = () => (tip.hidden = true);

  // ---------------------------------------------------------------- charts (SVG)
  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const series = () => [css("--series-1"), css("--series-2"), css("--series-3"), css("--series-4")];
  const niceMax = (v) => { if (v <= 0) return 1; const p = 10 ** Math.floor(Math.log10(v)); const m = v / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; };

  function frame(container, { left = 36, right = 8, top = 10, bottom = 22 } = {}) {
    container.innerHTML = "";
    const W = container.clientWidth || 400, H = container.clientHeight || 220;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
    container.append(svg);
    return { svg, W, H, x0: left, x1: W - right, y0: top, y1: H - bottom };
  }
  function yAxis(f, max, ticks = 4, fmt = (v) => v) {
    const g = svgEl("g", { class: "grid" }), a = svgEl("g", { class: "axis" });
    for (let i = 0; i <= ticks; i++) {
      const v = (max * i) / ticks, y = f.y1 - ((f.y1 - f.y0) * i) / ticks;
      g.append(svgEl("line", { x1: f.x0, x2: f.x1, y1: y, y2: y }));
      const t = svgEl("text", { x: f.x0 - 6, y: y + 4, "text-anchor": "end" }); t.textContent = fmt(v); a.append(t);
    }
    f.svg.append(g, a);
  }
  function legend(container, names, colors) {
    if (names.length < 2) return;
    const l = el("div", { class: "legend" });
    names.forEach((n, i) => l.append(el("span", {}, el("i", { style: `background:${colors[i]}` }), n)));
    container.append(l);
  }
  const empty = (c, msg = "no data yet") => { c.innerHTML = ""; c.append(el("div", { class: "empty" }, msg)); };

  /** Time buckets, up to 4 series: stacked bars with hover. data = [{t, a, b}], keys = ["a","b"] */
  function stackedBars(container, data, keys, names, { fmtVal = (v) => v } = {}) {
    if (!data.length) return empty(container);
    const f = frame(container), col = series();
    const max = niceMax(Math.max(...data.map((d) => keys.reduce((s, k) => s + (d[k] || 0), 0))));
    yAxis(f, max);
    const bw = (f.x1 - f.x0) / data.length, gap = Math.min(2, bw * 0.15);
    const sy = (v) => f.y1 - ((f.y1 - f.y0) * v) / max;
    data.forEach((d, i) => {
      let acc = 0; const x = f.x0 + i * bw + gap / 2, w = Math.max(1, bw - gap);
      keys.forEach((k, j) => {
        const v = d[k] || 0; if (!v) return;
        const y = sy(acc + v), h = sy(acc) - y - (acc ? 2 : 0);
        f.svg.append(svgEl("rect", { class: "bar", x, y, width: w, height: Math.max(0, h), fill: col[j] }));
        acc += v;
      });
      const hit = svgEl("rect", { class: "hit", x: f.x0 + i * bw, y: f.y0, width: bw, height: f.y1 - f.y0 });
      hit.addEventListener("mousemove", (e) => showTip(`<b>${fmtTs(d.t)}</b><br>` + keys.map((k, j) => `<span style="color:${col[j]}">■</span> ${names[j]}: ${fmtVal(d[k] || 0)}`).join("<br>"), e.clientX, e.clientY));
      hit.addEventListener("mouseleave", hideTip);
      f.svg.append(hit);
    });
    const ax = svgEl("g", { class: "axis" });
    [0, Math.floor(data.length / 2), data.length - 1].forEach((i) => { if (!data[i]) return; const t = svgEl("text", { x: f.x0 + i * bw + bw / 2, y: f.H - 6, "text-anchor": i === 0 ? "start" : i === data.length - 1 ? "end" : "middle" }); t.textContent = fmtTime(data[i].t); ax.append(t); });
    f.svg.append(ax);
    legend(container, names, col);
  }

  /** Horizontal category bars: data = [{label, value, extra}] */
  function hBars(container, data, { color = 0, fmtVal = (v) => v, extra = () => "" } = {}) {
    if (!data.length) return empty(container);
    const rowH = 22, H = Math.max(60, data.length * rowH + 10);
    container.style.height = H + "px";
    const f = frame(container, { left: 110, top: 4, bottom: 4 }), col = series()[color];
    const max = niceMax(Math.max(...data.map((d) => d.value)));
    data.forEach((d, i) => {
      const y = f.y0 + i * rowH + 4, w = ((f.x1 - f.x0) * d.value) / max;
      const lab = svgEl("text", { class: "dlabel", x: f.x0 - 8, y: y + 12, "text-anchor": "end" }); lab.textContent = d.label; f.svg.append(lab);
      f.svg.append(svgEl("rect", { class: "bar", x: f.x0, y, width: Math.max(1, w), height: rowH - 8, fill: col }));
      const v = svgEl("text", { class: "dlabel", x: f.x0 + w + 6, y: y + 12 }); v.textContent = fmtVal(d.value) + (extra(d) ? ` · ${extra(d)}` : ""); f.svg.append(v);
    });
  }

  /** Multi-line time series: data = [{t, a, b}] */
  function lines(container, data, keys, names, { fmtVal = (v) => n1(v), min = null, max = null } = {}) {
    if (data.length < 2) return empty(container);
    const f = frame(container, { left: 44 }), col = series();
    const vals = data.flatMap((d) => keys.map((k) => d[k]).filter((v) => v != null));
    let lo = min ?? Math.min(0, ...vals), hi = max ?? niceMax(Math.max(...vals, 1e-9));
    if (lo === hi) hi = lo + 1;
    const sx = (i) => f.x0 + ((f.x1 - f.x0) * i) / (data.length - 1), sy = (v) => f.y1 - ((f.y1 - f.y0) * (v - lo)) / (hi - lo);
    const g = svgEl("g", { class: "grid" }), a = svgEl("g", { class: "axis" });
    for (let i = 0; i <= 4; i++) { const v = lo + ((hi - lo) * i) / 4, y = sy(v); g.append(svgEl("line", { x1: f.x0, x2: f.x1, y1: y, y2: y })); const t = svgEl("text", { x: f.x0 - 6, y: y + 4, "text-anchor": "end" }); t.textContent = fmtVal(v); a.append(t); }
    f.svg.append(g, a);
    keys.forEach((k, j) => {
      let d = "", pen = false;
      data.forEach((p, i) => { if (p[k] == null) { pen = false; return; } d += (pen ? "L" : "M") + sx(i).toFixed(1) + "," + sy(p[k]).toFixed(1); pen = true; });
      f.svg.append(svgEl("path", { class: "line", d, stroke: col[j] }));
    });
    const cross = svgEl("line", { class: "crosshair", y1: f.y0, y2: f.y1, visibility: "hidden" }); f.svg.append(cross);
    const hit = svgEl("rect", { class: "hit", x: f.x0, y: f.y0, width: f.x1 - f.x0, height: f.y1 - f.y0 });
    hit.addEventListener("mousemove", (e) => {
      const r = f.svg.getBoundingClientRect(); const i = Math.round(((e.clientX - r.left) * (f.W / r.width) - f.x0) / (f.x1 - f.x0) * (data.length - 1));
      const p = data[Math.max(0, Math.min(data.length - 1, i))]; if (!p) return;
      cross.setAttribute("x1", sx(i)); cross.setAttribute("x2", sx(i)); cross.setAttribute("visibility", "visible");
      showTip(`<b>${fmtTs(p.t)}</b><br>` + keys.map((k, j) => `<span style="color:${col[j]}">■</span> ${names[j]}: ${p[k] == null ? "–" : fmtVal(p[k])}`).join("<br>"), e.clientX, e.clientY);
    });
    hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("visibility", "hidden"); });
    f.svg.append(hit);
    const ax = svgEl("g", { class: "axis" });
    [0, data.length - 1].forEach((i) => { const t = svgEl("text", { x: sx(i), y: f.H - 6, "text-anchor": i ? "end" : "start" }); t.textContent = fmtTime(data[i].t); ax.append(t); });
    f.svg.append(ax);
    legend(container, names, col);
  }

  // ---------------------------------------------------------------- tiles / tables
  const tile = (label, value, sub = "", status = null) => el("div", { class: "tile" },
    el("div", { class: "label" }, ...(status ? [el("span", { class: `status ${status}` })] : []), label),
    el("div", { class: "value" }, value), el("div", { class: "sub" }, sub));
  function table(node, cols, rows, { empty: emptyMsg = "nothing yet" } = {}) {
    node.innerHTML = "";
    if (!rows.length) { node.append(el("tr", {}, el("td", { class: "empty", colspan: cols.length }, emptyMsg))); return; }
    node.append(el("tr", {}, ...cols.map((c) => el("th", {}, c.h))));
    for (const r of rows) node.append(rowEl(cols, r));
  }
  const rowEl = (cols, r) => el("tr", {}, ...cols.map((c) => { const v = c.f(r); const td = el("td", { class: c.num ? "num" : "" }); typeof v === "string" && v.startsWith("<") ? (td.innerHTML = v) : td.append(v ?? "–"); return td; }));
  const prepend = (node, cols, r) => { const tr = rowEl(cols, r); tr.classList.add("new"); const first = node.querySelector("tr:nth-child(2)"); first ? first.before(tr) : node.append(tr); if (node.rows.length > 101) node.deleteRow(-1); };

  // ---------------------------------------------------------------- columns
  const LW_COLS = [
    { h: "Time", f: (r) => fmtTime(r.ts) },
    { h: "Dir", f: (r) => `<span class="badge ${r.direction}">${r.direction}</span>` },
    { h: "Type", f: (r) => r.mtype || "" },
    { h: "DevAddr / DevEUI", f: (r) => `<span class="mono">${esc(r.devaddr || r.dev_eui || "")}</span>` },
    { h: "FCnt", f: (r) => r.fcnt, num: true }, { h: "Port", f: (r) => r.fport, num: true },
    { h: "MHz", f: (r) => n1(r.freq, 3), num: true }, { h: "DR", f: (r) => r.datr || "" }, { h: "CR", f: (r) => r.codr || "" },
    { h: "RSSI", f: (r) => r.rssi == null ? "" : n1(r.rssi, 0), num: true }, { h: "SNR", f: (r) => r.lsnr == null ? "" : n1(r.lsnr, 1), num: true },
    { h: "Bytes", f: (r) => r.size, num: true },
    { h: "CRC", f: (r) => r.direction === "down" ? "" : r.stat === 1 ? "ok" : r.stat === -1 ? '<span class="badge bad">bad</span>' : "none" },
    { h: "Forwarded", f: (r) => (Array.isArray(r.forwarded) ? r.forwarded : []).join(", ") || (r.upstream || "") },
  ];
  const nodeName = (n) => n ? (n.long_name || n.short_name || hex(n.num)) : null;
  let nodeMap = {};
  const MESH_COLS = [
    { h: "Time", f: (r) => fmtTime(r.ts) },
    { h: "From", f: (r) => `<span title="${hex(r.from_node)}">${esc(nodeName(nodeMap[r.from_node]) || hex(r.from_node))}</span>` },
    { h: "To", f: (r) => r.to_node === 0xffffffff ? "broadcast" : esc(nodeName(nodeMap[r.to_node]) || hex(r.to_node)) },
    { h: "Port", f: (r) => (r.portnum || "").replace("_APP", "") },
    { h: "Ch", f: (r) => r.channel, num: true },
    { h: "Hops", f: (r) => r.hops_used == null ? "?" : `${r.hops_used}/${r.hop_start}`, num: true },
    { h: "Relay", f: (r) => r.relay_node == null ? "" : "…" + r.relay_node.toString(16).padStart(2, "0") },
    { h: "RSSI", f: (r) => r.rssi == null ? "" : n1(r.rssi, 0), num: true }, { h: "SNR", f: (r) => r.snr == null ? "" : n1(r.snr, 1), num: true },
    { h: "Via", f: (r) => r.via_mqtt ? "MQTT" : "RF" },
    { h: "Content", f: (r) => r.text ? esc(r.text) : r.payload ? `<span class="mono muted">${esc(JSON.stringify(r.payload).slice(0, 80))}</span>` : (r.encrypted ? "🔒 encrypted" : "") },
  ];
  const NODE_COLS = [
    { h: "Node", f: (r) => `<b>${esc(r.long_name || "")}</b> <span class="muted">${esc(r.short_name || "")}</span>` },
    { h: "ID", f: (r) => `<span class="mono">${esc(r.node_id || hex(r.num))}</span>` },
    { h: "Hardware", f: (r) => r.hw_model || "" }, { h: "Role", f: (r) => r.role || "" },
    { h: "Last heard", f: (r) => ago(r.last_heard) }, { h: "Hops", f: (r) => r.hops_away, num: true },
    { h: "SNR", f: (r) => r.snr == null ? "" : n1(r.snr, 1), num: true },
    { h: "Battery", f: (r) => r.battery == null ? "" : r.battery > 100 ? "mains" : `${r.battery}%`, num: true },
    { h: "Ch util", f: (r) => r.channel_util == null ? "" : n1(r.channel_util, 1) + "%", num: true },
    { h: "Position", f: (r) => r.lat == null ? "" : `<a href="https://www.openstreetmap.org/?mlat=${r.lat}&mlon=${r.lon}#map=14/${r.lat}/${r.lon}" target="_blank" rel="noopener">${n1(r.lat, 4)}, ${n1(r.lon, 4)}</a>` },
    { h: "Via", f: (r) => r.via_mqtt ? "MQTT" : "RF" },
  ];
  const EVENT_COLS = [
    { h: "Time", f: (r) => fmtTs(r.ts) }, { h: "Source", f: (r) => r.source },
    { h: "Level", f: (r) => `<span class="badge ${r.level === "error" ? "bad" : ""}">${r.level}</span>` }, { h: "Message", f: (r) => esc(r.message) },
  ];

  // ---------------------------------------------------------------- views
  let status = null;
  async function refreshStatus() {
    status = await api("/api/status");
    $("#title").textContent = status.title;
    document.title = status.title;
    const g = status.gwmp, m = status.mesh, gps = status.gps, sx = status.meshsx, tg = status.telegram;
    const fwdOk = g.forwarder_seen_s_ago != null && g.forwarder_seen_s_ago < 120;
    const upOk = !g.upstreams.length || g.upstreams.some((u) => u.alive);
    const meshOk = sx ? fwdOk : (!m.enabled || m.connected);
    const meshTile = sx
      ? tile("Meshtastic node", `${sx.node_id}`, `${sx.long_name} · ${sx.role} · ${sx.freq_mhz} MHz ${sx.datr}${sx.last_rx_s_ago != null ? ` · rx ${sx.last_rx_s_ago}s ago` : ""}`, fwdOk ? "good" : "critical")
      : tile("Meshtastic node", m.enabled ? (m.connected ? "connected" : "offline") : "disabled", m.my_node?.long_name ? `${m.my_node.long_name} · ${m.my_node.role || ""}` : m.target, !m.enabled ? "unknown" : m.connected ? "good" : "critical");
    $("#health-dot").className = "dot " + (!fwdOk ? "critical" : (!upOk || !meshOk) ? "warning" : "good");
    const tiles = $("#tiles"); tiles.innerHTML = "";
    tiles.append(
      tile("Packet forwarder", fwdOk ? "running" : "no data", g.forwarder_seen_s_ago == null ? "never seen" : `last seen ${g.forwarder_seen_s_ago}s ago · EUI ${g.gateway_eui || "?"}`, fwdOk ? "good" : "critical"),
      tile("Upstream networks", `${g.upstreams.filter((u) => u.alive).length} / ${g.upstreams.length}`, g.upstreams.map((u) => u.name).join(", ") || "none configured", g.upstreams.length ? (upOk ? "good" : "critical") : "unknown"),
      meshTile,
      tile("GPS fix", gps.enabled ? gps.fix : "disabled", gps.sats_used != null ? `${gps.sats_used} sats used · ${gps.sats_seen} seen` : (gps.connected ? "waiting for TPV" : "gpsd not connected"), !gps.enabled ? "unknown" : gps.fix === "3D" ? "good" : gps.fix === "2D" ? "warning" : "critical"),
      tile("Time sync", gps.time_sync?.source || "–", gps.time_sync?.offset_s != null ? `offset ${(gps.time_sync.offset_s * 1000).toFixed(1)} ms · stratum ${gps.time_sync.stratum}` : "chrony not reporting", gps.time_sync?.ref_id === "GPS" ? "good" : gps.time_sync?.source ? "warning" : "unknown"),
      tile("Stored packets", `${(status.db.lorawan || 0) + (status.db.mesh || 0)}`, `${status.db.lorawan || 0} LoRaWAN · ${status.db.mesh || 0} mesh · ${status.db.nodes || 0} nodes`),
      tile("CPU temperature", status.cpu_temp_c == null ? "–" : `${n1(status.cpu_temp_c, 1)} °C`, `up ${ago(Date.now() / 1000 - status.uptime_s)}`.replace(" ago", ""), status.cpu_temp_c == null ? "unknown" : status.cpu_temp_c > 75 ? "critical" : status.cpu_temp_c > 65 ? "warning" : "good"),
    );
    if (sx) tiles.append(tile("Mesh airtime (1 h)", `${n1(sx.duty_used_pct_1h, 2)} %`, `of ${sx.duty_limit_pct} % duty cycle · relayed ${sx.counters.relayed} · sent ${sx.counters.tx}`, sx.duty_used_pct_1h > sx.duty_limit_pct * 0.8 ? "warning" : "good"));
    if (tg && tg.enabled) tiles.append(tile("Telegram", tg.connected ? `@${tg.bot || "bot"}` : "offline", `in ${tg.counters.in} · out ${tg.counters.out} · posted ${tg.counters.posted}`, tg.connected ? "good" : "critical"));
  }

  async function refreshOverview() {
    const h = hours();
    const [lw, mesh, lwp, mp] = await Promise.all([api(`/api/lorawan/summary?hours=${h}`), api(`/api/mesh/summary?hours=${h}`), api("/api/lorawan/packets?limit=12"), api("/api/mesh/packets?limit=12")]);
    $("#lw-range").textContent = `last ${h} h · ${lw.totals.total || 0} total`;
    $("#mesh-range").textContent = `last ${h} h · ${mesh.totals.total || 0} total`;
    stackedBars($("#chart-lw-time"), pivot(lw.per_bucket, lw.bucket_s, h, "direction", ["up", "down"]), ["up", "down"], ["uplinks", "downlinks"]);
    stackedBars($("#chart-mesh-time"), fill(mesh.per_bucket.map((b) => ({ t: b.t, heard: b.n - (b.tx || 0), relayed: b.relayed_by_us || 0, tx: b.tx || 0 })), mesh.bucket_s, h, ["heard", "relayed", "tx"]), ["heard", "relayed", "tx"], ["heard", "relayed by us", "sent by us"]);
    table($("#ov-lw"), LW_COLS.slice(0, 11), lwp);
    table($("#ov-mesh"), MESH_COLS.slice(0, 9).concat(MESH_COLS.slice(-1)), mp);
  }

  const LW_STATE = { oldest: null };
  async function refreshLorawan() {
    const h = hours();
    const [s, ups] = await Promise.all([api(`/api/lorawan/summary?hours=${h}`), api("/api/lorawan/upstreams")]);
    const t = s.totals, tiles = $("#lw-tiles"); tiles.innerHTML = "";
    tiles.append(tile("Uplinks", `${t.up || 0}`, `last ${h} h`), tile("Downlinks", `${t.down || 0}`, `last ${h} h`),
      tile("CRC errors", `${t.crc_bad || 0}`, t.up ? `${(100 * (t.crc_bad || 0) / t.up).toFixed(1)} % of uplinks` : ""),
      tile("Mean RSSI", t.rssi == null ? "–" : `${n1(t.rssi, 0)} dBm`, `mean SNR ${n1(t.snr, 1)} dB`),
      tile("Devices", `${s.devices.length}`, "distinct DevAddr"));
    hBars($("#chart-lw-freq"), s.by_freq.filter((r) => r.freq).map((r) => ({ label: n1(r.freq, 1) + " MHz", value: r.n, rssi: r.rssi })), { extra: (d) => `${n1(d.rssi, 0)} dBm` });
    hBars($("#chart-lw-datr"), s.by_datr.filter((r) => r.datr).map((r) => ({ label: r.datr, value: r.n })), { color: 1 });
    table($("#lw-upstreams"), [
      { h: "Name", f: (r) => r.name }, { h: "Host", f: (r) => `${r.host}:${r.port}` },
      { h: "Pushed", f: (r) => r.push_sent, num: true }, { h: "Acked", f: (r) => r.push_acked, num: true },
      { h: "Ack rate", f: (r) => r.push_sent ? `${(100 * r.push_acked / r.push_sent).toFixed(1)} %` : "", num: true },
      { h: "Downlinks", f: (r) => r.pull_resp, num: true }, { h: "Last PULL_ACK", f: (r) => ago(r.last_pull_ack) },
    ], ups, { empty: "no upstream networks configured (see /etc/lorarepeater/config.yaml)" });
    table($("#lw-devices"), [
      { h: "DevAddr", f: (r) => `<span class="mono">${r.devaddr}</span>` }, { h: "Packets", f: (r) => r.n, num: true },
      { h: "Last FCnt", f: (r) => r.fcnt, num: true }, { h: "RSSI", f: (r) => n1(r.rssi, 0), num: true }, { h: "SNR", f: (r) => n1(r.snr, 1), num: true }, { h: "Last seen", f: (r) => ago(r.last_ts) },
    ], s.devices);
    await loadLwLog(true);
  }
  async function loadLwLog(reset) {
    const q = new URLSearchParams({ limit: 50 });
    if ($("#lw-dir").value) q.set("direction", $("#lw-dir").value);
    if ($("#lw-devaddr").value.trim()) q.set("devaddr", $("#lw-devaddr").value.trim());
    if (!reset && LW_STATE.oldest) q.set("before_id", LW_STATE.oldest);
    const rows = await api(`/api/lorawan/packets?${q}`);
    if (reset) table($("#lw-log"), LW_COLS, rows); else rows.forEach((r) => $("#lw-log").append(rowEl(LW_COLS, r)));
    if (rows.length) LW_STATE.oldest = rows[rows.length - 1].id;
  }
  $("#lw-more").onclick = () => loadLwLog(false);
  $("#lw-dir").onchange = $("#lw-devaddr").onchange = () => loadLwLog(true);

  const MESH_STATE = { oldest: null };
  async function refreshMesh() {
    const h = hours();
    const [s, nodes, msgs] = await Promise.all([api(`/api/mesh/summary?hours=${h}`), api("/api/mesh/nodes"), api("/api/mesh/packets?limit=30&text_only=true")]);
    nodeMap = Object.fromEntries(nodes.map((n) => [n.num, n]));
    const t = s.totals, tiles = $("#mesh-tiles"); tiles.innerHTML = "";
    tiles.append(tile("Packets heard", `${t.total || 0}`, `last ${h} h · ${t.undecodable || 0} undecodable`),
      tile("Relayed by us", `${t.relayed_by_us || 0}`, t.total ? `${(100 * (t.relayed_by_us || 0) / t.total).toFixed(0)} % of heard` : ""),
      tile("Sent by us", `${t.tx || 0}`, `airtime ${n1(t.airtime_s, 0)} s`),
      tile("Multi-hop", `${t.relayed || 0}`, `${t.direct || 0} heard directly`), tile("Text messages", `${t.text || 0}`, ""),
      tile("Active nodes", `${t.nodes || 0}`, `${nodes.length} known`), tile("Mean SNR", t.snr == null ? "–" : `${n1(t.snr, 1)} dB`, `RSSI ${n1(t.rssi, 0)} dBm`));
    hBars($("#chart-mesh-port"), s.by_port.map((r) => ({ label: (r.portnum || "unknown").replace("_APP", ""), value: r.n })), { color: 2 });
    hBars($("#chart-mesh-hops"), s.by_hops.map((r) => ({ label: `${r.hops_used} hop${r.hops_used === 1 ? "" : "s"}`, value: r.n })), { color: 3 });
    const sel = $("#mesh-port"); const cur = sel.value; sel.innerHTML = '<option value="">all</option>';
    s.by_port.forEach((r) => r.portnum && sel.append(el("option", { value: r.portnum }, r.portnum))); sel.value = cur;
    const box = $("#mesh-msgs"); box.innerHTML = "";
    if (!msgs.length) box.append(el("div", { class: "empty" }, "no text messages yet"));
    msgs.forEach((m) => box.append(msgEl(m)));
    table($("#mesh-nodes"), NODE_COLS, nodes, { empty: "no nodes yet (is meshtasticd running?)" });
    await loadMeshLog(true);
  }
  const msgEl = (m) => el("div", { class: "msg" }, el("div", {}, m.text), el("div", { class: "meta" }, `${nodeName(nodeMap[m.from_node]) || hex(m.from_node)} → ${m.to_node === 0xffffffff ? "all" : nodeName(nodeMap[m.to_node]) || hex(m.to_node)} · ch ${m.channel} · ${fmtTs(m.ts)} · ${m.hops_used ?? "?"} hops · SNR ${n1(m.snr, 1)}`));
  async function loadMeshLog(reset) {
    const q = new URLSearchParams({ limit: 50 });
    if ($("#mesh-port").value) q.set("portnum", $("#mesh-port").value);
    if (!reset && MESH_STATE.oldest) q.set("before_id", MESH_STATE.oldest);
    const rows = await api(`/api/mesh/packets?${q}`);
    if (reset) table($("#mesh-log"), MESH_COLS, rows); else rows.forEach((r) => $("#mesh-log").append(rowEl(MESH_COLS, r)));
    if (rows.length) MESH_STATE.oldest = rows[rows.length - 1].id;
  }
  $("#mesh-more").onclick = () => loadMeshLog(false);
  $("#mesh-port").onchange = () => loadMeshLog(true);

  async function refreshGps() {
    const h = hours();
    const hist = await api(`/api/gps/history?hours=${h}`);
    const g = status.gps, tiles = $("#gps-tiles"); tiles.innerHTML = "";
    tiles.append(tile("Fix", g.fix, g.device || "", g.fix === "3D" ? "good" : g.fix === "2D" ? "warning" : "critical"),
      tile("Satellites", g.sats_used == null ? "–" : `${g.sats_used}`, `${g.sats_seen ?? "–"} in view · HDOP ${n1(g.hdop, 1)}`),
      tile("Accuracy", g.eph == null ? "–" : `${n1(g.eph, 0)} m`, `vertical ${n1(g.epv, 0)} m`),
      tile("Reference", g.time_sync?.ref_id || "–", g.time_sync?.source || "", g.time_sync?.ref_id === "GPS" ? "good" : "warning"),
      tile("Clock offset", g.time_sync?.offset_s == null ? "–" : `${(g.time_sync.offset_s * 1000).toFixed(2)} ms`, `rms ${g.time_sync?.rms_offset_s == null ? "–" : (g.time_sync.rms_offset_s * 1000).toFixed(2)} ms`),
      tile("Stratum", g.time_sync?.stratum || "–", g.time_sync?.leap || ""));
    lines($("#chart-gps-sats"), hist.map((r) => ({ t: r.ts, used: r.sats_used, seen: r.sats_seen })), ["used", "seen"], ["used", "in view"], { fmtVal: (v) => n1(v, 0), min: 0 });
    lines($("#chart-gps-offset"), hist.map((r) => ({ t: r.ts, off: r.offset_s == null ? null : r.offset_s * 1000 })), ["off"], ["offset"], { fmtVal: (v) => n1(v, 2) });
    table($("#gps-sources"), [
      { h: "Source", f: (r) => r.name }, { h: "Mode", f: (r) => ({ "#": "refclock", "^": "server", "=": "peer" }[r.mode] || r.mode) },
      { h: "State", f: (r) => ({ "*": "selected", "+": "combined", "-": "not combined", "?": "unreachable", "x": "false ticker", "~": "too variable" }[r.state] || r.state) },
      { h: "Stratum", f: (r) => r.stratum, num: true }, { h: "Reach", f: (r) => r.reach, num: true }, { h: "Last rx (s)", f: (r) => r.last_rx, num: true }, { h: "Offset (s)", f: (r) => r.offset, num: true },
    ], g.time_sync?.sources || [], { empty: "chrony not reporting" });
    $("#gps-pos").innerHTML = g.lat == null ? "no position yet" : `${n1(g.lat, 6)}, ${n1(g.lon, 6)} · ${n1(g.alt, 0)} m · <a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat=${g.lat}&mlon=${g.lon}#map=15/${g.lat}/${g.lon}">OpenStreetMap</a> · GPS time ${esc(g.time || "")}`;
  }

  async function refreshEvents() { table($("#events"), EVENT_COLS, await api("/api/events?limit=200")); }

  // bucket helpers: fill gaps so bars are evenly spaced in time
  function fill(rows, bucket, h, keys) {
    const now = Math.floor(Date.now() / 1000 / bucket) * bucket, start = now - h * 3600 + bucket;
    const m = new Map(rows.map((r) => [r.t, r])), out = [];
    for (let t = start; t <= now; t += bucket) { const r = m.get(t) || { t }; out.push(Object.assign({ t }, Object.fromEntries(keys.map((k) => [k, r[k] || 0])))); }
    return out;
  }
  function pivot(rows, bucket, h, dim, vals) {
    const by = {}; rows.forEach((r) => { (by[r.t] ||= { t: r.t })[r[dim]] = r.n; });
    return fill(Object.values(by), bucket, h, vals);
  }

  // ---------------------------------------------------------------- live feed
  function connectWs() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => $("#live").classList.add("on");
    ws.onclose = () => { $("#live").classList.remove("on"); setTimeout(connectWs, 5000); };
    ws.onmessage = (e) => {
      const { kind, row } = JSON.parse(e.data);
      if (kind === "lorawan_packets") { if (tab === "overview") prepend($("#ov-lw"), LW_COLS.slice(0, 11), row); if (tab === "lorawan") prepend($("#lw-log"), LW_COLS, row); }
      if (kind === "mesh_packets") { if (tab === "overview") prepend($("#ov-mesh"), MESH_COLS.slice(0, 9).concat(MESH_COLS.slice(-1)), row); if (tab === "mesh") { prepend($("#mesh-log"), MESH_COLS, row); if (row.text) { const b = $("#mesh-msgs"); b.querySelector(".empty")?.remove(); b.prepend(msgEl(row)); } } }
      if (kind === "events" && tab === "events") prepend($("#events"), EVENT_COLS, row);
    };
    setInterval(() => ws.readyState === 1 && ws.send("ping"), 30000);
  }

  // ---------------------------------------------------------------- orchestration
  const refreshTab = (name) => ({ overview: refreshOverview, lorawan: refreshLorawan, mesh: refreshMesh, gps: refreshGps, events: refreshEvents }[name]().catch(console.error));
  async function refreshAll() { try { await refreshStatus(); await refreshTab(tab); } catch (e) { console.error(e); } }
  showTab(tab);
  refreshStatus().catch(console.error);
  connectWs();
  setInterval(() => refreshStatus().catch(console.error), 15000);
  setInterval(() => refreshTab(tab), 60000);
  addEventListener("resize", () => refreshTab(tab));
})();
