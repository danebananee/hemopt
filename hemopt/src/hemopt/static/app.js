/* hemopt control panel.
 *
 * Plain JavaScript, no build step: the add-on serves this file as is behind
 * Home Assistant's ingress. Every request is relative so the <base> tag the
 * server injects routes it through the ingress prefix.
 */
"use strict";

const REFRESH_STATUS_MS = 30_000;
const REFRESH_PLAN_MS = 120_000;
const PRIORITY_LABELS = { 1: "Håll", 2: "Normal", 3: "Flexibel" };
const PRIORITY_HELP = {
  1: "Temperaturen hålls inom komfortbandet.",
  2: "Får svaja lite när elen är dyr.",
  3: "Bär mest av lastflytten.",
};
const CONTRACT_SHORT = {
  fixed: "fastpris",
  monthly: "månadspris",
  daily: "dygnspris",
  hourly: "timpris",
  quarterly: "kvartspris",
};

const state = {
  status: null,
  plan: null,
  rooms: null,
  savings: null,
  savingsDays: 30,
  priceBack: 0,
  history: { days: 2, res: "hour" },
  loaded: new Set(),
};

/* ------------------------------------------------------------------ utils */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const nf = (digits) =>
  new Intl.NumberFormat("sv-SE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmt0 = nf(0);
const fmt1 = nf(1);
const fmt2 = nf(2);

function kr(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  return `${(digits ? nf(digits) : fmt0).format(value)} kr`;
}

function ore(sekPerKwh) {
  if (sekPerKwh === null || sekPerKwh === undefined) return "–";
  return fmt0.format(sekPerKwh * 100);
}

function kw(value, digits = 1) {
  if (value === null || value === undefined) return "–";
  return `${nf(digits).format(value)} kW`;
}

function deg(value, digits = 1) {
  if (value === null || value === undefined) return "–";
  return `${nf(digits).format(value)} °C`;
}

const timeFmt = new Intl.DateTimeFormat("sv-SE", { hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat("sv-SE", { weekday: "short", day: "numeric", month: "short" });
const shortDayFmt = new Intl.DateTimeFormat("sv-SE", { weekday: "short" });
const dateFmt = new Intl.DateTimeFormat("sv-SE", { day: "numeric", month: "short" });
const monthFmt = new Intl.DateTimeFormat("sv-SE", { month: "short", year: "2-digit" });

const hhmm = (date) => timeFmt.format(date);

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function relativeDay(date, now = new Date()) {
  if (sameDay(date, now)) return "idag";
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (sameDay(date, tomorrow)) return "imorgon";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(date, yesterday)) return "igår";
  return dayFmt.format(date);
}

function ago(iso) {
  if (!iso) return "aldrig";
  const minutes = Math.round((nowDate().getTime() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "nyss";
  if (minutes < 60) return `${minutes} min sedan`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h sedan`;
  return `${Math.round(hours / 24)} dagar sedan`;
}

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {
      /* not JSON */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

/* Price classes: thirds of the prices in view, so "dyrt" always means
 * expensive relative to what the planner can choose between. */
function priceClasses(values) {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return () => "mid";
  const q = (p) => sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
  const low = q(1 / 3);
  const high = q(2 / 3);
  const spread = sorted[sorted.length - 1] - sorted[0];
  return (value) => {
    if (spread < 0.05) return "mid";
    if (value <= low) return "cheap";
    if (value >= high) return "dear";
    return "mid";
  };
}

const CLASS_WORD = { cheap: "billigt", mid: "normalt", dear: "dyrt" };
const WHEN = { now: "Nu", soon: "Snart", later: "Senare" };

/* ----------------------------------------------------------------- tooltip */

const tooltip = $("#tooltip");

function showTip(event, html) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 14;
  const { innerWidth, innerHeight } = window;
  const rect = tooltip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > innerHeight - 8) y = event.clientY - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTip() {
  tooltip.hidden = true;
}

/* ------------------------------------------------------------------ charts */

/* A chart frame: width follows the container, height is fixed per chart.
 * Returns scales and the root <svg>. Charts are redrawn on resize. */
function frame(host, { height = 240, left = 40, right = 44, top = 30, bottom = 28 } = {}) {
  const width = Math.max(host.clientWidth || 600, 280);
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  host.replaceChildren(root);
  return {
    root,
    width,
    height,
    x0: left,
    x1: width - right,
    y0: height - bottom,
    y1: top,
  };
}

const NICE_STEPS = [1, 2, 2.5, 5];
const NICE_STEPS_FIXED = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8];

function niceCandidates(raw, list) {
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const out = [];
  for (const e of [exp / 10, exp, exp * 10]) for (const n of list) out.push(n * e);
  return out.filter((v) => v >= raw * 0.5).sort((a, b) => a - b);
}

/* Rounded axis bounds and ticks. The smallest round step that covers the
 * data in at most count + 1 intervals wins. With `fixed` the axis gets
 * exactly `count` intervals, so a second axis can share the first one's grid
 * lines. */
function niceScale(lo, hi, { count = 4, fixed = false } = {}) {
  if (!(hi > lo)) hi = lo + 1;
  const raw = (hi - lo) / count;
  for (const step of niceCandidates(raw, fixed ? NICE_STEPS_FIXED : NICE_STEPS)) {
    const min = Math.floor(lo / step + 1e-9) * step;
    const max = fixed ? min + step * count : Math.ceil(hi / step - 1e-9) * step;
    const intervals = Math.round((max - min) / step);
    if (max < hi - 1e-9 || intervals > count + 1) continue;
    const ticks = [];
    for (let i = 0; i <= intervals; i += 1) {
      const v = min + i * step;
      ticks.push(Math.abs(v) < 1e-9 ? 0 : v);
    }
    return { min, max, step, ticks };
  }
  return { min: lo, max: hi, step: hi - lo, ticks: [lo, hi] };
}

function stepDigits(step) {
  if (Number.isInteger(Math.round(step * 1000) / 1000)) return 0;
  return Number.isInteger(Math.round(step * 10000) / 1000) ? 1 : 2;
}

function yAxis(f, scale, { side = "left", unit = "" } = {}) {
  const group = svg("g", { class: side === "left" ? "axis grid" : "axis" });
  const digits = stepDigits(scale.step);
  const format = nf(digits);
  for (const value of scale.ticks) {
    const y = f.y0 - ((value - scale.min) / (scale.max - scale.min)) * (f.y0 - f.y1);
    if (side === "left") {
      group.append(svg("line", { x1: f.x0, x2: f.x1, y1: y, y2: y }));
      group.append(svg("text", { x: f.x0 - 6, y: y + 4, "text-anchor": "end" }, format.format(value)));
    } else {
      group.append(svg("text", { x: f.x1 + 6, y: y + 4, "text-anchor": "start" }, format.format(value)));
    }
  }
  if (unit) {
    const x = side === "left" ? f.x0 - 6 : f.x1 + 6;
    group.append(svg("text", { x, y: f.y1 - 14, "text-anchor": side === "left" ? "end" : "start" }, unit));
  }
  f.root.append(group);
}

const yScale = (f, scale) => (v) => f.y0 - ((v - scale.min) / (scale.max - scale.min)) * (f.y0 - f.y1);

/* Time axis with hour labels and a weekday at each midnight. */
function timeAxis(f, times, xOf, { every = 3 } = {}) {
  const group = svg("g", { class: "axis" });
  times.forEach((time, i) => {
    const h = time.getHours();
    const m = time.getMinutes();
    if (m !== 0) return;
    const x = xOf(i);
    if (h === 0) {
      group.append(svg("line", { x1: x, x2: x, y1: f.y1, y2: f.y0, class: "baseline" }));
      group.append(svg("text", { x: x + 4, y: f.y1 + 10, "text-anchor": "start" }, shortDayFmt.format(time)));
    }
    if (h % every === 0) {
      group.append(svg("text", { x, y: f.y0 + 16, "text-anchor": "middle" }, String(h).padStart(2, "0")));
    }
  });
  f.root.append(group);
}

/* Invisible hover columns that drive the tooltip. */
function hoverColumns(f, count, xOf, stepWidth, html) {
  const band = svg("rect", { class: "hover-band", y: f.y1, height: f.y0 - f.y1, width: stepWidth, x: f.x0, visibility: "hidden" });
  const layer = svg("rect", {
    x: f.x0,
    y: f.y1,
    width: f.x1 - f.x0,
    height: f.y0 - f.y1,
    fill: "transparent",
  });
  const locate = (event) => {
    const box = f.root.getBoundingClientRect();
    const scale = f.width / box.width;
    const x = (event.clientX - box.left) * scale;
    const index = Math.max(0, Math.min(count - 1, Math.floor((x - f.x0) / stepWidth)));
    return index;
  };
  const move = (event) => {
    const index = locate(event);
    band.setAttribute("x", xOf(index));
    band.setAttribute("visibility", "visible");
    showTip(event, html(index));
  };
  layer.addEventListener("pointermove", move);
  layer.addEventListener("pointerdown", move);
  layer.addEventListener("pointerleave", () => {
    band.setAttribute("visibility", "hidden");
    hideTip();
  });
  f.root.append(band, layer);
}

function nowMarker(f, x, label = "nu") {
  f.root.append(
    svg("line", { x1: x, x2: x, y1: f.y1 - 4, y2: f.y0, class: "now-marker" }),
    svg("text", { x: x + 4, y: f.y1 + 22, class: "now-label" }, label),
  );
}

function stepPath(values, xOf, yOf, stepWidth) {
  let d = "";
  values.forEach((value, i) => {
    if (!Number.isFinite(value)) return;
    const x = xOf(i);
    const y = yOf(value);
    d += `${d ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}L${(x + stepWidth).toFixed(1)},${y.toFixed(1)}`;
  });
  return d;
}

const CLASS_FILL = { cheap: "var(--cheap)", mid: "var(--mid)", dear: "var(--dear)" };

/* The signature chart: the next 36 hours, price as colour, heat pump as bars. */
function drawHorizon(plan, now) {
  const host = $("#horizon-chart");
  if (!plan || !plan.times || !plan.times.length) {
    host.replaceChildren(el("p", { class: "empty", text: "Ingen plan ännu." }));
    return;
  }
  const times = plan.times.map((t) => new Date(t));
  const n = times.length;
  const f = frame(host, { height: 250 });
  const stepWidth = (f.x1 - f.x0) / n;
  const xOf = (i) => f.x0 + i * stepWidth;

  const pump = plan.heat_pump_kw;
  const kwScale = niceScale(0, Math.max(...pump, 0.5));
  const prices = plan.price.map((p) => p * 100);
  const priceScale = niceScale(Math.min(0, ...prices), Math.max(...prices, 50), {
    count: kwScale.ticks.length - 1,
    fixed: true,
  });
  const yKw = yScale(f, kwScale);
  const yPrice = yScale(f, priceScale);
  const classOf = priceClasses(plan.price);

  yAxis(f, kwScale, { unit: "kW" });
  yAxis(f, priceScale, { side: "right", unit: "öre" });

  const bars = svg("g");
  pump.forEach((value, i) => {
    const cls = classOf(plan.price[i]);
    const h = f.y0 - yKw(value);
    bars.append(
      svg("rect", {
        x: xOf(i) + 0.5,
        y: f.y0 - Math.max(h, 0),
        width: Math.max(stepWidth - 1, 0.6),
        height: Math.max(h, 0),
        fill: CLASS_FILL[cls],
        opacity: 0.9,
      }),
    );
    // A thin price-coloured strip under the axis shows the price even when
    // the pump is idle.
    bars.append(
      svg("rect", {
        x: xOf(i),
        y: f.y0 + 1,
        width: stepWidth + 0.2,
        height: 4,
        fill: CLASS_FILL[cls],
      }),
    );
  });
  f.root.append(bars);
  f.root.append(svg("path", { d: stepPath(prices, xOf, yPrice, stepWidth), class: "price-line" }));

  timeAxis(f, times, xOf);

  const nowIndex = times.findIndex((t, i) => t <= now && (i === n - 1 || times[i + 1] > now));
  if (nowIndex >= 0) nowMarker(f, xOf(nowIndex) + ((now - times[nowIndex]) / 900000) * stepWidth);

  hoverColumns(f, n, xOf, stepWidth, (i) => {
    const t = times[i];
    const cls = classOf(plan.price[i]);
    const lines = [
      `<b>${relativeDay(t, now)} ${hhmm(t)}</b>`,
      `Elpris ${ore(plan.price[i])} öre/kWh (${CLASS_WORD[cls]})`,
      `Värmepump ${kw(pump[i], 2)}`,
    ];
    if (plan.hot_water && plan.hot_water.charge_fraction[i] > 0.05) lines.push("Laddar varmvatten");
    if (plan.outdoor) lines.push(`Ute ${deg(plan.outdoor[i])}`);
    return lines.join("<br>");
  });
}

function drawSavingsChart(days) {
  const host = $("#savings-chart");
  if (!days.length) {
    host.replaceChildren(
      el("p", {
        class: "empty",
        text: "Inga dagar bokförda ännu. Första siffran kommer efter en kvart, en rättvisande bild efter någon vecka.",
      }),
    );
    return;
  }
  const f = frame(host, { height: 220, right: 16 });
  const n = days.length;
  const stepWidth = (f.x1 - f.x0) / n;
  const xOf = (i) => f.x0 + i * stepWidth;
  const tops = days.map((d) => Math.max(d.contract_effect_sek, 0) + Math.max(d.control_effect_sek, 0));
  const bottoms = days.map((d) => Math.min(d.contract_effect_sek, 0) + Math.min(d.control_effect_sek, 0));
  const scale = niceScale(Math.min(...bottoms, 0), Math.max(...tops, 1));
  const yOf = yScale(f, scale);
  yAxis(f, scale, { unit: "kr" });
  f.root.append(svg("line", { x1: f.x0, x2: f.x1, y1: yOf(0), y2: yOf(0), class: "baseline" }));

  const g = svg("g");
  days.forEach((day, i) => {
    const w = Math.max(stepWidth * 0.72, 1);
    const x = xOf(i) + (stepWidth - w) / 2;
    let up = 0;
    let down = 0;
    for (const [value, fill] of [
      [day.contract_effect_sek, "var(--cheap)"],
      [day.control_effect_sek, "var(--good)"],
    ]) {
      if (value >= 0) {
        g.append(svg("rect", { x, width: w, y: yOf(up + value), height: yOf(up) - yOf(up + value), fill }));
        up += value;
      } else {
        g.append(svg("rect", { x, width: w, y: yOf(down), height: yOf(down + value) - yOf(down), fill, opacity: 0.55 }));
        down += value;
      }
    }
  });
  f.root.append(g);

  const axis = svg("g", { class: "axis" });
  const every = Math.ceil(n / 10);
  days.forEach((day, i) => {
    if (i % every) return;
    const date = new Date(`${day.day}T12:00:00`);
    axis.append(svg("text", { x: xOf(i) + stepWidth / 2, y: f.y0 + 16, "text-anchor": "middle" }, dateFmt.format(date)));
  });
  f.root.append(axis);

  hoverColumns(f, n, xOf, stepWidth, (i) => {
    const d = days[i];
    const date = new Date(`${d.day}T12:00:00`);
    const cover = d.coverage < 0.95 ? `<br>Bokfört ${fmt0.format(d.coverage * 100)} % av dygnet` : "";
    return (
      `<b>${dayFmt.format(date)}</b><br>` +
      `Kvartspris: ${kr(d.contract_effect_sek, 2)}<br>` +
      `Styrning: ${kr(d.control_effect_sek, 2)}<br>` +
      `<b>Totalt ${kr(d.total_sek, 2)}</b>${cover}`
    );
  });
}

function drawDhw(plan, now) {
  const host = $("#dhw-chart");
  if (!plan || !plan.hot_water) {
    host.replaceChildren(el("p", { class: "empty", text: "Varmvattnet planeras inte (ingen givare för tanken)." }));
    return;
  }
  const times = plan.times.map((t) => new Date(t));
  const n = times.length;
  const f = frame(host, { height: 170, right: 16 });
  const stepWidth = (f.x1 - f.x0) / n;
  const xOf = (i) => f.x0 + i * stepWidth;
  const temps = plan.hot_water.temperature;
  const scale = niceScale(Math.min(...temps, 40), Math.max(...temps, 55), { count: 3 });
  const yOf = yScale(f, scale);
  yAxis(f, scale, { unit: "°C" });
  const g = svg("g");
  plan.hot_water.charge_fraction.forEach((c, i) => {
    if (c < 0.02) return;
    g.append(svg("rect", { x: xOf(i), y: f.y1, width: stepWidth + 0.2, height: f.y0 - f.y1, fill: "var(--heat)", opacity: 0.18 * c + 0.08 }));
  });
  f.root.append(g);
  f.root.append(svg("path", { d: stepPath(temps, xOf, yOf, stepWidth), class: "price-line", style: "stroke: var(--heat)" }));
  timeAxis(f, times, xOf, { every: 6 });
  const nowIndex = times.findIndex((t, i) => t <= now && (i === n - 1 || times[i + 1] > now));
  if (nowIndex >= 0) nowMarker(f, xOf(nowIndex));
  hoverColumns(f, n, xOf, stepWidth, (i) => {
    const t = times[i];
    const charging = plan.hot_water.charge_fraction[i] > 0.05 ? "<br>Laddar" : "";
    return `<b>${relativeDay(t, now)} ${hhmm(t)}</b><br>Tank ${deg(temps[i])}${charging}`;
  });
}

function drawPrices(payload, now) {
  const host = $("#price-chart");
  const points = payload.points || [];
  if (!points.length) {
    host.replaceChildren(el("p", { class: "empty", text: "Inga spotpriser kunde hämtas." }));
    return;
  }
  const times = points.map((p) => new Date(p.t));
  const totals = points.map((p) => p.total * 100);
  const n = points.length;
  const f = frame(host, { height: 240, right: 16 });
  const stepWidth = (f.x1 - f.x0) / n;
  const xOf = (i) => f.x0 + i * stepWidth;
  const scale = niceScale(Math.min(0, ...totals), Math.max(...totals, 50));
  const yOf = yScale(f, scale);
  const classOf = priceClasses(points.map((p) => p.total));
  yAxis(f, scale, { unit: "öre" });
  const g = svg("g");
  points.forEach((p, i) => {
    const y = yOf(Math.max(totals[i], 0));
    g.append(svg("rect", { x: xOf(i), y, width: Math.max(stepWidth - (n > 200 ? 0 : 0.6), 0.5), height: yOf(0) - y, fill: CLASS_FILL[classOf(p.total)] }));
  });
  f.root.append(g);
  if (n <= 200) timeAxis(f, times, xOf, { every: n > 120 ? 6 : 3 });
  else {
    const axis = svg("g", { class: "axis" });
    times.forEach((t, i) => {
      if (t.getHours() === 0 && t.getMinutes() === 0 && t.getDate() % (n > 1500 ? 5 : 1) === 0) {
        axis.append(svg("text", { x: xOf(i), y: f.y0 + 16, "text-anchor": "middle" }, dateFmt.format(t)));
      }
    });
    f.root.append(axis);
  }
  const nowIndex = times.findIndex((t, i) => t <= now && (i === n - 1 || times[i + 1] > now));
  if (nowIndex >= 0) nowMarker(f, xOf(nowIndex));
  hoverColumns(f, n, xOf, stepWidth, (i) => {
    const t = times[i];
    return `<b>${relativeDay(t, now)} ${hhmm(t)}</b><br>${fmt0.format(totals[i])} öre/kWh totalt<br>Spot ${fmt0.format(points[i].spot * 100)} öre exkl. moms`;
  });
}

function drawHistory(payload, now) {
  const host = $("#history-chart");
  const points = payload.points || [];
  if (!points.length) {
    host.replaceChildren(el("p", { class: "empty", text: "Ingen förbrukning uppmätt ännu. Välj en elmätare under System." }));
    return;
  }
  const hourly = payload.resolution === "hour";
  const values = points.map((p) => (hourly ? p.kw : p.kwh));
  const times = points.map((p) => new Date(p.t));
  const n = points.length;
  const f = frame(host, { height: 230, right: 16 });
  const stepWidth = (f.x1 - f.x0) / n;
  const xOf = (i) => f.x0 + i * stepWidth;
  const threshold = hourly && payload.peak_enabled ? payload.peak_threshold_kw : null;
  const scale = niceScale(0, Math.max(...values, threshold || 0, 0.5));
  const yOf = yScale(f, scale);
  yAxis(f, scale, { unit: hourly ? "kW" : "kWh" });
  const g = svg("g");
  values.forEach((v, i) => {
    const over = threshold && v > threshold;
    g.append(svg("rect", {
      x: xOf(i) + stepWidth * 0.1,
      y: yOf(v),
      width: Math.max(stepWidth * 0.8, 0.8),
      height: f.y0 - yOf(v),
      fill: over ? "var(--dear)" : "var(--cheap)",
      opacity: 0.85,
    }));
  });
  f.root.append(g);
  if (threshold) {
    f.root.append(svg("line", { x1: f.x0, x2: f.x1, y1: yOf(threshold), y2: yOf(threshold), stroke: "var(--dear)", "stroke-dasharray": "5 4" }));
  }
  if (hourly) timeAxis(f, times, xOf, { every: n > 60 ? 6 : 3 });
  else {
    const axis = svg("g", { class: "axis" });
    const every = Math.ceil(n / 10);
    times.forEach((t, i) => {
      if (i % every) return;
      const label = payload.resolution === "month" ? monthFmt.format(t) : dateFmt.format(t);
      axis.append(svg("text", { x: xOf(i) + stepWidth / 2, y: f.y0 + 16, "text-anchor": "middle" }, label));
    });
    f.root.append(axis);
  }
  hoverColumns(f, n, xOf, stepWidth, (i) => {
    const t = times[i];
    const p = points[i];
    if (hourly) return `<b>${relativeDay(t, now)} ${hhmm(t)}</b><br>Timmedel ${kw(p.kw, 2)}`;
    const label = payload.resolution === "month" ? monthFmt.format(t) : payload.resolution === "week" ? `vecka från ${dateFmt.format(t)}` : dayFmt.format(t);
    return `<b>${label}</b><br>${fmt1.format(p.kwh)} kWh<br>Medel ${kw(p.kw, 2)}`;
  });
}

/* ------------------------------------------------------------------ views */

function renderMasthead() {
  const s = state.status;
  if (!s) return;
  const toggle = $("#control-toggle");
  toggle.checked = Boolean(s.control_enabled);
  toggle.disabled = Boolean(s.booting);
  $("#control-hint").textContent = s.control_enabled ? "På – hemopt ställer termostaterna" : "Av – hemopt räknar bara";
  const contract = CONTRACT_SHORT[s.contract] || "";
  const area = s.price_area ? `Elområde ${s.price_area.replace("SE", "")}` : "";
  $("#brand-sub").textContent = [area, contract && `du har ${contract}`].filter(Boolean).join(", ");
}

function renderSummary() {
  const s = state.status;
  const line = $("#summary-line");
  const sub = $("#summary-sub");
  if (!s) return;
  if (s.booting || s.starting) {
    line.textContent = "hemopt startar och räknar fram första planen…";
    sub.textContent = "Det kan ta några minuter på en Raspberry Pi.";
    return;
  }
  line.textContent = s.model_action || "Planen är klar.";
  const parts = [];
  if (!s.control_enabled) parts.push("Styrningen är av, så termostaterna rörs inte. Planen visar vad hemopt skulle göra.");
  else if (s.rooms_with_climate) parts.push(`hemopt styr ${s.rooms_with_climate} rum.`);
  if (s.guard_blocking) parts.push(`Effektvakten håller emot: ${s.guard_reason}.`);
  if (!s.prices_available) parts.push("Spotpriserna kunde inte hämtas.");
  if (!s.home_assistant_online) parts.push("Home Assistant svarar inte.");
  sub.textContent = parts.join(" ");
}

function figure(label, value, unit, note, tone) {
  return el(
    "div",
    { class: "figure" },
    el("span", { class: "figure-label", text: label }),
    el("span", { class: `figure-value ${tone ? `tone-${tone}` : ""}` }, value, unit ? el("small", { text: unit }) : null),
    note ? el("span", { class: "figure-note", text: note }) : null,
  );
}

function renderFigures() {
  const s = state.status;
  const plan = state.plan;
  const box = $("#figures");
  if (!s) return;
  const items = [];

  if (s.current_price_sek !== undefined) {
    let tone = null;
    let note = "inklusive skatt och nät";
    if (plan && plan.price) {
      const cls = priceClasses(plan.price)(s.current_price_sek);
      tone = cls === "mid" ? null : cls;
      note = `${CLASS_WORD[cls]} jämfört med kommande dygn`;
    }
    items.push(figure("Elpris nu", ore(s.current_price_sek), "öre/kWh", note, tone));
  }

  if (state.rooms && state.rooms.length) {
    const measured = state.rooms.filter((r) => r.temperature !== null);
    const inside = measured.filter((r) => r.temperature >= r.comfort_min - 0.2 && r.temperature <= r.comfort_max + 0.3);
    const cold = measured
      .filter((r) => r.temperature < r.comfort_min - 0.2)
      .sort((a, b) => a.temperature - a.comfort_min - (b.temperature - b.comfort_min));
    const note = cold.length ? `kallast: ${cold[0].name} ${deg(cold[0].temperature)}` : "alla rum inom sitt band";
    items.push(
      figure(
        "Inomhus",
        `${inside.length}`,
        `av ${measured.length} rum`,
        note,
        cold.length ? "warn" : "good",
      ),
    );
  }

  const sav = state.savings;
  if (sav && sav.totals && sav.totals.measured_days > 0) {
    const perDay = sav.saving_per_day_sek;
    items.push(
      figure(
        `Hade sparat, ${fmt0.format(Math.min(sav.totals.measured_days, state.savingsDays))} dagar`,
        fmt0.format(sav.totals.total_sek),
        "kr",
        perDay !== null ? `≈ ${kr(perDay, 1)} per dygn med kvartspris och styrning` : "med kvartspris och styrning",
        sav.totals.total_sek > 0 ? "good" : null,
      ),
    );
  } else {
    items.push(figure("Besparing", "–", "", "bokföringen har precis börjat"));
  }

  if (s.planned_power_kw !== undefined) {
    items.push(
      figure(
        "Värmepump",
        nf(1).format(s.planned_power_kw),
        "kW",
        s.control_enabled ? "planerat just nu" : "vad planen vill just nu",
      ),
    );
  }
  box.replaceChildren(...items);
}

function renderActions() {
  const s = state.status;
  const list = $("#model-body");
  const actions = (s && s.model_actions) || [];
  if (!actions.length) {
    list.replaceChildren(el("li", { class: "empty", text: s && s.starting ? "Väntar på första planen…" : "Inget särskilt att berätta just nu." }));
    return;
  }
  list.replaceChildren(
    ...actions.map((a) =>
      el(
        "li",
        { class: `kind-${a.kind || "info"}` },
        el("span", { class: "when", text: WHEN[a.when] || a.when || "Nu" }),
        el("span", { class: "what" }, el("b", { text: a.title }), a.detail ? el("span", { text: a.detail }) : null),
      ),
    ),
  );
}

function renderErrors() {
  const errors = (state.status && state.status.errors) || [];
  $("#error-card").hidden = !errors.length;
  $("#errors").replaceChildren(...errors.map((e) => el("li", { text: e })));
}

function renderPlanNotes() {
  const plan = state.plan;
  const notes = (plan && plan.notes) || [];
  $("#plan-notes").replaceChildren(...notes.map((n) => el("li", { text: n })));
  if (plan && plan.times) {
    const hours = Math.round((plan.times.length * plan.step_minutes) / 60);
    const saved = plan.baseline_energy_cost_sek + plan.baseline_peak_cost_sek - (plan.energy_cost_sek + plan.peak_cost_sek);
    const text = `${hours} timmar framåt. Staplarna är värmepumpens planerade effekt, färgen elpriset just den kvarten.`;
    const extra = Number.isFinite(saved) && saved > 0.5 ? ` Planen är ${kr(saved)} billigare än att värma jämnt över perioden, räknat på kvartspris.` : "";
    $("#horizon-sub").textContent = text + extra;
  }
}

function renderFooter() {
  const s = state.status;
  if (!s) return;
  const bits = [`Plan ${ago(s.last_plan)}`, `mätning ${ago(s.last_sample)}`];
  if (s.last_training) bits.push(`modeller lärda ${ago(s.last_training)}`);
  if (s.version) bits.push(`version ${s.version}`);
  $("#footer-status").textContent = bits.join(", ");
}

/* Savings ---------------------------------------------------------------- */

function renderSavings() {
  const sav = state.savings;
  const ladder = $("#savings-ladder");
  if (!sav) return;
  const t = sav.totals;
  const current = CONTRACT_SHORT[sav.contract] || sav.contract_name;
  $("#savings-sub").textContent =
    `Varje kvart räknar hemopt vad huset hade kostat med kvartspris, med och utan styrning, jämfört med ditt ${current}. ` +
    "Energidelen av elräkningen, inklusive påslag, skatt och moms.";

  if (!t || !t.measured_days) {
    ladder.replaceChildren(el("p", { class: "empty", text: "Inget bokfört ännu." }));
    drawSavingsChart([]);
    $("#savings-comfort").replaceChildren();
    $("#savings-notes").replaceChildren(...(sav.notes || []).map((n) => el("li", { text: n })));
    return;
  }

  const withQuarter = t.cost_quarter_sek;
  const withHemopt = t.cost_with_hemopt_sek;
  const rung = (name, cost, delta, best) =>
    el(
      "div",
      { class: `rung ${best ? "best" : ""}` },
      el("span", { class: "rung-name", text: name }),
      el("span", { class: "rung-cost", text: kr(cost) }),
      delta === null
        ? el("span", { class: "rung-delta muted", text: `${fmt1.format(t.house_kwh)} kWh på ${fmt1.format(t.measured_days)} dygn` })
        : el("span", { class: `rung-delta ${delta > 0 ? "tone-good" : "tone-bad"}`, text: delta > 0 ? `${kr(delta)} billigare` : `${kr(-delta)} dyrare` }),
    );
  ladder.replaceChildren(
    rung(`Ditt ${current}, som idag`, t.cost_now_sek, null, false),
    sav.contract === "quarterly" ? null : rung("Kvartspris, samma förbrukning", withQuarter, t.contract_effect_sek, false),
    rung("Kvartspris och hemopt styr", withHemopt, t.total_sek, t.total_sek > 0),
  );

  drawSavingsChart(sav.days || []);

  const comfort = $("#savings-comfort");
  const extra = t.optimised_cold_dh - t.reference_cold_dh;
  let pill;
  let text;
  if (extra <= 0.5) {
    pill = el("span", { class: "pill good", text: "Samma komfort" });
    text = "Styrningen hade inte gjort något rum kallare än dess komfortband.";
  } else {
    const perDay = extra / Math.max(t.measured_days, 1);
    pill = el("span", { class: `pill ${perDay > 2 ? "warn" : "neutral"}`, text: `${fmt1.format(perDay)} gradtimmar/dygn` });
    text = "Så mycket under komfortbandet hade rummen med låg prioritet legat sammanlagt. Höj prioriteten för rum som känns för kalla.";
  }
  comfort.replaceChildren(pill, el("span", { class: "muted", text }));

  const notes = [...(sav.notes || [])];
  notes.push("Staplarna: blått är vinsten av kvartspris med samma förbrukning, grönt vinsten av att hemopt flyttar värmen till billigare kvartar.");
  $("#savings-notes").replaceChildren(...notes.map((n) => el("li", { text: n })));
}

function renderAdvice(advice) {
  const body = $("#advice-body");
  if (!advice) return;
  const items = (advice.actions || []).filter((a) => a.title);
  const nodes = [];
  if (items.length) {
    nodes.push(
      el(
        "div",
        { class: "advice-list" },
        ...items.map((a) =>
          el(
            "div",
            { class: `advice-item status-${a.status || "info"}` },
            el(
              "div",
              { class: "row" },
              el("b", { text: a.title }),
              a.annual_saving_sek > 0 ? el("span", { class: "pill good", text: `${kr(a.annual_saving_sek)}/år` }) : null,
            ),
            a.summary ? el("p", { text: a.summary }) : null,
            a.action ? el("p", { text: a.action }) : null,
            a.caveat ? el("p", { class: "muted", text: a.caveat }) : null,
          ),
        ),
      ),
    );
  }
  const scenarios = advice.scenarios || [];
  if (scenarios.length) {
    const rows = scenarios.map((s) =>
      el(
        "tr",
        { class: s.is_current ? "current" : "" },
        el("td", { text: `${s.name}${s.is_current ? " (nu)" : ""}` }),
        el("td", { text: `${fmt0.format(s.without_control.ore_per_kwh)} öre` }),
        el("td", { text: kr(s.without_control.annual_sek) }),
        el("td", { text: kr(s.with_control.annual_sek) }),
      ),
    );
    nodes.push(
      el(
        "div",
        { class: "table-wrap" },
        el(
          "table",
          { class: "data" },
          el(
            "thead",
            {},
            el("tr", {}, el("th", { text: "Avtal" }), el("th", { text: "Snittpris" }), el("th", { text: "Per år, som idag" }), el("th", { text: "Per år, med lastflytt" })),
          ),
          el("tbody", {}, ...rows),
        ),
      ),
    );
    nodes.push(el("p", { class: "muted", style: "margin-top:8px", text: `Uppräknat från ${fmt1.format(advice.measured_days)} dygns mätning. «Med lastflytt» är ett tak: hela den flyttbara delen läggs i dygnets billigaste timmar.` }));
  }
  for (const note of advice.notes || []) nodes.push(el("p", { class: "muted", text: note }));
  if (!nodes.length) nodes.push(el("p", { class: "empty", text: "Ingen rådgivning ännu – den behöver minst ett dygns mätning." }));
  body.replaceChildren(...nodes);
}

/* Rooms ------------------------------------------------------------------ */

const FLOOR_TYPE_TEXT = { concrete: "betongplatta", light: "lätt bjälklag" };

function roomBand(room) {
  const lo = Math.min(room.comfort_min - 1.5, room.temperature ?? 99, room.planned_setpoint ?? 99) - 0.3;
  const hi = Math.max(room.comfort_max + 1.5, room.temperature ?? -99, room.planned_setpoint ?? -99) + 0.3;
  const pos = (v) => `${(((v - lo) / (hi - lo)) * 100).toFixed(2)}%`;
  const band = el("div", { class: "band" }, el("div", { class: "band-track" }));
  band.append(el("div", { class: "band-comfort", style: `left:${pos(room.comfort_min)};width:calc(${pos(room.comfort_max)} - ${pos(room.comfort_min)})` }));
  if (room.planned_setpoint !== null && room.planned_setpoint !== undefined) {
    band.append(el("div", { class: "band-target", style: `left:${pos(room.planned_setpoint)}`, title: `Planerat börvärde ${deg(room.planned_setpoint)}` }));
  }
  if (room.temperature !== null) {
    const cls = room.temperature < room.comfort_min - 0.2 ? "cold" : room.temperature > room.comfort_max + 0.3 ? "warm" : "";
    band.append(el("div", { class: `band-dot ${cls}`, style: `left:${pos(room.temperature)}`, title: `Nu ${deg(room.temperature)}` }));
  }
  const labels = el(
    "div",
    { class: "band-labels" },
    el("span", {}, "Komfort ", el("b", { text: `${fmt1.format(room.comfort_min)}–${fmt1.format(room.comfort_max)}` })),
    el("span", {}, room.temperature !== null ? el("b", { text: deg(room.temperature) }) : "ingen mätning", room.humidity ? ` · ${fmt0.format(room.humidity)} %` : ""),
  );
  return el("div", { class: "band-wrap" }, band, labels);
}

function roomRow(room) {
  const priority = el(
    "div",
    { class: "segmented", role: "group", "aria-label": `Prioritet ${room.name}` },
    ...[1, 2, 3].map((p) =>
      el("button", {
        type: "button",
        "aria-pressed": room.priority === p ? "true" : "false",
        title: PRIORITY_HELP[p],
        text: PRIORITY_LABELS[p],
        onclick: () => setPriority(room, p),
      }),
    ),
  );

  const m = room.model;
  const fitted = m.fitted
    ? `Inlärd på ${fmt0.format(m.history_days || m.samples / 96)} dagars data; lärs om var sjätte timme och blir bättre ju längre hemopt körs.`
    : "Inte inlärd ännu – använder startvärden för golvtypen.";
  const quality =
    m.rmse_4h !== null && m.rmse_4h !== undefined
      ? `Prognosfel 4 h framåt: ±${fmt2.format(m.rmse_4h)} °C.`
      : "";
  const minInput = el("input", { type: "number", step: "0.5", min: "5", max: "30", value: room.comfort_min, "aria-label": "Lägsta" });
  const maxInput = el("input", { type: "number", step: "0.5", min: "5", max: "32", value: room.comfort_max, "aria-label": "Högsta" });
  const save = el("button", { type: "button", class: "btn ghost", text: "Spara" });
  const status = el("span", { class: "muted" });
  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "";
    try {
      await api(`api/rooms/${encodeURIComponent(room.key)}/comfort`, {
        method: "POST",
        body: { comfort_min: Number(minInput.value), comfort_max: Number(maxInput.value) },
      });
      status.textContent = "Sparat, planen är omräknad.";
      await Promise.all([loadRooms(), loadPlan()]);
    } catch (error) {
      status.textContent = `Kunde inte spara: ${error.message}`;
    } finally {
      save.disabled = false;
    }
  });

  const detail = el(
    "details",
    { class: "room-more" },
    el("summary", { text: "Komfort och modell" }),
    el(
      "div",
      { class: "room-detail" },
      el(
        "div",
        {},
        el("p", { class: "muted", text: "Komfortband (°C)" }),
        el("div", { class: "comfort-form" }, minInput, "–", maxInput, save),
        status,
      ),
      el(
        "dl",
        { class: "facts" },
        el("dt", { text: "Golv" }),
        el("dd", { text: FLOOR_TYPE_TEXT[room.floor_type] || room.floor_type }),
        el("dt", { text: "Tröghet" }),
        el("dd", { text: `${fmt0.format(m.tau_hours)} h` }),
        el("dt", { text: "Golvets fördröjning" }),
        el("dd", { text: m.tau_slab_hours > 0 ? `${fmt1.format(m.tau_slab_hours)} h` : "ingen" }),
        el("dt", { text: "Uppvärmning" }),
        el("dd", { text: `${fmt2.format(m.k_heat_per_hour)} °C/h fullt på` }),
        m.k_sun_per_hour > 0 ? el("dt", { text: "Sol" }) : null,
        m.k_sun_per_hour > 0 ? el("dd", { text: `+${fmt2.format(m.k_sun_per_hour)} °C/h i full sol` }) : null,
        m.k_wind_per_hour > 0 ? el("dt", { text: "Blåst" }) : null,
        m.k_wind_per_hour > 0 ? el("dd", { text: `tappar värme ${fmt1.format(m.k_wind_per_hour * m.tau_hours * 100)} % snabbare per m/s vind` }) : null,
        m.k_stove_per_hour > 0 ? el("dt", { text: "Braskamin" }) : null,
        m.k_stove_per_hour > 0 ? el("dd", { text: `+${fmt2.format(m.k_stove_per_hour)} °C/h` }) : null,
        el("dt", { text: "Termostat" }),
        el("dd", { text: room.thermostat_setpoint !== null ? deg(room.thermostat_setpoint) : "okänd" }),
      ),
      el("p", { class: "muted", text: `${fitted} ${quality}` }),
    ),
  );

  return el(
    "div",
    { class: "room" },
    el("div", { class: "room-name" }, el("b", { text: room.name }), el("small", { text: PRIORITY_HELP[room.priority] })),
    roomBand(room),
    el("div", { class: "room-controls" }, priority),
    detail,
  );
}

function renderRooms() {
  const host = $("#rooms");
  const rooms = state.rooms || [];
  if (!rooms.length) {
    host.replaceChildren(el("p", { class: "empty", text: "Inga rum konfigurerade." }));
    return;
  }
  const floors = new Map();
  for (const room of rooms) {
    const key = room.floor || "Rum";
    if (!floors.has(key)) floors.set(key, []);
    floors.get(key).push(room);
  }
  host.replaceChildren(
    ...Array.from(floors.entries()).map(([name, list]) => {
      const types = new Set(list.map((r) => FLOOR_TYPE_TEXT[r.floor_type] || r.floor_type));
      return el(
        "div",
        { class: "floor" },
        el("div", { class: "floor-head" }, el("h3", { text: name }), el("span", { text: Array.from(types).join(", ") })),
        ...list.map(roomRow),
      );
    }),
  );
}

async function setPriority(room, priority) {
  if (room.priority === priority) return;
  const previous = room.priority;
  room.priority = priority;
  renderRooms();
  try {
    await api(`api/rooms/${encodeURIComponent(room.key)}/priority`, { method: "POST", body: { priority } });
    await loadPlan();
  } catch (error) {
    room.priority = previous;
    renderRooms();
    alert(`Kunde inte ändra prioritet: ${error.message}`);
  }
}

function renderDhwSub() {
  const s = state.status;
  const plan = state.plan;
  const sub = $("#dhw-sub");
  if (!plan || !plan.hot_water) {
    sub.textContent = "Ingen tankgivare konfigurerad.";
    return;
  }
  const temps = plan.hot_water.temperature;
  const perDay = s ? s.hot_water_kwh_per_day : null;
  sub.textContent =
    `Tanken ligger mellan ${fmt0.format(Math.min(...temps))} och ${fmt0.format(Math.max(...temps))} °C. ` +
    (perDay ? `Uppmätt användning ≈ ${fmt1.format(perDay)} kWh per dygn. ` : "") +
    "Skuggat område: planerad laddning.";
}

const FLAME = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-2.2 1-3.6 2.2-4.8.2 1.6 1 2.6 2 3 0-3.2-1-6.2.8-9.2z"/></svg>';

function stoveButton(lit) {
  const button = el("button", {
    type: "button",
    class: lit ? "btn ghost" : "btn",
    text: lit ? "Brasan har slocknat" : "Jag har tänt brasan",
  });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      renderStove(await api("api/wood-stove/lit", { method: "POST", body: { lit: !lit } }));
      loadStatus();
    } catch (error) {
      alert(`Kunde inte markera brasan: ${error.message}`);
      button.disabled = false;
    }
  });
  return button;
}

const STOVE_SOURCE = {
  manual: "markerad av dig",
  model: "upptäckt av hemopt",
  binary: "enligt givaren",
  temperature: "enligt givaren",
};

function renderStove(report) {
  const body = $("#stove-body");
  if (!report) return;
  $("#stove-title").textContent = report.name || "Braskamin";
  const reading = report.reading || {};
  const lit = Boolean(reading.lit);
  const source = lit && STOVE_SOURCE[reading.source] ? ` (${STOVE_SOURCE[reading.source]})` : "";
  const nodes = [
    el(
      "div",
      { class: "stove-state" },
      el("span", { class: `flame ${lit ? "lit" : ""}`, html: FLAME }),
      el(
        "div",
        { class: "stove-text" },
        el("b", { text: `${report.summary || (lit ? "Brasan brinner" : "Släckt")}${source}` }),
        report.detail ? el("p", { class: "muted", text: report.detail }) : null,
      ),
    ),
  ];
  // With a sensor the stove reports itself; otherwise the button is how
  // hemopt learns, and it stays useful after that to correct the detector.
  const hasSensor = reading.source === "binary" || reading.source === "temperature";
  if (!hasSensor) nodes.push(el("div", { class: "stove-actions" }, stoveButton(lit)));

  const effects = (report.effects || []).filter((e) => e.k_stove_per_hour > 0);
  if (effects.length) {
    nodes.push(el("p", { class: "stove-label", text: "Så mycket värmer brasan:" }));
    nodes.push(
      el(
        "dl",
        { class: "facts" },
        ...effects.flatMap((e) => [
          el("dt", { text: e.room_name }),
          el("dd", { text: `+${fmt2.format(e.k_stove_per_hour)} °C/h${e.equivalent_kw ? `, som ${kw(e.equivalent_kw)} golvvärme` : ""}` }),
        ]),
      ),
    );
  } else if (report.status !== "not_started") {
    nodes.push(el("p", { class: "muted", text: `${report.sessions_observed || 0} brasor, ${fmt1.format(report.hours_lit_observed || 0)} timmar hittills.` }));
  }
  const windows = report.windows || [];
  if (windows.length && effects.length) {
    nodes.push(el("p", { class: "stove-label", text: "Bäst att tända:" }));
    nodes.push(
      el(
        "ul",
        { class: "windows" },
        ...windows.slice(0, 3).map((w) => {
          const start = new Date(w.start);
          const end = new Date(w.end);
          const saving = w.saving_sek !== null && w.saving_sek !== undefined && w.saving_sek >= 1 ? `sparar ≈ ${kr(w.saving_sek)}, ` : "";
          return el(
            "li",
            {},
            el("span", { text: `${relativeDay(start)} ${hhmm(start)}–${hhmm(end)}` }),
            el("span", { class: "muted", text: `${saving}${ore(w.mean_price_sek)} öre/kWh, ${deg(w.mean_outdoor_c, 0)} ute` }),
          );
        }),
      ),
    );
  }
  body.replaceChildren(...nodes);
}

/* Energy ----------------------------------------------------------------- */

function renderHistoryFacts(payload) {
  const facts = $("#history-facts");
  const items = [];
  if (payload.total_kwh !== undefined) {
    items.push(["Förbrukat", `${fmt0.format(payload.total_kwh)} kWh på ${fmt1.format(payload.measured_days)} dygn`]);
  }
  if (payload.kwh_per_day) items.push(["Per dygn", `${fmt1.format(payload.kwh_per_day)} kWh`]);
  if (payload.peak_hour_kw) {
    const at = new Date(payload.peak_hour_at);
    items.push(["Högsta timme", `${kw(payload.peak_hour_kw, 2)} ${relativeDay(at)} ${hhmm(at)}`]);
  }
  facts.replaceChildren(...items.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
}

function renderPeaks(peaks) {
  const body = $("#peak-body");
  const sub = $("#peak-sub");
  if (!peaks) return;
  if (!peaks.enabled) {
    sub.textContent = "Ingen effektavgift konfigurerad. hemopt optimerar bara mot elpriset.";
    body.replaceChildren(
      el("p", { class: "muted", text: "Alla nätbolag ska ha infört en effektavgift senast 1 januari 2027. När din faktura visar en, slå på peak_tariff i konfigurationen." }),
    );
    return;
  }
  const w = peaks.window;
  sub.textContent = `Snittet av de ${peaks.n_peaks} högsta timmarna på olika dygn, ${w.weekdays_only ? "vardagar " : ""}${w.hour_start}–${w.hour_end}. ${kr(peaks.price_per_kw_sek)} per kW och månad.`;
  const hour = peaks.current_hour;
  const allowed = hour.allowed_kw;
  const used = hour.energy_kwh;
  const share = peaks.threshold_kw > 0 ? Math.min(used / peaks.threshold_kw, 1.2) : 0;
  const nodes = [
    el(
      "dl",
      { class: "facts" },
      el("dt", { text: "Månadens snitt" }),
      el("dd", { text: `${kw(peaks.average_kw, 2)} → ${kr(peaks.projected_cost_sek)}` }),
      el("dt", { text: "Tröskel för ny topp" }),
      el("dd", { text: kw(peaks.threshold_kw, 2) }),
      el("dt", { text: "Timmen som pågår" }),
      el("dd", { text: `${fmt2.format(used)} kWh förbrukat, ${fmt0.format(hour.minutes_remaining)} min kvar${allowed !== null ? `, ${kw(allowed, 1)} till tillåts` : ""}` }),
    ),
    el("div", { class: "meter-gauge" }, el("span", { class: share >= 1 ? "over" : "", style: `width:${(Math.min(share, 1) * 100).toFixed(1)}%` })),
  ];
  if (peaks.counted.length) {
    nodes.push(el("p", { style: "margin-top:12px", text: "Toppar som räknas just nu:" }));
    nodes.push(
      el(
        "div",
        { class: "peaks" },
        ...peaks.counted.map((p) =>
          el("div", { class: "peak" }, el("b", { text: kw(p.kw, 2) }), el("small", { text: `${dateFmt.format(new Date(`${p.day}T12:00:00`))} kl ${String(p.hour).padStart(2, "0")}` })),
        ),
      ),
    );
  }
  body.replaceChildren(...nodes);
}

/* System ----------------------------------------------------------------- */

function renderSystems() {
  const s = state.status;
  if (!s) return;
  const row = (level, title, detail) =>
    el("li", { class: level }, el("span", { class: "dot" }), el("div", {}, el("b", { text: title }), detail ? el("small", { text: detail }) : null));
  const diag = s.ha_diagnosis || {};
  const rows = [
    row(
      s.home_assistant_online ? "ok" : "bad",
      s.home_assistant_online ? "Home Assistant svarar" : "Home Assistant svarar inte",
      s.home_assistant_online ? s.ha_base_url : diag.error || (s.ha_token_present ? s.ha_base_url : "Token saknas"),
    ),
    row(s.prices_available ? "ok" : "bad", s.prices_available ? "Spotpriser hämtas" : "Spotpriser saknas", `Elområde ${s.price_area || "?"}`),
    row(
      s.forecast_available ? "ok" : "warn",
      s.forecast_available ? "Väderprognos används" : "Ingen väderprognos",
      s.forecast_available ? s.weather_entity || "" : "Planen antar att det är lika kallt hela dygnet. Ange site.weather_entity.",
    ),
    row(s.total_power_entity ? "ok" : "warn", s.total_power_entity ? "Elmätare för hela huset" : "Ingen elmätare vald", s.total_power_entity || "Behövs för förbrukning, effekttoppar och avtalseffekten i besparingen."),
    row(s.rooms_with_climate ? "ok" : "warn", `${s.rooms_with_climate || 0} av ${s.rooms_configured || 0} rum har termostat`, s.rooms_with_climate ? "Via LK Arc Climate." : s.room_setpoint_entity ? `Styr hela huset via ${s.room_setpoint_entity}` : "Inget att styra."),
    row(s.hot_water_enabled ? (s.hot_water_setpoint_entity ? "ok" : "warn") : "warn", s.hot_water_enabled ? "Varmvatten planeras" : "Varmvatten planeras inte", s.hot_water_setpoint_entity ? `Börvärde via ${s.hot_water_setpoint_entity}` : "Inget börvärde att skriva till – planen visas men styr inte tanken."),
    row(s.mqtt_online ? "ok" : s.mqtt_configured ? "bad" : "warn", s.mqtt_online ? "MQTT ansluten" : s.mqtt_configured ? "MQTT svarar inte" : "MQTT avstängt", "Sensorerna hemopt_* i Home Assistant kommer härifrån."),
    row(
      s.power_backfill && s.power_backfill.seen ? "ok" : "warn",
      "Förbrukningshistorik från Home Assistant",
      s.power_backfill
        ? `${fmt0.format(s.power_backfill.hours)} timmar ifyllda senast, ${s.power_backfill.source === "statistics" ? "ur långtidsstatistiken" : "ur de senaste 10 dagarnas historik"}.`
        : "Hämtas när en elmätare är vald.",
    ),
    row(
      s.location_known ? "ok" : "warn",
      s.location_known ? "Solens läge räknas fram" : "Husets position okänd",
      s.location_known ? "Från Home Assistants inställningar." : "Solinstrålningen kan inte räknas ut. Ange plats i Home Assistant.",
    ),
    row(s.ext_enabled ? "ok" : "warn", s.ext_enabled ? "Effektvakten får bryta via EXT" : "Effektvakten räknar men bryter inte", s.guard_reason || ""),
  ];
  $("#systems-body").replaceChildren(...rows);
}

async function renderMeters() {
  const body = $("#meter-body");
  let payload;
  try {
    payload = await api("api/meters");
  } catch (error) {
    body.replaceChildren(el("p", { class: "empty", text: `Kunde inte läsa mätare: ${error.message}` }));
    return;
  }
  if (!payload.online) {
    body.replaceChildren(el("p", { class: "muted", text: `Home Assistant svarar inte. Vald mätare: ${payload.current || "ingen"}.` }));
    return;
  }
  const candidates = payload.candidates || [];
  const choose = async (entity) => {
    try {
      await api("api/meters/total", { method: "PUT", body: { entity_id: entity } });
      await loadStatus();
      renderMeters();
    } catch (error) {
      alert(`Kunde inte välja mätaren: ${error.message}`);
    }
  };
  const list = el(
    "div",
    { class: "meter-list" },
    ...candidates.map((c) => {
      const id = typeof c === "string" ? c : c.entity_id;
      const label = typeof c === "string" ? "" : c.name || "";
      const value = typeof c === "string" ? "" : c.state !== undefined ? ` (${c.state}${c.unit ? ` ${c.unit}` : ""})` : "";
      return el(
        "label",
        { class: "meter-option" },
        el("input", { type: "radio", name: "meter", checked: id === payload.current, onchange: () => choose(id) }),
        el("span", {}, label ? `${label} ` : "", el("code", { text: id }), value),
      );
    }),
  );
  body.replaceChildren(
    el("p", { class: "muted", text: payload.current ? `Vald: ${payload.current}` : "Ingen mätare vald." }),
    candidates.length ? list : el("p", { class: "empty", text: "Hittade inga effektsensorer som ser ut att mäta hela huset." }),
  );
}

async function renderPeakSettings() {
  try {
    const p = await api("api/settings/peaks");
    const months =
      p.months.length === 12
        ? "hela året"
        : p.months.map((m) => new Intl.DateTimeFormat("sv-SE", { month: "short" }).format(new Date(2026, m - 1, 1))).join(", ");
    const items = [
      ["Aktiverad", p.enabled ? "ja" : "nej"],
      ["Toppar som snittas", String(p.n_peaks)],
      ["Pris", `${kr(p.price_per_kw_sek)} per kW och månad`],
      ["Mäts", `${p.weekdays_only ? "vardagar " : ""}${p.hour_start}–${p.hour_end}`],
      ["Månader", months],
    ];
    $("#peak-settings").replaceChildren(...items.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  } catch (error) {
    $("#peak-settings").replaceChildren(el("dd", { text: error.message }));
  }
}

/* ----------------------------------------------------------------- loading */

async function loadStatus() {
  try {
    state.status = await api("api/status");
  } catch (error) {
    state.status = { booting: true, errors: [`Panelen når inte hemopt: ${error.message}`] };
  }
  renderMasthead();
  renderSummary();
  renderFigures();
  renderActions();
  renderErrors();
  renderFooter();
  if (currentTab() === "system") renderSystems();
}

async function loadPlan() {
  try {
    state.plan = await api("api/plan");
  } catch (error) {
    state.plan = null;
  }
  const now = nowDate();
  drawHorizon(state.plan, now);
  renderPlanNotes();
  renderFigures();
  if (currentTab() === "rooms") {
    drawDhw(state.plan, now);
    renderDhwSub();
  }
}

async function loadRooms() {
  try {
    state.rooms = await api("api/rooms");
  } catch (error) {
    state.rooms = null;
  }
  renderFigures();
  if (currentTab() === "rooms") renderRooms();
}

async function loadSavings() {
  try {
    state.savings = await api(`api/savings?days=${state.savingsDays}`);
  } catch (error) {
    state.savings = null;
  }
  renderFigures();
  if (currentTab() === "savings") renderSavings();
}

async function loadAdvice(recompute = false) {
  const button = $("#advice-btn");
  button.disabled = recompute;
  try {
    renderAdvice(await api("api/advice", recompute ? { method: "POST" } : {}));
  } catch (error) {
    $("#advice-body").replaceChildren(el("p", { class: "empty", text: `Kunde inte hämta: ${error.message}` }));
  } finally {
    button.disabled = false;
  }
}

async function loadStove() {
  try {
    renderStove(await api("api/wood-stove"));
  } catch (error) {
    $("#stove-body").replaceChildren(el("p", { class: "empty", text: error.message }));
  }
}

async function loadPrices() {
  const back = state.priceBack;
  try {
    const payload = await api(`api/prices?days_back=${back}&days_forward=1`);
    drawPrices(payload, nowDate());
    const sub = $("#price-sub");
    if (payload.current_total_sek !== null && payload.current_total_sek !== undefined) {
      sub.textContent = `Just nu ${ore(payload.current_total_sek)} öre/kWh inklusive påslag, nätavgift, skatt och moms (spot ${ore(payload.current_spot_sek)} öre).`;
    }
  } catch (error) {
    $("#price-chart").replaceChildren(el("p", { class: "empty", text: error.message }));
  }
}

async function loadHistory() {
  const { days, res } = state.history;
  try {
    const payload = await api(`api/history?days=${days}&resolution=${res}`);
    drawHistory(payload, nowDate());
    renderHistoryFacts(payload);
  } catch (error) {
    $("#history-chart").replaceChildren(el("p", { class: "empty", text: error.message }));
  }
}

async function loadPeaks() {
  try {
    renderPeaks(await api("api/peaks"));
  } catch (error) {
    $("#peak-body").replaceChildren(el("p", { class: "empty", text: error.message }));
  }
}

function nowDate() {
  return state.status && state.status.now ? new Date(state.status.now) : new Date();
}

/* -------------------------------------------------------------------- tabs */

function currentTab() {
  const active = $(".tab[aria-selected='true']");
  return active ? active.dataset.tab : "overview";
}

const TAB_LOADERS = {
  overview: () => {
    drawHorizon(state.plan, nowDate());
  },
  savings: () => {
    renderSavings();
    loadSavings();
    loadAdvice();
  },
  rooms: () => {
    renderRooms();
    loadRooms();
    drawDhw(state.plan, nowDate());
    renderDhwSub();
    loadStove();
  },
  energy: () => {
    loadPrices();
    loadHistory();
    loadPeaks();
  },
  system: () => {
    renderSystems();
    renderMeters();
    renderPeakSettings();
  },
};

function selectTab(name, { push = true } = {}) {
  if (!TAB_LOADERS[name]) name = "overview";
  for (const tab of $$(".tab")) tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
  for (const view of $$(".view")) view.hidden = view.dataset.tab !== name;
  if (push) history.replaceState(null, "", `#${name}`);
  hideTip();
  TAB_LOADERS[name]();
}

/* ------------------------------------------------------------------ events */

function bindSegmented(selector, onPick) {
  const group = $(selector);
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    for (const b of $$("button", group)) b.setAttribute("aria-pressed", b === button ? "true" : "false");
    onPick(button.dataset);
  });
}

function bind() {
  $("#tab-nav").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) selectTab(tab.dataset.tab);
  });

  $("#control-toggle").addEventListener("change", async (event) => {
    const enabled = event.target.checked;
    try {
      await api("api/control", { method: "POST", body: { enabled } });
    } catch (error) {
      event.target.checked = !enabled;
      alert(`Kunde inte ändra styrningen: ${error.message}`);
    }
    await loadStatus();
  });

  $("#replan-btn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Räknar…";
    try {
      await api("api/replan", { method: "POST" });
    } catch (error) {
      alert(`Planeringen misslyckades: ${error.message}`);
    }
    await Promise.all([loadStatus(), loadPlan(), loadRooms()]);
    button.disabled = false;
    button.textContent = "Räkna om";
  });

  $("#train-btn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Lär…";
    try {
      await api("api/train", { method: "POST" });
      await Promise.all([loadStatus(), loadRooms()]);
    } catch (error) {
      alert(`Inlärningen misslyckades: ${error.message}`);
    }
    button.disabled = false;
    button.textContent = "Lär om modellerna";
  });

  $("#advice-btn").addEventListener("click", () => loadAdvice(true));

  bindSegmented("#savings-period", ({ days }) => {
    state.savingsDays = Number(days);
    loadSavings();
  });
  bindSegmented("#price-period", ({ back }) => {
    state.priceBack = Number(back);
    loadPrices();
  });
  bindSegmented("#history-period", ({ days, res }) => {
    state.history = { days: Number(days), res };
    loadHistory();
  });

  window.addEventListener("hashchange", () => selectTab((location.hash || "#overview").slice(1), { push: false }));

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => selectTab(currentTab(), { push: false }), 150);
  });
}

async function start() {
  bind();
  const initial = (location.hash || "#overview").slice(1);
  await loadStatus();
  selectTab(initial, { push: false });
  await Promise.all([loadPlan(), loadRooms(), loadSavings()]);
  setInterval(loadStatus, REFRESH_STATUS_MS);
  setInterval(() => {
    loadPlan();
    loadRooms();
    loadSavings();
  }, REFRESH_PLAN_MS);
}

start();
