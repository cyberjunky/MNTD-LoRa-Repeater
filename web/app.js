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
  if (location.hash.length > 1) tab = location.hash.slice(1);   // bookmarkable: /#diag
  if (!document.getElementById(`tab-${tab}`)) tab = "overview";
  const showTab = (name) => {
    tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
    try { localStorage.setItem("tab", name); } catch {}
    history.replaceState(null, "", `#${name}`);
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
    el("div", { class: /^\S{12,}$/.test(String(value)) ? "value long" : "value" }, value), el("div", { class: "sub" }, sub));
  // tables keep their rows and render one page at a time; the pager sits right below the table
  const PAGE_SIZE = 25;
  function table(node, cols, rows, { empty: emptyMsg = "nothing yet", pageSize = PAGE_SIZE } = {}) {
    node._t = { cols, rows: [...rows], page: 0, size: pageSize, empty: emptyMsg };
    renderTable(node);
  }
  function renderTable(node, flashFirst = false) {
    const t = node._t, pages = Math.max(1, Math.ceil(t.rows.length / t.size));
    t.page = Math.min(t.page, pages - 1);
    node.innerHTML = "";
    if (!t.rows.length) node.append(el("tr", {}, el("td", { class: "empty", colspan: t.cols.length }, t.empty)));
    else {
      node.append(el("tr", {}, ...t.cols.map((c) => el("th", { class: c.num ? "num" : "" }, c.h))));
      t.rows.slice(t.page * t.size, (t.page + 1) * t.size).forEach((r, i) => {
        const tr = rowEl(t.cols, r); if (flashFirst && i === 0) tr.classList.add("new"); node.append(tr);
      });
    }
    let pager = node.nextElementSibling?.classList.contains("pager") ? node.nextElementSibling : null;
    if (!pager) { pager = el("div", { class: "pager" }); node.after(pager); }
    pager.hidden = pages < 2;
    pager.innerHTML = "";
    const go = (p) => { t.page = p; renderTable(node); };
    const btn = (label, p, off) => { const b = el("button", {}, label); b.disabled = off; b.onclick = () => go(p); return b; };
    pager.append(btn("«", 0, t.page === 0), btn("‹ Prev", t.page - 1, t.page === 0),
      el("span", {}, `page ${t.page + 1} of ${pages} · ${t.rows.length} rows`),
      btn("Next ›", t.page + 1, t.page >= pages - 1), btn("»", pages - 1, t.page >= pages - 1));
  }
  const appendRows = (node, rows) => { node._t.rows.push(...rows); renderTable(node); };
  const rowEl = (cols, r) => el("tr", {}, ...cols.map((c) => { const v = c.f(r); const td = el("td", { class: c.num ? "num" : "" }); typeof v === "string" && v.startsWith("<") ? (td.innerHTML = v) : td.append(v ?? "–"); return td; }));
  const prepend = (node, cols, r) => {      // live feed: newest first; only redraw when page 1 is shown
    if (!node._t) table(node, cols, []);
    node._t.rows.unshift(r);
    if (node._t.rows.length > 2000) node._t.rows.length = 2000;
    if (node._t.page === 0) renderTable(node, true);
    else renderTable(node);
  };

  // ---------------------------------------------------------------- formatters
  // link quality from the SNR margin above the LoRa demodulation floor for that spreading factor
  // (Semtech: SF7 -7.5 dB ... SF12 -20 dB). One gateway cannot give a distance; this says how
  // much headroom the link has.
  const SNR_FLOOR = { 5: -2.5, 6: -5, 7: -7.5, 8: -10, 9: -12.5, 10: -15, 11: -17.5, 12: -20 };
  function linkBadge(snr, datr) {
    const sfs = String(datr || "").match(/SF(\d+)/gi);
    if (snr == null || !sfs) return "";
    const sf = Math.max(...sfs.map((x) => +x.slice(2)));        // slowest rate seen: conservative
    const margin = snr - SNR_FLOOR[sf];
    const [label, cls] = margin >= 10 ? ["strong", "up"] : margin >= 5 ? ["good", "up"] : margin >= 2 ? ["fair", ""] : ["edge", "bad"];
    return `<span class="badge ${cls}" title="${n1(margin, 1)} dB above the SF${sf} floor (${SNR_FLOOR[sf]} dB)">${label}</span>`;
  }
  const macText = (mac) => (mac || []).map((m) => {
    const { cmd, ...rest } = m;
    if (cmd === "DevStatusAns") return `${cmd} battery ${m.battery === 0 ? "external" : m.battery == null ? "?" : Math.round(m.battery / 2.54) + "%"} margin ${m.margin} dB`;
    if (cmd === "DeviceTimeAns") return `${cmd} ${fmtTs(m.gps_time)}`;
    const kv = Object.entries(rest).map(([k, v]) => `${k}=${v}`).join(" ");
    return kv ? `${cmd} ${kv}` : cmd;
  }).join("; ");
  const nn = (num) => esc(nodeName(nodeMap[num]) || hex(num));
  function meshContent(r) {
    if (r.text) return esc(r.text);
    const p = r.payload || {};
    if (p.user) return `👤 ${esc(p.user.longName || "")} (${esc(p.user.shortName || "")}) · ${esc(p.user.hwModel || "")} · ${esc(p.user.role || "CLIENT")}${p.user.hasPublicKey ? " · 🔑" : ""}`;
    if (p.position) return p.position.latitude == null ? "📍 no fix" : `📍 ${n1(p.position.latitude, 5)}, ${n1(p.position.longitude, 5)}${p.position.altitude ? ` · ${p.position.altitude} m` : ""}${p.position.satsInView ? ` · ${p.position.satsInView} sats` : ""}`;
    if (p.telemetry) {
      const t = p.telemetry, d = t.deviceMetrics, e = t.environmentMetrics, s = t.localStats, w = t.powerMetrics, a = t.airQualityMetrics;
      if (d) return `🔋 ${d.batteryLevel ?? "?"}% · ${n1(d.voltage, 2)} V · ch util ${n1(d.channelUtilization, 1)}% · air tx ${n1(d.airUtilTx, 1)}%`;
      if (e) return "🌡 " + Object.entries(e).map(([k, v]) => `${k} ${typeof v === "number" ? n1(v, 1) : v}`).join(" · ");
      if (s) return `📊 sees ${s.numOnlineNodes ?? "?"}/${s.numTotalNodes ?? "?"} nodes · tx ${s.numPacketsTx ?? 0} · rx ${s.numPacketsRx ?? 0} · bad ${s.numPacketsRxBad ?? 0} · relayed ${s.numTxRelay ?? 0}`;
      if (w) return "⚡ " + Object.entries(w).map(([k, v]) => `${k} ${n1(v, 2)}`).join(" · ");
      if (a) return "🌫 " + Object.entries(a).map(([k, v]) => `${k} ${v}`).join(" · ");
      return `<span class="mono muted">${esc(JSON.stringify(t).slice(0, 120))}</span>`;
    }
    if (p.neighborInfo) return `🕸 ${(p.neighborInfo.neighbors || []).map((x) => `${nn(x.nodeId)} ${n1(x.snr, 1)} dB`).join(", ") || "no neighbours"}`;
    if (p.traceroute) return `🧭 ${[r.to_node, ...(p.traceroute.route || []), r.from_node].map(nn).join(" → ")}`;
    if (p.mapReport) return `🗺 ${esc(p.mapReport.longName || "")} · ${esc(p.mapReport.hwModel || "")} · fw ${esc(p.mapReport.firmwareVersion || "?")} · ${esc(p.mapReport.modemPreset || "")} · sees ${p.mapReport.numOnlineLocalNodes ?? "?"}`;
    if (p.waypoint) return `📌 ${esc(p.waypoint.name || "")} ${esc(p.waypoint.description || "")} · ${n1(p.waypoint.latitude, 5)}, ${n1(p.waypoint.longitude, 5)}`;
    if (p.paxcount) return `👥 wifi ${p.paxcount.wifi ?? 0} · ble ${p.paxcount.ble ?? 0}`;
    if (p.routing) return `↩ ${esc(p.routing.errorReason || "ACK")}`;
    if (r.encrypted) return r.to_node === 0xffffffff ? `🔒 encrypted · channel hash ${r.channel_hash ?? "?"}` : "🔒 direct message (PKI)";
    return Object.keys(p).length ? `<span class="mono muted">${esc(JSON.stringify(p).slice(0, 120))}</span>` : "";
  }

  // ---------------------------------------------------------------- columns
  const LW_COLS = [
    { h: "Time", f: (r) => fmtTime(r.ts) },
    { h: "Dir", f: (r) => `<span class="badge ${r.direction}">${r.direction}</span>` },
    { h: "Type", f: (r) => r.mtype || "" },
    { h: "DevAddr / DevEUI", f: (r) => `<span class="mono" title="${esc(r.dev_vendor || "")}">${esc(r.devaddr || r.dev_eui || "")}</span>` },
    { h: "Network", f: (r) => esc(r.network || (r.netid ? `NetID ${r.netid}` : "")) },
    { h: "Vendor", f: (r) => esc(r.dev_vendor || "") },
    { h: "Flags", f: (r) => [r.adr ? "ADR" : "", r.adr_ack_req ? "ADRreq" : "", r.ack ? "ACK" : "", r.fpending ? (r.direction === "down" ? "FPend" : "ClassB") : ""].filter(Boolean).join(" ") },
    { h: "FCnt", f: (r) => r.fcnt, num: true }, { h: "Port", f: (r) => r.fport, num: true },
    { h: "MHz", f: (r) => n1(r.freq, 3), num: true }, { h: "DR", f: (r) => r.datr || "" }, { h: "CR", f: (r) => r.codr || "" },
    { h: "RSSI", f: (r) => r.rssi == null ? "" : n1(r.rssi, 0), num: true }, { h: "SNR", f: (r) => r.lsnr == null ? "" : n1(r.lsnr, 1), num: true }, { h: "Link", f: (r) => r.direction === "up" ? linkBadge(r.lsnr, r.datr) : "" },
    { h: "Bytes", f: (r) => r.size, num: true }, { h: "Airtime", f: (r) => r.airtime_ms == null ? "" : `${n1(r.airtime_ms, 0)} ms`, num: true },
    { h: "MAC", f: (r) => macText(r.mac) || (r.fopts ? `<span class="mono muted" title="encrypted (LoRaWAN 1.1)">${esc(r.fopts)}</span>` : "") },
    { h: "CRC", f: (r) => r.direction === "down" ? "" : r.stat === 1 ? "ok" : r.stat === -1 ? '<span class="badge bad">bad</span>' : "none" },
    { h: "Forwarded", f: (r) => (Array.isArray(r.forwarded) ? r.forwarded : []).join(", ") || (r.upstream || "") },
  ];
  const OV_LW_COLS = LW_COLS.filter((c) => ["Time", "Type", "DevAddr / DevEUI", "Network", "FCnt", "DR", "RSSI", "SNR", "Link"].includes(c.h));  // compact overview
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
    { h: "Content", f: (r) => meshContent(r) },
  ];
  const NODE_COLS = [
    { h: "Node", f: (r) => `<b>${esc(r.long_name || "")}</b> <span class="muted">${esc(r.short_name || "")}</span>` },
    { h: "ID", f: (r) => `<span class="mono">${esc(r.node_id || hex(r.num))}</span>` },
    { h: "Hardware", f: (r) => r.hw_model || "" }, { h: "Role", f: (r) => r.role || "" },
    { h: "Last heard", f: (r) => ago(r.last_heard) }, { h: "Hops", f: (r) => r.hops_away, num: true },
    { h: "SNR", f: (r) => r.snr == null ? "" : n1(r.snr, 1), num: true },
    { h: "Link", f: (r) => r.hops_away === 0 ? linkBadge(r.snr, status?.meshsx?.datr) : r.hops_away > 0 ? '<span class="muted">via relay</span>' : "" },
    { h: "Firmware", f: (r) => esc([r.firmware, r.modem_preset].filter(Boolean).join(" · ")) },
    { h: "Packets", f: (r) => r.packets, num: true },
    { h: "Battery", f: (r) => r.battery == null ? "" : r.battery > 100 ? "mains" : `${r.battery}%`, num: true },
    { h: "Env", f: (r) => [r.temperature != null ? `${n1(r.temperature, 1)} °C` : "", r.humidity != null ? `${n1(r.humidity, 0)} %` : "", r.pressure != null ? `${n1(r.pressure, 0)} hPa` : ""].filter(Boolean).join(" · ") },
    { h: "Uptime", f: (r) => r.uptime_s == null ? "" : r.uptime_s < 86400 ? `${(r.uptime_s / 3600).toFixed(1)} h` : `${(r.uptime_s / 86400).toFixed(1)} d`, num: true },
    { h: "Sees", f: (r) => r.online_nodes == null ? "" : `${r.online_nodes}${r.total_nodes ? "/" + r.total_nodes : ""}`, num: true },
    { h: "PKI", f: (r) => r.has_pki ? "🔑" : "" },
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
    const g = status.gwmp, clk = status.time, sx = status.meshsx, tg = status.telegram;
    const fwdOk = g.forwarder_seen_s_ago != null && g.forwarder_seen_s_ago < 120;
    const upOk = !g.upstreams.length || g.upstreams.some((u) => u.alive);
    const meshTile = sx
      ? tile("Meshtastic node", `${sx.node_id}`, `${sx.long_name} · ${sx.role} · ${sx.freq_mhz} MHz ${sx.datr}${sx.last_rx_s_ago != null ? ` · rx ${sx.last_rx_s_ago}s ago` : ""}`, fwdOk ? "good" : "critical")
      : tile("Meshtastic node", "disabled", "meshsx.enabled in config.yaml", "unknown");
    $("#health-dot").className = "dot " + (!fwdOk ? "critical" : !upOk ? "warning" : "good");
    const tiles = $("#tiles"); tiles.innerHTML = "";
    tiles.append(
      tile("Packet forwarder", fwdOk ? "running" : "no data", g.forwarder_seen_s_ago == null ? "never seen" : `last seen ${g.forwarder_seen_s_ago}s ago · EUI ${g.gateway_eui || "?"}`, fwdOk ? "good" : "critical"),
      tile("Upstream networks", `${g.upstreams.filter((u) => u.alive).length} / ${g.upstreams.length}`, g.upstreams.map((u) => u.name).join(", ") || "none configured", g.upstreams.length ? (upOk ? "good" : "critical") : "unknown"),
      meshTile,
      tile("Time sync", clk.synchronized ? "synced" : clk.server ? "not synced" : "–", [clk.server, clk.stratum != null ? `stratum ${clk.stratum}` : null, clk.poll_interval ? `poll ${clk.poll_interval}` : null].filter(Boolean).join(" · ") || "timesyncd not reporting", clk.synchronized ? "good" : clk.server ? "warning" : "unknown"),
      tile("Stored packets", `${(status.db.lorawan || 0) + (status.db.mesh || 0)}`, `${status.db.lorawan || 0} LoRaWAN · ${status.db.mesh || 0} mesh · ${status.db.nodes || 0} nodes`),
      tile("CPU temperature", status.cpu_temp_c == null ? "–" : `${n1(status.cpu_temp_c, 1)} °C`, `up ${ago(Date.now() / 1000 - status.uptime_s)}`.replace(" ago", ""), status.cpu_temp_c == null ? "unknown" : status.cpu_temp_c > 75 ? "critical" : status.cpu_temp_c > 65 ? "warning" : "good"),
    );
    if (sx) tiles.append(tile("Mesh airtime (1 h)", `${n1(sx.duty_used_pct_1h, 2)} %`, `of ${sx.duty_limit_pct} % duty cycle · relayed ${sx.counters.relayed} · sent ${sx.counters.tx}`, sx.duty_used_pct_1h > sx.duty_limit_pct * 0.8 ? "warning" : "good"));
    if (tg && tg.enabled) tiles.append(tile("Telegram", tg.connected ? `@${tg.bot || "bot"}` : "offline", `in ${tg.counters.in} · out ${tg.counters.out} · posted ${tg.counters.posted}`, tg.connected ? "good" : "critical"));
  }

  async function refreshOverview() {
    const h = hours();
    const [lw, mesh, lwp, mp] = await Promise.all([api(`/api/lorawan/summary?hours=${h}`), api(`/api/mesh/summary?hours=${h}`), api("/api/lorawan/packets?limit=240"), api("/api/mesh/packets?limit=240")]);
    $("#lw-range").textContent = `last ${h} h · ${lw.totals.total || 0} total`;
    $("#mesh-range").textContent = `last ${h} h · ${mesh.totals.total || 0} total`;
    stackedBars($("#chart-lw-time"), pivot(lw.per_bucket, lw.bucket_s, h, "direction", ["up", "down"]), ["up", "down"], ["uplinks", "downlinks"]);
    stackedBars($("#chart-mesh-time"), fill(mesh.per_bucket.map((b) => ({ t: b.t, heard: b.n - (b.tx || 0), relayed: b.relayed_by_us || 0, tx: b.tx || 0 })), mesh.bucket_s, h, ["heard", "relayed", "tx"]), ["heard", "relayed", "tx"], ["heard", "relayed by us", "sent by us"]);
    table($("#ov-lw"), OV_LW_COLS, lwp, { pageSize: 12 });
    table($("#ov-mesh"), MESH_COLS.slice(0, 9).concat(MESH_COLS.slice(-1)), mp, { pageSize: 12 });
  }

  const LW_STATE = { oldest: null };
  async function refreshLorawan() {
    const h = hours();
    const s = await api(`/api/lorawan/summary?hours=${h}`);
    const t = s.totals, tiles = $("#lw-tiles"); tiles.innerHTML = "";
    tiles.append(tile("Uplinks", `${t.up || 0}`, `last ${h} h`), tile("Downlinks", `${t.down || 0}`, `last ${h} h`),
      tile("CRC errors", `${t.crc_bad || 0}`, t.up ? `${(100 * (t.crc_bad || 0) / t.up).toFixed(1)} % of uplinks` : ""),
      tile("Mean RSSI", t.rssi == null ? "–" : `${n1(t.rssi, 0)} dBm`, `mean SNR ${n1(t.snr, 1)} dB`),
      tile("Devices", `${t.devices || 0}`, `${t.joiners || 0} joining (DevEUI)`),
      tile("Networks", `${s.by_network.length}`, s.by_network.slice(0, 3).map((r) => r.network).join(", ")),
      tile("Airtime", `${n1(t.airtime_s, 1)} s`, `heard in ${h} h`));
    table($("#lw-networks"), [{ h: "Network", f: (r) => esc(r.network) }, { h: "Devices", f: (r) => r.devices, num: true }, { h: "Packets", f: (r) => r.n, num: true }], s.by_network);
    table($("#lw-mac"), [{ h: "Time", f: (r) => fmtTime(r.ts) }, { h: "DevAddr", f: (r) => `<span class="mono">${r.devaddr}</span>` }, { h: "Dir", f: (r) => r.direction === "down" ? "↓ sent by us" : /Down/.test(r.mtype || "") ? "↓ heard" : "↑ uplink" }, { h: "Commands", f: (r) => esc(macText(r.mac)) }], s.mac, { empty: "no MAC commands in the clear yet" });
    table($("#lw-joins"), [
      { h: "DevEUI", f: (r) => `<span class="mono">${r.dev_eui}</span>` }, { h: "Device vendor", f: (r) => esc(r.dev_vendor || "") },
      { h: "JoinEUI", f: (r) => `<span class="mono">${r.join_eui || ""}</span>` }, { h: "Join server vendor", f: (r) => esc(r.join_vendor || "") },
      { h: "Kind", f: (r) => esc(r.kinds || "") }, { h: "Count", f: (r) => r.n, num: true }, { h: "RSSI", f: (r) => n1(r.rssi, 0), num: true }, { h: "Last seen", f: (r) => ago(r.last_ts) },
    ], s.joins, { empty: "no join requests yet" });
    hBars($("#chart-lw-freq"), s.by_freq.filter((r) => r.freq).map((r) => ({ label: n1(r.freq, 1) + " MHz", value: r.n, rssi: r.rssi })), { extra: (d) => `${n1(d.rssi, 0)} dBm` });
    hBars($("#chart-lw-datr"), s.by_datr.filter((r) => r.datr).map((r) => ({ label: r.datr, value: r.n })), { color: 1 });
    table($("#lw-devices"), [
      { h: "DevAddr", f: (r) => `<span class="mono">${r.devaddr}</span>` }, { h: "Network", f: (r) => esc(r.network || `NetID ${r.netid || "?"}`) },
      { h: "Packets", f: (r) => r.n, num: true }, { h: "Last FCnt", f: (r) => r.fcnt, num: true },
      { h: "DR", f: (r) => esc(r.datr || "") }, { h: "Ports", f: (r) => esc(r.fports || "") }, { h: "Bytes", f: (r) => r.payload_len == null ? "" : n1(r.payload_len, 0), num: true },
      { h: "ADR", f: (r) => r.adr ? "yes" : "" }, { h: "Battery", f: (r) => r.battery == null ? "" : r.battery === 0 ? "external" : `${Math.round(r.battery / 2.54)}%`, num: true },
      { h: "Margin", f: (r) => r.margin == null ? "" : `${r.margin} dB`, num: true }, { h: "Airtime", f: (r) => `${n1(r.airtime_ms / 1000, 1)} s`, num: true },
      { h: "RSSI", f: (r) => n1(r.rssi, 0), num: true }, { h: "SNR", f: (r) => n1(r.snr, 1), num: true }, { h: "Link", f: (r) => linkBadge(r.snr, r.datr) }, { h: "Last seen", f: (r) => ago(r.last_ts) },
    ], s.devices);
    await loadLwLog(true);
  }
  async function loadLwLog(reset) {
    const q = new URLSearchParams({ limit: 500 });
    if ($("#lw-dir").value) q.set("direction", $("#lw-dir").value);
    if ($("#lw-devaddr").value.trim()) q.set("devaddr", $("#lw-devaddr").value.trim());
    if (!reset && LW_STATE.oldest) q.set("before_id", LW_STATE.oldest);
    const rows = await api(`/api/lorawan/packets?${q}`);
    if (reset) table($("#lw-log"), LW_COLS, rows); else appendRows($("#lw-log"), rows);
    if (rows.length) LW_STATE.oldest = rows[rows.length - 1].id;
  }
  $("#lw-more").onclick = () => loadLwLog(false);
  $("#lw-dir").onchange = $("#lw-devaddr").onchange = () => loadLwLog(true);

  const MESH_STATE = { oldest: null };
  async function refreshMesh() {
    const h = hours();
    const [s, nodes, msgs, links] = await Promise.all([api(`/api/mesh/summary?hours=${h}`), api("/api/mesh/nodes"), api("/api/mesh/packets?limit=30&text_only=true"), api(`/api/mesh/links?hours=${Math.max(h, 168)}`)]);
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
    table($("#mesh-nodes"), NODE_COLS, nodes, { empty: "no nodes heard yet" });
    table($("#mesh-links"), [{ h: "From", f: (r) => nn(r.a) }, { h: "Heard by", f: (r) => nn(r.b) }, { h: "Source", f: (r) => r.source },
      { h: "SNR", f: (r) => r.snr == null ? "" : n1(r.snr, 1), num: true }, { h: "Seen", f: (r) => r.count, num: true }, { h: "Last", f: (r) => ago(r.ts) }], links, { empty: "no links yet" });
    table($("#mesh-channels"), [{ h: "Hash", f: (r) => r.channel_hash ?? "?", num: true }, { h: "Channel", f: (r) => r.channel != null ? (status?.meshsx?.channels || [])[r.channel] || r.channel : "unknown key" },
      { h: "Packets", f: (r) => r.n, num: true }, { h: "Encrypted", f: (r) => r.encrypted, num: true }, { h: "Nodes", f: (r) => r.nodes, num: true }], s.by_channel, { empty: "nothing heard yet" });
    await loadMeshLog(true);
  }
  const msgEl = (m) => el("div", { class: "msg" }, el("div", {}, m.text), el("div", { class: "meta" }, `${nodeName(nodeMap[m.from_node]) || hex(m.from_node)} → ${m.to_node === 0xffffffff ? "all" : nodeName(nodeMap[m.to_node]) || hex(m.to_node)} · ch ${m.channel} · ${fmtTs(m.ts)} · ${m.hops_used ?? "?"} hops · SNR ${n1(m.snr, 1)}`));
  async function loadMeshLog(reset) {
    const q = new URLSearchParams({ limit: 500 });
    if ($("#mesh-port").value) q.set("portnum", $("#mesh-port").value);
    if (!reset && MESH_STATE.oldest) q.set("before_id", MESH_STATE.oldest);
    const rows = await api(`/api/mesh/packets?${q}`);
    if (reset) table($("#mesh-log"), MESH_COLS, rows); else appendRows($("#mesh-log"), rows);
    if (rows.length) MESH_STATE.oldest = rows[rows.length - 1].id;
  }
  $("#mesh-more").onclick = () => loadMeshLog(false);
  $("#mesh-port").onchange = () => loadMeshLog(true);

  async function refreshUpstreams() {
    const [ups, downs] = await Promise.all([api("/api/lorawan/upstreams"), api("/api/lorawan/packets?direction=down&limit=100")]);
    const live = Object.fromEntries((status?.gwmp?.upstreams || []).map((u) => [u.name, u]));
    const eui = status?.gwmp?.gateway_eui || "?";
    const tiles = $("#up-tiles"); tiles.innerHTML = "";
    for (const u of ups) {
      const l = live[u.name] || {}, rate = u.push_sent ? 100 * u.push_acked / u.push_sent : null;
      tiles.append(tile(u.name, l.alive ? "connected" : u.push_sent ? "no answer" : "idle",
        `${u.host}:${u.port}${l.rx_only ? " · rx only" : ""} · ${l.last_pull_ack_s_ago != null ? `last ack ${l.last_pull_ack_s_ago}s ago` : "never acked"}${rate != null ? ` · ack ${rate.toFixed(0)} %` : ""}`,
        l.alive ? "good" : u.push_sent ? "critical" : "unknown"));
    }
    tiles.append(tile("Gateway EUI", eui, "register this EUI at each network"));
    const help = $("#up-help"), silent = ups.filter((u) => u.push_sent > 0 && !u.push_acked);
    help.hidden = !(silent.length || !ups.length);
    help.innerHTML = !ups.length
      ? `<h3>No upstream networks</h3><p>Add one under <span class="mono">gwmp.upstreams</span> in <span class="mono">/etc/lorarepeater/config.yaml</span> and restart lorarepeater. Everything is still logged locally.</p>`
      : `<h3>${silent.map((u) => esc(u.name)).join(", ")}: packets sent, nothing acknowledged</h3>
         <p>Network servers ignore gateways that are not registered to an account. Register gateway EUI <b class="mono">${esc(eui)}</b>${silent.some((u) => /thethings/.test(u.host)) ? ` in the <a href="https://eu1.cloud.thethings.network/console/gateways/add" target="_blank" rel="noopener">TTN console (Europe 1)</a> with frequency plan <i>Europe 863-870 MHz (SF9 for RX2 - recommended)</i>` : ""}. Acknowledgements start within a minute.</p>`;
    table($("#up-table"), [
      { h: "Name", f: (r) => r.name }, { h: "Host", f: (r) => `${r.host}:${r.port}` },
      { h: "Uplinks pushed", f: (r) => r.push_sent, num: true }, { h: "Acked", f: (r) => r.push_acked, num: true },
      { h: "Ack rate", f: (r) => r.push_sent ? `${(100 * r.push_acked / r.push_sent).toFixed(1)} %` : "", num: true },
      { h: "Downlinks", f: (r) => r.pull_resp, num: true }, { h: "Last PUSH_ACK", f: (r) => ago(r.last_push_ack) }, { h: "Last PULL_ACK", f: (r) => ago(r.last_pull_ack) },
    ], ups, { empty: "no upstream networks configured" });
    table($("#up-downlinks"), [
      { h: "Time", f: (r) => fmtTs(r.ts) }, { h: "From", f: (r) => esc(r.upstream || "") }, { h: "Type", f: (r) => r.mtype || "" },
      { h: "DevAddr", f: (r) => `<span class="mono">${esc(r.devaddr || "")}</span>` }, { h: "MHz", f: (r) => n1(r.freq, 3), num: true }, { h: "DR", f: (r) => r.datr || "" },
      { h: "Airtime", f: (r) => r.airtime_ms == null ? "" : `${n1(r.airtime_ms, 0)} ms`, num: true }, { h: "MAC", f: (r) => esc(macText(r.mac)) },
      { h: "Result", f: (r) => (Array.isArray(r.forwarded) && r.forwarded.length ? r.forwarded.join(", ") : "sent to radio") },
    ], downs, { empty: "no downlinks yet" });
  }

  async function refreshAbout() {
    if (status) $("#about-version").textContent = `Version ${status.version} · running ${ago(Date.now() / 1000 - status.uptime_s).replace(" ago", "")} · LoRaWAN gateway and Meshtastic repeater for the MNTD Goldspot`;
  }

  // ---------------------------------------------------------------- helium
  const solscan = (kind, id) => `<a class="mono" href="https://solscan.io/${kind}/${id}" target="_blank" rel="noopener">${esc(id.slice(0, 6))}…${esc(id.slice(-4))}</a>`;
  const fmtAmt = (v, d = 2) => v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
  const kvRows = (node, rows) => table(node, [{ h: "", f: (r) => `<span class="muted">${esc(r[0])}</span>` }, { h: "", f: (r) => r[1] ?? "–" }], rows.filter((r) => r[1] !== undefined));
  async function refreshHelium() {
    const [s, ups] = await Promise.all([api("/api/helium"), api("/api/lorawan/upstreams")]);
    const tiles = $("#he-tiles"), note = $("#he-note"); tiles.innerHTML = "";
    if (!s.enabled) { note.hidden = false; note.innerHTML = "<h3>Helium view disabled</h3><p>Set <span class=\"mono\">helium.enabled: true</span> in config.yaml.</p>"; return; }
    if (!s.updated) { note.hidden = false; note.innerHTML = "<h3>Collecting…</h3><p>The first Helium snapshot arrives about 10 seconds after the backend starts, then every 15 minutes.</p>"; return; }
    const gw = s.gateway || {}, h = s.hotspot || {}, r = s.rewards || {}, t = r.tokens || {}, wal = s.wallet || {}, eur = s.price?.HNT?.eur;
    const up = ups.find((u) => u.name === "helium"), live = (status?.gwmp?.upstreams || []).find((u) => u.name === "helium");
    const lastClaim = Math.max(0, ...Object.values(t).flatMap((x) => (x.claims || []).map((c) => c.time || 0)));
    const errs = Object.entries(s.errors || {});
    note.hidden = !errs.length;
    note.innerHTML = errs.length ? `<h3>Some sources did not answer</h3><p>${errs.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")}</p>` : "";
    tiles.append(
      tile("Hotspot", h.name || gw.name || "?", gw.key || "", gw.key ? "good" : "critical"),
      tile("Packet router", gw.router?.connected ? "connected" : "offline", `${gw.region || "?"} · gateway-rs ${gw.fw || "?"}`, gw.router?.connected ? "good" : "critical"),
      tile("On-chain status", h.active ? "active" : "inactive", `${(h.networks || []).join(", ").toUpperCase() || "–"} · onboarded ${h.onboarded ? new Date(h.onboarded).toLocaleDateString() : "?"}`, h.active ? "good" : "warning"),
      tile("Asserted location", h.city || "–", `${h.gain_dbi != null ? h.gain_dbi + " dBi" : ""}${h.elevation_m != null ? ` · ${h.elevation_m} m` : ""}`),
      tile("HNT earned", fmtAmt(t.HNT?.lifetime, 4), eur && t.HNT ? `≈ € ${fmtAmt(t.HNT.lifetime * eur)} at € ${fmtAmt(eur, 3)}/HNT` : "lifetime"),
      tile("IOT earned", fmtAmt(t.IOT?.lifetime, 0), "lifetime, before HIP-138"),
      tile("Unclaimed", `${fmtAmt(t.HNT?.unclaimed, 4)} HNT`, `${fmtAmt(t.IOT?.unclaimed, 0)} IOT`, (t.HNT?.unclaimed || 0) > 0 ? "warning" : "good"),
      tile("Wallet", wal.balances ? `${fmtAmt(wal.balances.HNT, 3)} HNT` : "–", wal.balances && eur ? `≈ € ${fmtAmt(wal.balances.HNT * eur)} · ${fmtAmt(wal.balances.SOL, 3)} SOL` : ""),
      tile("Last claim", lastClaim ? ago(lastClaim) : "–", lastClaim ? new Date(lastClaim * 1000).toLocaleDateString() : ""),
    );
    const cum = (claims) => { let c = 0; return [...(claims || [])].sort((a, b) => a.time - b.time).map((x) => ({ t: x.time, v: (c += x.amount) })); };
    lines($("#chart-he-hnt"), cum(t.HNT?.claims), ["v"], ["HNT"], { fmtVal: (v) => n1(v, 2), min: 0 });
    lines($("#chart-he-iot"), cum(t.IOT?.claims), ["v"], ["IOT"], { fmtVal: (v) => fmtAmt(v, 0), min: 0 });
    kvRows($("#he-hotspot"), [
      ["Name", esc(h.name)], ["Hotspot key", h.key ? `<span class="mono">${esc(h.key)}</span>` : undefined],
      ["NFT asset", h.asset_id ? solscan("token", h.asset_id) : undefined],
      ["Location", h.lat != null ? `<a href="https://www.openstreetmap.org/?mlat=${h.lat}&mlon=${h.lon}#map=15/${h.lat}/${h.lon}" target="_blank" rel="noopener">${esc([h.city, h.state, h.country].filter(Boolean).join(", "))}</a>` : "not asserted"],
      ["H3 cell", h.h3 ? `<span class="mono">${esc(h.h3)}</span>` : undefined],
      ["Antenna gain", h.gain_dbi != null ? `${h.gain_dbi} dBi` : undefined], ["Elevation", h.elevation_m != null ? `${h.elevation_m} m` : undefined],
      ["Onboarded", h.onboarded ? fmtTs(Date.parse(h.onboarded) / 1000) : undefined],
      ["Onboarding fee", h.dc_onboarding_fee_paid != null ? `${fmtAmt(h.dc_onboarding_fee_paid, 0)} DC (≈ $${fmtAmt(h.dc_onboarding_fee_paid / 1e5, 0)})` : undefined],
      ["Rewardable networks", esc((h.networks || []).join(", "))], ["Active", h.active == null ? undefined : h.active ? "yes" : "no"],
    ]);
    table($("#he-rewards"), [
      { h: "Token", f: (x) => x.sym }, { h: "Lifetime", f: (x) => fmtAmt(x.lifetime, 4), num: true }, { h: "Claimed", f: (x) => fmtAmt(x.claimed, 4), num: true },
      { h: "Unclaimed", f: (x) => fmtAmt(x.unclaimed, 4), num: true }, { h: "Value", f: (x) => x.sym === "HNT" && eur ? `€ ${fmtAmt(x.lifetime * eur)}` : "", num: true },
      { h: "Recipient", f: (x) => x.recipient ? solscan("account", x.recipient) : "" },
    ], Object.entries(t).map(([sym, x]) => ({ sym, ...x })), { empty: "no reward data" });
    kvRows($("#he-wallet"), [
      ["Address", wal.address ? solscan("account", wal.address) : "found from the claim transactions"],
      ...Object.entries(wal.balances || {}).map(([k, v]) => [k, `${fmtAmt(v, k === "HNT" ? 4 : 3)}${k === "HNT" && eur ? ` <span class="muted">≈ € ${fmtAmt(v * eur)}</span>` : ""}`]),
    ]);
    const claims = Object.entries(t).flatMap(([sym, x]) => (x.claims || []).map((c) => ({ sym, ...c }))).sort((a, b) => b.time - a.time);
    table($("#he-claims"), [
      { h: "Date", f: (c) => fmtTs(c.time) }, { h: "Token", f: (c) => c.sym }, { h: "Amount", f: (c) => fmtAmt(c.amount, 4), num: true },
      { h: "Value now", f: (c) => c.sym === "HNT" && eur ? `€ ${fmtAmt(c.amount * eur)}` : "", num: true }, { h: "Transaction", f: (c) => solscan("tx", c.signature) },
    ], claims, { empty: "no claims found" });
    kvRows($("#he-gateway"), [
      ["Service", gw.error ? `<span class="badge bad">not reachable</span> ${esc(gw.error)}` : `<span class="badge up">running</span> gateway-rs ${esc(gw.fw || "")}`],
      ["Region", esc(gw.region)], ["Packet router", gw.router ? `${gw.router.connected ? "connected" : "disconnected"} · ${esc(gw.router.uri || "")}` : undefined],
      ["Mux upstream", up ? `${live?.alive ? "acknowledging" : "silent"} · ${up.push_sent} uplinks pushed, ${up.push_acked} acked, ${up.pull_resp} downlinks` : "not configured (add a helium upstream on 127.0.0.1:1680)"],
      ["Logs", "Diagnostics → Log → source <span class=\"mono\">helium-gateway</span>"],
    ]);
    $("#he-sources").textContent = `Updated ${ago(s.updated)}. Sources: gateway-rs local API, Helium entity service, Helium reward oracles, Solana mainnet RPC, CoinGecko.`;
  }

  async function refreshEvents() { table($("#events"), EVENT_COLS, await api("/api/events?limit=200")); }

  // ---------------------------------------------------------------- diagnostics
  const post = (p) => fetch(p, { method: "POST" }).then(async (r) => { const b = await r.json().catch(() => ({})); if (!r.ok) throw new Error(b.detail || `${p}: ${r.status}`); return b; });
  const fmtBytes = (b) => b == null ? "–" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${(b / 1e6).toFixed(0)} MB`;
  const fmtDur = (s) => s == null ? "–" : s < 3600 ? `${(s / 60) | 0} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`;
  const DIAG = { follow: null, rawPaused: false, rawLines: 0, srcReady: false };
  const SERVICE_COLS = [
    { h: "Unit", f: (r) => r.unit }, { h: "Description", f: (r) => r.description || "" },
    { h: "State", f: (r) => `<span class="badge ${r.active === "active" ? "up" : r.active === "failed" ? "bad" : ""}">${esc(r.active)}${r.sub && r.sub !== r.active ? " / " + esc(r.sub) : ""}</span>` },
    { h: "Enabled", f: (r) => r.enabled || "–" }, { h: "Restarts", f: (r) => r.restarts ?? "–", num: true },
    { h: "Since", f: (r) => r.since ? esc(r.since.replace(/^\w+ /, "")) : "–" },
    { h: "", f: (r) => r.restartable ? `<button class="sm" data-action="restart:${r.unit}">Restart</button>` : "" },
  ];
  async function runAction(name, btn) {
    const msg = $("#diag-action-msg"), out = $("#diag-action-out");
    if (btn) btn.disabled = true;
    msg.textContent = `${name}…`;
    try {
      const r = await post(`/api/diag/action/${encodeURIComponent(name)}`);
      msg.textContent = r.ok ? `${name}: ok${r.eui ? ` · EUI ${r.eui} · reset GPIO ${r.reset_pin}` : ""}` : `${name}: failed`;
      if (r.output) { out.textContent = r.output; out.hidden = false; }
      setTimeout(() => refreshDiagServices().catch(console.error), 1500);
    } catch (e) { msg.textContent = `${name}: ${e.message}`; }
    if (btn) btn.disabled = false;
  }
  $("#diag-services").onclick = (e) => { const a = e.target.dataset.action; if (a) runAction(a, e.target); };
  $("#diag-probe").onclick = (e) => runAction("probe-concentrator", e.target);
  $("#diag-clear-db").onclick = async (e) => {
    if (!confirm("Delete all logged LoRaWAN and Meshtastic packets, gateway stats and events?")) return;
    const nodes = confirm("Also forget the known Meshtastic nodes?\n\nOK = clear nodes too, Cancel = keep the node list");
    await runAction(nodes ? "clear-database-all" : "clear-database", e.target);
    refreshAll();
  };
  $("#diag-clear-logs").onclick = async (e) => {
    if (!confirm("Empty the systemd journal? journald cannot delete per unit, so this clears the log of every service.")) return;
    await runAction("clear-logs", e.target);
    refreshDiagLog().catch(console.error);
  };
  async function refreshDiagServices() {
    const rows = await api("/api/diag/services"), sx = status?.meshsx;
    // the default Meshtastic node is not a unit: it runs inside lorarepeater on the SX1302 service channel
    if (sx) rows.splice(2, 0, {
      unit: "meshtastic (built-in)", description: `${sx.node_id} · ${sx.role} · ${sx.freq_mhz} MHz ${sx.datr} · rx ${sx.counters.rx} · relayed ${sx.counters.relayed} · tx ${sx.counters.tx}${sx.last_rx_s_ago != null ? ` · last rx ${sx.last_rx_s_ago}s ago` : " · nothing heard yet"}`,
      active: sx.enabled ? "active" : "inactive", sub: sx.tx_enabled ? "rx+tx" : "rx only", enabled: "in lorarepeater",
      restarts: null, since: sx.last_tx_s_ago != null ? `last tx ${sx.last_tx_s_ago}s ago` : null, restartable: false,
    });
    table($("#diag-services"), SERVICE_COLS, rows, { empty: "systemctl not available" });
  }
  async function refreshDiagSystem() {
    const s = await api("/api/diag/system"), t = $("#diag-tiles"); t.innerHTML = "";
    t.append(
      tile("Host", s.hostname, `${s.model || ""} · ${s.os || ""}`),
      tile("Kernel", s.kernel, `${s.machine} · Python ${s.python}`),
      tile("Uptime", fmtDur(s.uptime_s), `load ${(s.load || []).join(" ")}`),
      tile("Memory free", fmtBytes(s.mem_available), `of ${fmtBytes(s.mem_total)}`, s.mem_total && s.mem_available / s.mem_total < 0.1 ? "warning" : "good"),
      tile("Disk free", fmtBytes(s.disk?.free), `of ${fmtBytes(s.disk?.total)}`, s.disk && s.disk.free / s.disk.total < 0.1 ? "warning" : "good"),
      tile("Power supply", s.undervoltage == null ? "–" : s.undervoltage ? "under-voltage" : "ok", s.undervoltage == null ? "rpi_volt sensor not found" : s.undervoltage ? "supply voltage too low right now" : "no under-voltage", s.undervoltage == null ? "unknown" : s.undervoltage ? "critical" : "good"),
      (() => { const g = status?.gwmp || {}, ok = g.forwarder_seen_s_ago != null && g.forwarder_seen_s_ago < 120;
        return tile("Forwarder", ok ? "delivering" : "silent", `last packet ${g.forwarder_seen_s_ago ?? "–"}s ago · ${g.counters?.rxpk ?? 0} received · HAL ${s.hal_version || "?"}`, ok ? "good" : "critical"); })(),
    );
  }
  async function refreshDiagLog() {
    const q = new URLSearchParams({ source: $("#diag-log-src").value || "lora-pkt-fwd", lines: $("#diag-log-lines").value, quiet: $("#diag-log-quiet").checked });
    if ($("#diag-log-filter").value) q.set("filter", $("#diag-log-filter").value);
    $("#diag-log-dl").href = `/api/diag/log.txt?${q}`;
    const r = await api(`/api/diag/log?${q}`), pre = $("#diag-log");
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = r.text || "(empty)";
    if (atEnd || DIAG.follow) pre.scrollTop = pre.scrollHeight;
  }
  function setFollow() {
    clearInterval(DIAG.follow); DIAG.follow = null;
    if ($("#diag-log-follow").checked) DIAG.follow = setInterval(() => tab === "diag" && refreshDiagLog().catch(console.error), 4000);
  }
  $("#diag-log-refresh").onclick = () => refreshDiagLog().catch(console.error);
  $("#diag-log-src").onchange = $("#diag-log-lines").onchange = $("#diag-log-quiet").onchange = () => refreshDiagLog().catch(console.error);
  $("#diag-log-filter").onkeydown = (e) => e.key === "Enter" && refreshDiagLog().catch(console.error);
  $("#diag-log-follow").onchange = setFollow;
  const kv = (k, v) => el("div", { class: "kv" }, el("b", {}, k), typeof v === "string" ? el("pre", { class: "log short" }, v || "(nothing)") : v);
  async function refreshDiagHardware() {
    const h = await api("/api/diag/hardware"), box = $("#diag-hw"); box.innerHTML = "";
    box.append(
      kv(`I2C bus 1 (i2cdetect)`, h.i2cdetect),
      kv(`GPIO (reset pin GPIO ${h.reset_pin}, button 7)`, h.gpio),
      kv("I2C devices with kernel drivers", Object.entries(h.i2c_drivers || {}).map(([k, v]) => `${k}: ${v}`).join("\n")),
      kv("Devices", (h.devices || []).join("  ")),
      kv("LEDs (/sys/class/leds)", (h.leds || []).join("  ")),
      kv("HAT EEPROM, first 64 bytes", h.eeprom_head || "not readable (at24 driver not bound)"),
      kv("USB", h.usb || "lsusb not available"),
    );
  }
  $("#diag-hw-refresh").onclick = () => refreshDiagHardware().catch(console.error);
  async function refreshDiagConfig() { $("#diag-config").textContent = JSON.stringify(await api("/api/diag/config"), null, 2); }
  function rawFeed(kind, row) {
    if (DIAG.rawPaused || tab !== "diag") return;
    const pre = $("#diag-raw");
    pre.append(`${fmtTime(row.ts || Date.now() / 1000)} ${kind} ${JSON.stringify(row)}\n`);
    if (++DIAG.rawLines > 300) { pre.textContent = pre.textContent.split("\n").slice(-200).join("\n"); DIAG.rawLines = 200; }
    pre.scrollTop = pre.scrollHeight;
  }
  $("#diag-raw-pause").onchange = (e) => (DIAG.rawPaused = e.target.checked);
  $("#diag-raw-clear").onclick = () => { $("#diag-raw").textContent = ""; DIAG.rawLines = 0; };
  async function refreshDiag() {
    if (!DIAG.srcReady) {
      const sel = $("#diag-log-src");
      for (const s of ["lora-pkt-fwd", "lorarepeater", "upstreams", "helium-gateway", "systemd-timesyncd", "kernel", "all"]) sel.append(el("option", { value: s }, s));
      DIAG.srcReady = true;
    }
    await Promise.all([refreshDiagSystem(), refreshDiagServices(), refreshDiagLog(), refreshDiagHardware(), refreshDiagConfig()].map((p) => p.catch(console.error)));
  }

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
      if (kind === "lorawan_packets") { if (tab === "overview") prepend($("#ov-lw"), OV_LW_COLS, row); if (tab === "lorawan") prepend($("#lw-log"), LW_COLS, row); }
      if (kind === "mesh_packets") { if (tab === "overview") prepend($("#ov-mesh"), MESH_COLS.slice(0, 9).concat(MESH_COLS.slice(-1)), row); if (tab === "mesh") { prepend($("#mesh-log"), MESH_COLS, row); if (row.text) { const b = $("#mesh-msgs"); b.querySelector(".empty")?.remove(); b.prepend(msgEl(row)); } } }
      if (kind === "events" && tab === "events") prepend($("#events"), EVENT_COLS, row);
      rawFeed(kind, row);
    };
    setInterval(() => ws.readyState === 1 && ws.send("ping"), 30000);
  }

  // ---------------------------------------------------------------- orchestration
  const refreshTab = (name) => ({ overview: refreshOverview, lorawan: refreshLorawan, mesh: refreshMesh, upstreams: refreshUpstreams, helium: refreshHelium, events: refreshEvents, diag: refreshDiag, about: refreshAbout }[name]().catch(console.error));
  async function refreshAll() { try { await refreshStatus(); await refreshTab(tab); } catch (e) { console.error(e); } }
  // tabs read the status (gateway EUI, node, channels): load it before the first tab renders
  refreshStatus().catch(console.error).finally(() => showTab(tab));
  connectWs();
  setInterval(() => refreshStatus().catch(console.error), 15000);
  setInterval(() => refreshTab(tab), 60000);
  addEventListener("resize", () => refreshTab(tab));
})();
