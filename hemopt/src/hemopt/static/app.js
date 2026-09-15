const SVG_NS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 20000;

const state = {
  plan: null,
  status: null,
  peaks: null,
  rooms: [],
  meters: null,
  peakSettings: null,
  history: null,
  advice: null,
  prices: null,
};


/* ------------------------------------------------------------------ utils */

function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function html(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmt(value, digits = 1, fallback = "—") {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return Number(value).toLocaleString("sv-SE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function clockLabel(iso) {
  return new Date(iso).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
}

// Behind Home Assistant's ingress the panel lives under a per-session prefix,
// carried by the <base> tag the server writes into the page. Resolving against
// it keeps the same paths working both there and on a plain localhost port.
function apiUrl(path) {
  return new URL(path.replace(/^\//, ""), document.baseURI).toString();
}

async function getJSON(url) {
  const response = await fetch(apiUrl(url));
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

async function postJSON(url, body) {
  const response = await fetch(apiUrl(url), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

async function putJSON(url, body) {
  const response = await fetch(apiUrl(url), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------------ chart */

function scaleLinear(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value) => r0 + ((value - d0) / span) * (r1 - r0);
}

function linePath(points) {
  return points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ");
}

function renderPlanChart(host, plan, nowIso) {
  host.textContent = "";
  if (!plan || !plan.times.length) {
    host.appendChild(html("p", "empty", "Ingen plan att visa ännu."));
    return;
  }

  const width = host.clientWidth || 900;
  const height = host.clientHeight || 320;
  const margin = { top: 14, right: 52, bottom: 26, left: 46 };
  const innerW = Math.max(width - margin.left - margin.right, 10);
  const innerH = Math.max(height - margin.top - margin.bottom, 10);

  const times = plan.times.map((t) => new Date(t).getTime());
  const x = scaleLinear([times[0], times[times.length - 1]], [0, innerW]);

  const maxPower = Math.max(
    ...plan.total_power_kw,
    plan.peak_threshold_kw || 0,
    1,
  ) * 1.15;
  const maxPrice = Math.max(...plan.price, 0.1) * 1.15;
  const yPower = scaleLinear([0, maxPower], [innerH, 0]);
  const yPrice = scaleLinear([0, maxPrice], [innerH, 0]);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  const root = el("g", { transform: `translate(${margin.left},${margin.top})` });
  svg.appendChild(root);

  // billable-window shading
  const billable = new Set(
    (plan.hour_peaks || []).filter((h) => h.billable).map((h) => new Date(h.hour_start).getTime()),
  );
  for (const hour of billable) {
    const x0 = x(hour);
    const x1 = x(hour + 3600000);
    if (x1 < 0 || x0 > innerW) continue;
    root.appendChild(
      el("rect", {
        class: "band-peak",
        x: Math.max(x0, 0),
        y: 0,
        width: Math.max(Math.min(x1, innerW) - Math.max(x0, 0), 0),
        height: innerH,
      }),
    );
  }

  // horizontal gridlines + power axis
  const axis = el("g", { class: "axis" });
  for (let i = 0; i <= 4; i += 1) {
    const value = (maxPower / 4) * i;
    const y = yPower(value);
    root.appendChild(el("line", { class: "gridline", x1: 0, x2: innerW, y1: y, y2: y }));
    axis.appendChild(
      el("text", { x: -8, y: y + 3.5, "text-anchor": "end" }, [
        document.createTextNode(fmt(value, 1)),
      ]),
    );
  }
  axis.appendChild(
    el("text", { x: -8, y: -4, "text-anchor": "end" }, [document.createTextNode("kW")]),
  );
  axis.appendChild(
    el("text", { x: innerW + 8, y: -4, "text-anchor": "start" }, [
      document.createTextNode("SEK/kWh"),
    ]),
  );
  for (let i = 0; i <= 4; i += 1) {
    const value = (maxPrice / 4) * i;
    axis.appendChild(
      el("text", { x: innerW + 8, y: yPrice(value) + 3.5, "text-anchor": "start" }, [
        document.createTextNode(fmt(value, 1)),
      ]),
    );
  }

  // price area
  const priceArea = plan.price.map((p, i) => [x(times[i]), yPrice(p)]);
  root.appendChild(
    el("path", {
      d: `${linePath(priceArea)} L${innerW},${innerH} L0,${innerH} Z`,
      fill: "#3d5a80",
      opacity: 0.32,
    }),
  );
  root.appendChild(
    el("path", { d: linePath(priceArea), fill: "none", stroke: "#5b82b8", "stroke-width": 1.2 }),
  );

  // peak threshold
  if (plan.peak_threshold_kw > 0) {
    const y = yPower(plan.peak_threshold_kw);
    root.appendChild(
      el("line", {
        x1: 0,
        x2: innerW,
        y1: y,
        y2: y,
        stroke: "#ff5f56",
        "stroke-width": 1.6,
        "stroke-dasharray": "6 4",
      }),
    );
  }

  // Hourly means are what the peak tariff actually bills, so they get their
  // own step line: individual quarters may cross the threshold without
  // costing anything as long as the hour they belong to does not.
  const hours = plan.hour_peaks || [];
  if (hours.length) {
    const steps = [];
    for (const hour of hours) {
      const t0 = new Date(hour.hour_start).getTime();
      steps.push([x(t0), yPower(hour.mean_kw)], [x(t0 + 3600000), yPower(hour.mean_kw)]);
    }
    root.appendChild(
      el("path", {
        d: linePath(steps),
        fill: "none",
        stroke: "#a481ff",
        "stroke-width": 1.5,
        opacity: 0.85,
      }),
    );
    for (const hour of hours) {
      if (!hour.billable || hour.over_threshold_kw <= 0.01) continue;
      const t0 = new Date(hour.hour_start).getTime();
      root.appendChild(
        el("rect", {
          x: x(t0),
          y: yPower(hour.mean_kw),
          width: Math.max(x(t0 + 3600000) - x(t0), 1),
          height: Math.max(yPower(plan.peak_threshold_kw) - yPower(hour.mean_kw), 1),
          fill: "rgba(255,95,86,0.45)",
        }),
      );
    }
  }

  // total and pump power
  root.appendChild(
    el("path", {
      d: linePath(plan.total_power_kw.map((p, i) => [x(times[i]), yPower(p)])),
      fill: "none",
      stroke: "#4da3ff",
      "stroke-width": 1.9,
      "stroke-linejoin": "round",
    }),
  );
  root.appendChild(
    el("path", {
      d: linePath(plan.heat_pump_kw.map((p, i) => [x(times[i]), yPower(p)])),
      fill: "none",
      stroke: "#ff9f43",
      "stroke-width": 1.9,
      "stroke-linejoin": "round",
    }),
  );

  // time axis
  let lastLabel = -Infinity;
  for (let i = 0; i < times.length; i += 1) {
    const date = new Date(times[i]);
    if (date.getMinutes() !== 0 || date.getHours() % 3 !== 0) continue;
    const px = x(times[i]);
    if (px - lastLabel < 46) continue;
    lastLabel = px;
    axis.appendChild(
      el("text", { x: px, y: innerH + 16, "text-anchor": "middle" }, [
        document.createTextNode(String(date.getHours()).padStart(2, "0")),
      ]),
    );
  }

  // now marker
  const now = new Date(nowIso).getTime();
  if (now >= times[0] && now <= times[times.length - 1]) {
    const px = x(now);
    root.appendChild(el("line", { class: "now-line", x1: px, x2: px, y1: 0, y2: innerH }));
  }

  root.appendChild(axis);
  host.appendChild(svg);
}

function renderSparkline(host, room) {
  host.textContent = "";
  const width = host.clientWidth || 240;
  const height = host.clientHeight || 46;
  if (!room.temperature || !room.temperature.length) return;

  const values = room.temperature;
  const lo = Math.min(...values, room.comfort_min) - 0.3;
  const hi = Math.max(...values, room.comfort_max) + 0.3;
  const x = scaleLinear([0, values.length - 1], [0, width]);
  const y = scaleLinear([lo, hi], [height - 2, 2]);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });

  svg.appendChild(
    el("rect", {
      x: 0,
      y: y(room.comfort_max),
      width,
      height: Math.max(y(room.comfort_min) - y(room.comfort_max), 1),
      fill: "rgba(53,196,138,0.13)",
    }),
  );

  const points = values.map((v, i) => [x(i), y(v)]);
  svg.appendChild(
    el("path", {
      d: linePath(points),
      fill: "none",
      stroke: "#4da3ff",
      "stroke-width": 1.6,
      "stroke-linejoin": "round",
    }),
  );
  host.appendChild(svg);
}

function renderHotWater(host, plan) {
  host.textContent = "";
  if (!plan || !plan.hot_water) {
    host.appendChild(html("p", "empty", "Varmvatten är inte konfigurerat."));
    return;
  }

  const width = host.clientWidth || 420;
  const height = host.clientHeight || 210;
  const margin = { top: 12, right: 12, bottom: 24, left: 38 };
  const innerW = Math.max(width - margin.left - margin.right, 10);
  const innerH = Math.max(height - margin.top - margin.bottom, 10);

  const temps = plan.hot_water.temperature;
  const charge = plan.hot_water.charge_fraction;
  const times = plan.times.map((t) => new Date(t).getTime());
  const x = scaleLinear([times[0], times[times.length - 1]], [0, innerW]);
  const lo = Math.min(...temps) - 2;
  const hi = Math.max(...temps) + 2;
  const y = scaleLinear([lo, hi], [innerH, 0]);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  const root = el("g", { transform: `translate(${margin.left},${margin.top})` });
  svg.appendChild(root);

  const barWidth = Math.max(innerW / charge.length, 1);
  charge.forEach((value, i) => {
    if (value <= 0.02) return;
    root.appendChild(
      el("rect", {
        x: x(times[i]),
        y: innerH - value * innerH * 0.4,
        width: barWidth,
        height: value * innerH * 0.4,
        fill: "rgba(255,159,67,0.45)",
      }),
    );
  });

  const axis = el("g", { class: "axis" });
  for (let i = 0; i <= 3; i += 1) {
    const value = lo + ((hi - lo) / 3) * i;
    const py = y(value);
    root.appendChild(el("line", { class: "gridline", x1: 0, x2: innerW, y1: py, y2: py }));
    axis.appendChild(
      el("text", { x: -7, y: py + 3.5, "text-anchor": "end" }, [
        document.createTextNode(fmt(value, 0)),
      ]),
    );
  }

  root.appendChild(
    el("path", {
      d: linePath(temps.map((t, i) => [x(times[i]), y(t)])),
      fill: "none",
      stroke: "#ff5f56",
      "stroke-width": 1.9,
    }),
  );

  let lastLabel = -Infinity;
  times.forEach((t) => {
    const date = new Date(t);
    if (date.getMinutes() !== 0 || date.getHours() % 6 !== 0) return;
    const px = x(t);
    if (px - lastLabel < 40) return;
    lastLabel = px;
    axis.appendChild(
      el("text", { x: px, y: innerH + 15, "text-anchor": "middle" }, [
        document.createTextNode(`${String(date.getHours()).padStart(2, "0")}:00`),
      ]),
    );
  });

  root.appendChild(axis);
  host.appendChild(svg);
}

/* ------------------------------------------------------------------ panels */

function renderPills() {
  const host = document.getElementById("status-pills");
  const status = state.status || {};
  host.textContent = "";

  const pricesOk = Boolean(status.prices_available || state.prices?.available);
  const mqttExpected = Boolean(status.mqtt_configured);
  const weatherExpected = Boolean(status.weather_entity);

  const items = [
    ["Home Assistant", status.home_assistant_online, true],
    ["Spotpris", pricesOk, true],
    [
      weatherExpected ? "Väderprognos" : "Väder (ej satt)",
      status.forecast_available,
      weatherExpected,
    ],
    [mqttExpected ? "MQTT" : "MQTT (ej satt)", status.mqtt_online, mqttExpected],
  ];
  for (const [label, ok, expected] of items) {
    const cls = !expected ? "pill dim" : ok ? "pill ok" : "pill bad";
    host.appendChild(html("span", cls, label));
  }
}

function renderKpis() {
  const host = document.getElementById("kpis");
  const status = state.status || {};
  const peaks = state.peaks || {};
  const prices = state.prices || {};
  host.textContent = "";

  const priceNow =
    prices.current_total_sek ?? status.current_price_sek ?? prices.current_spot_sek ?? null;
  const spotNote =
    prices.current_spot_sek != null
      ? `Spot ${fmt(prices.current_spot_sek, 2)} · planerad effekt ${fmt(status.planned_power_kw, 2)} kW`
      : `Planerad effekt ${fmt(status.planned_power_kw, 2)} kW`;

  const cards = [
    {
      label: "Elpris nu",
      value: fmt(priceNow, 2),
      unit: "SEK/kWh",
      note: spotNote,
      tone: "",
    },
    {
      label: "Besparing planperiod",
      value: fmt(status.savings_sek, 0),
      unit: "SEK",
      note: `${fmt(status.total_cost_sek, 0)} mot ${fmt(status.baseline_cost_sek, 0)} SEK utan styrning`,
      tone: "good",
    },
    {
      label: "Effekttak att slå",
      value: fmt(peaks.threshold_kw, 1),
      unit: "kW",
      note:
        peaks.current_hour && peaks.current_hour.allowed_kw !== null
          ? `${fmt(peaks.current_hour.allowed_kw, 1)} kW kvar denna timme`
          : "Utanför effektfönstret",
      tone: "warm",
    },
    {
      label: "Prognos effektavgift",
      value: fmt(peaks.projected_cost_sek, 0),
      unit: "SEK/mån",
      note: `Snitt av ${peaks.n_peaks || 5} toppar: ${fmt(peaks.average_kw, 1)} kW`,
      tone: "violet",
    },
  ];

  for (const card of cards) {
    const node = html("div", `kpi ${card.tone}`);
    node.appendChild(html("span", "kpi-label", card.label));
    const value = html("span", "kpi-value", card.value);
    value.appendChild(html("small", null, card.unit));
    node.appendChild(value);
    node.appendChild(html("span", "kpi-note", card.note));
    host.appendChild(node);
  }
}

function renderPeaks() {
  const host = document.getElementById("peak-body");
  const peaks = state.peaks;
  host.textContent = "";
  if (!peaks) return;

  const subtitle = document.getElementById("peak-subtitle");
  const w = peaks.window || {};
  subtitle.textContent = peaks.enabled
    ? `Snitt av ${peaks.n_peaks} toppar på olika dygn, ${String(w.hour_start).padStart(2, "0")}–${String(w.hour_end).padStart(2, "0")} vardagar`
    : "Effektavgift är avstängd — aktivera under Configuration";

  const summary = html("div", "peak-summary");
  const figures = [
    [`${fmt(peaks.average_kw, 2)} kW`, "Månadens snitt"],
    [`${fmt(peaks.threshold_kw, 2)} kW`, "Tröskel att slå"],
    [`${fmt(peaks.marginal_sek_per_kw, 0)} kr/kW`, "Marginalkostnad"],
  ];
  for (const [value, label] of figures) {
    const figure = html("div", "peak-figure");
    figure.appendChild(html("div", "value", value));
    figure.appendChild(html("div", "label", label));
    summary.appendChild(figure);
  }
  host.appendChild(summary);

  if (peaks.current_hour) {
    const hour = peaks.current_hour;
    const box = html("div", "peak-now");
    box.appendChild(
      html(
        "div",
        "peak-now-title",
        `Pågående timme · ${fmt(hour.energy_kwh, 2)} kWh hittills`,
      ),
    );
    box.appendChild(
      html(
        "div",
        "muted",
        hour.allowed_kw === null
          ? "Utanför effektfönstret just nu"
          : `${fmt(hour.allowed_kw, 2)} kW kvar innan tröskeln · ${fmt(hour.minutes_remaining, 0)} min kvar`,
      ),
    );
    host.appendChild(box);
  }

  if (!peaks.counted.length) {
    host.appendChild(html("p", "empty", "Inga mätta toppar denna månad ännu."));
  } else {
    const max = Math.max(...peaks.counted.map((p) => p.kw), 1);
    const table = html("table");
    const thead = html("thead");
    const headRow = html("tr");
    for (const label of ["Dygn", "Timme", "Effekt", ""]) {
      headRow.appendChild(html("th", null, label));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = html("tbody");
    for (const peak of peaks.counted) {
      const row = html("tr");
      row.appendChild(html("td", null, peak.day));
      row.appendChild(html("td", null, `${String(peak.hour).padStart(2, "0")}:00`));
      row.appendChild(html("td", null, `${fmt(peak.kw, 2)} kW`));
      const barCell = html("td", "bar-cell");
      const bar = html("div", "bar");
      bar.style.width = `${(peak.kw / max) * 100}%`;
      barCell.appendChild(bar);
      row.appendChild(barCell);
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    host.appendChild(table);
  }

  if (peaks.history && peaks.history.length) {
    host.appendChild(html("h3", "subhead", "Tidigare månader"));
    const hist = html("table");
    const head = html("tr");
    for (const label of ["Månad", "Snitt", "Tröskel", "Kostnad"]) {
      head.appendChild(html("th", null, label));
    }
    const thead = html("thead");
    thead.appendChild(head);
    hist.appendChild(thead);
    const tbody = html("tbody");
    for (const row of peaks.history) {
      const tr = html("tr");
      tr.appendChild(html("td", null, row.month));
      tr.appendChild(html("td", null, `${fmt(row.average_kw, 2)} kW`));
      tr.appendChild(html("td", null, `${fmt(row.threshold_kw, 2)} kW`));
      tr.appendChild(html("td", null, `${fmt(row.cost_sek, 0)} kr`));
      tbody.appendChild(tr);
    }
    hist.appendChild(tbody);
    host.appendChild(hist);
  }
}

const MONTH_LABELS = [
  "",
  "jan",
  "feb",
  "mar",
  "apr",
  "maj",
  "jun",
  "jul",
  "aug",
  "sep",
  "okt",
  "nov",
  "dec",
];

function renderPriceChart() {
  const host = document.getElementById("price-chart");
  const subtitle = document.getElementById("price-subtitle");
  host.textContent = "";
  const prices = state.prices;
  if (!prices || !prices.points || !prices.points.length) {
    subtitle.textContent = "Spotpris igår, idag och imorgon — dra i grafen för exakt pris";
    host.appendChild(
      html(
        "p",
        "empty",
        prices && prices.available === false
          ? "Kunde inte hämta spotpris just nu."
          : "Hämtar elpris…",
      ),
    );
    return;
  }

  const current =
    prices.current_total_sek != null
      ? `${fmt(prices.current_total_sek, 2)} SEK/kWh (spot ${fmt(prices.current_spot_sek, 2)})`
      : "—";
  subtitle.textContent = `${prices.area} · nu ${current} · dra eller klicka i grafen`;

  const points = prices.points;
  const width = host.clientWidth || 900;
  const height = Math.max(host.clientHeight || 280, 260);
  const margin = { top: 14, right: 18, bottom: 28, left: 46 };
  const innerW = Math.max(width - margin.left - margin.right, 10);
  const innerH = Math.max(height - margin.top - margin.bottom, 10);
  const times = points.map((p) => new Date(p.t).getTime());
  const maxPrice = Math.max(...points.map((p) => Math.max(p.total, p.spot)), 0.05) * 1.12;
  const x = scaleLinear([times[0], times[times.length - 1]], [0, innerW]);
  const xInv = scaleLinear([0, innerW], [times[0], times[times.length - 1]]);
  const y = scaleLinear([0, maxPrice], [innerH, 0]);
  const nowMs = prices.now ? new Date(prices.now).getTime() : Date.now();

  const wrap = html("div", "price-chart-wrap");
  const readout = html("div", "price-readout");
  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    class: "price-svg",
  });
  const root = el("g", { transform: `translate(${margin.left},${margin.top})` });
  svg.appendChild(root);

  for (let i = 0; i <= 4; i += 1) {
    const value = (maxPrice / 4) * i;
    const yy = y(value);
    root.appendChild(el("line", { class: "gridline", x1: 0, x2: innerW, y1: yy, y2: yy }));
    root.appendChild(
      el("text", { x: -8, y: yy + 3.5, "text-anchor": "end", class: "axis" }, [
        document.createTextNode(fmt(value, 2)),
      ]),
    );
  }

  const tomorrow = points.find((p) => {
    const day = p.day || p.t.slice(0, 10);
    const today = (prices.now || "").slice(0, 10);
    return day > today;
  });
  if (tomorrow) {
    const x0 = x(new Date(tomorrow.t).getTime());
    root.appendChild(
      el("rect", {
        class: "band-peak",
        x: x0,
        y: 0,
        width: Math.max(innerW - x0, 0),
        height: innerH,
        opacity: "0.35",
      }),
    );
  }

  const spotPath = points.map((p, i) => [x(times[i]), y(p.spot)]);
  const totalPath = points.map((p, i) => [x(times[i]), y(p.total)]);
  root.appendChild(
    el("path", {
      d: `${linePath(totalPath)} L${innerW},${innerH} L0,${innerH} Z`,
      fill: "#3d5a80",
      opacity: 0.28,
    }),
  );
  root.appendChild(
    el("path", { d: linePath(totalPath), fill: "none", stroke: "#5b82b8", "stroke-width": 1.6 }),
  );
  root.appendChild(
    el("path", {
      d: linePath(spotPath),
      fill: "none",
      stroke: "#9ec1ff",
      "stroke-width": 1.2,
      "stroke-dasharray": "4 3",
    }),
  );

  if (nowMs >= times[0] && nowMs <= times[times.length - 1]) {
    root.appendChild(
      el("line", {
        x1: x(nowMs),
        x2: x(nowMs),
        y1: 0,
        y2: innerH,
        stroke: "#ff9f43",
        "stroke-width": 1.2,
        "stroke-dasharray": "3 3",
      }),
    );
  }

  const cursor = el("g", { class: "price-cursor", style: "display:none" });
  const cursorLine = el("line", {
    y1: 0,
    y2: innerH,
    stroke: "#e8eef8",
    "stroke-width": 1,
    "stroke-dasharray": "4 3",
  });
  const cursorSpot = el("circle", { r: 4, fill: "#9ec1ff", stroke: "#0b0f16", "stroke-width": 1 });
  const cursorTotal = el("circle", { r: 5, fill: "#5b82b8", stroke: "#0b0f16", "stroke-width": 1 });
  cursor.appendChild(cursorLine);
  cursor.appendChild(cursorSpot);
  cursor.appendChild(cursorTotal);
  root.appendChild(cursor);

  const hit = el("rect", {
    x: 0,
    y: 0,
    width: innerW,
    height: innerH,
    fill: "transparent",
    style: "cursor: crosshair",
  });
  root.appendChild(hit);

  function nearestIndex(ms) {
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < times.length; i += 1) {
      const dist = Math.abs(times[i] - ms);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    return best;
  }

  function showAt(index) {
    const point = points[index];
    const px = x(times[index]);
    cursor.setAttribute("style", "display:block");
    cursorLine.setAttribute("x1", String(px));
    cursorLine.setAttribute("x2", String(px));
    cursorSpot.setAttribute("cx", String(px));
    cursorSpot.setAttribute("cy", String(y(point.spot)));
    cursorTotal.setAttribute("cx", String(px));
    cursorTotal.setAttribute("cy", String(y(point.total)));

    const when = new Date(point.t).toLocaleString("sv-SE", {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    const oreTotal = Math.round(point.total * 100);
    const oreSpot = Math.round(point.spot * 100);
    readout.textContent = "";
    readout.appendChild(html("strong", null, when));
    readout.appendChild(
      html(
        "span",
        null,
        `Totalt ${fmt(point.total, 3)} SEK/kWh (${oreTotal} öre) · Spot ${fmt(point.spot, 3)} SEK/kWh (${oreSpot} öre)`,
      ),
    );
  }

  function pointerToIndex(event) {
    const rect = svg.getBoundingClientRect();
    const clientX = event.clientX ?? event.touches?.[0]?.clientX;
    if (clientX == null) return null;
    const localX = ((clientX - rect.left) / rect.width) * width - margin.left;
    const clamped = Math.min(Math.max(localX, 0), innerW);
    return nearestIndex(xInv(clamped));
  }

  let locked = null;
  function onMove(event) {
    if (locked !== null) return;
    const index = pointerToIndex(event);
    if (index == null) return;
    event.preventDefault();
    showAt(index);
  }
  function onDown(event) {
    const index = pointerToIndex(event);
    if (index == null) return;
    event.preventDefault();
    locked = index;
    showAt(index);
  }
  function onUp() {
    locked = null;
  }

  hit.addEventListener("mousemove", onMove);
  hit.addEventListener("touchmove", onMove, { passive: false });
  hit.addEventListener("mousedown", onDown);
  hit.addEventListener("touchstart", onDown, { passive: false });
  window.addEventListener("mouseup", onUp);
  window.addEventListener("touchend", onUp);
  hit.addEventListener("mouseleave", () => {
    if (locked !== null) return;
    cursor.setAttribute("style", "display:none");
    readout.textContent = "Dra eller klicka i grafen för exakt tid och pris.";
  });

  // Default: current (or nearest) slot.
  showAt(nearestIndex(nowMs));
  if (!readout.textContent) {
    readout.textContent = "Dra eller klicka i grafen för exakt tid och pris.";
  }

  wrap.appendChild(readout);
  wrap.appendChild(svg);
  host.appendChild(wrap);
}

let peakSettingsFingerprint = "";

function renderPeakSettings() {
  const form = document.getElementById("peak-settings");
  const settings = state.peakSettings;
  if (!settings) {
    form.textContent = "";
    form.appendChild(html("p", "empty", "Kunde inte läsa reglerna."));
    peakSettingsFingerprint = "";
    return;
  }
  const fingerprint = JSON.stringify(settings);
  if (fingerprint === peakSettingsFingerprint && form.childElementCount) return;
  peakSettingsFingerprint = fingerprint;
  form.textContent = "";

  form.appendChild(
    html(
      "p",
      "muted",
      "Ändra under Settings → Add-ons → Kostnadsoptimering → Configuration. " +
        "Månader med effektavgift sätter du i /homeassistant/hemopt.yaml.",
    ),
  );

  const grid = html("div", "settings-grid readonly-grid");
  const rows = [
    ["Minimera toppar", settings.enabled ? "På" : "Av"],
    ["Antal toppar", String(settings.n_peaks)],
    ["Pris per kW", `${fmt(settings.price_per_kw_sek, 1)} kr`],
    [
      "Fönster",
      `${String(settings.hour_start).padStart(2, "0")}–${String(settings.hour_end).padStart(2, "0")}`,
    ],
    ["Bara vardagar", settings.weekdays_only ? "Ja" : "Nej"],
    [
      "Månader",
      (settings.months || []).map((m) => MONTH_LABELS[m] || m).join(", ") || "—",
    ],
    ["Marginalkostnad", `${fmt(settings.marginal_sek_per_kw, 0)} kr/kW`],
  ];
  for (const [label, value] of rows) {
    const cell = html("div", "readonly-field");
    cell.appendChild(html("span", "label", label));
    cell.appendChild(html("span", "value", value));
    grid.appendChild(cell);
  }
  form.appendChild(grid);
}

function renderHistory() {
  const host = document.getElementById("history-chart");
  const summary = document.getElementById("history-summary");
  const subtitle = document.getElementById("history-subtitle");
  host.textContent = "";
  summary.textContent = "";
  const history = state.history;
  if (!history) {
    host.appendChild(html("p", "empty", "Ingen historik ännu."));
    return;
  }

  subtitle.textContent = `Senaste ${history.days} dygnen`;
  const points = history.points || [];
  if (!points.length) {
    host.appendChild(
      html("p", "empty", "Ingen timdata sparad ännu — den fylls på när elmätaren är kopplad."),
    );
  } else {
    renderPowerHistory(host, points);
  }

  const cards = html("div", "history-kpis");
  const items = [
    ["Planerad besparing", history.savings_sek, "kr mot utan styrning"],
    ["Kostnad med styrning", history.optimised_cost_sek, "kr i planperioden"],
    ["Kostnad utan styrning", history.baseline_cost_sek, "kr i planperioden"],
  ];
  for (const [label, value, note] of items) {
    const card = html("div", "history-kpi");
    card.appendChild(html("div", "label", label));
    card.appendChild(html("div", "value", value == null ? "—" : `${fmt(value, 0)} kr`));
    card.appendChild(html("div", "muted", note));
    cards.appendChild(card);
  }
  summary.appendChild(cards);
}

function renderPowerHistory(host, points) {
  const width = host.clientWidth || 480;
  const height = 180;
  const pad = { top: 12, right: 12, bottom: 24, left: 36 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const times = points.map((p) => new Date(p.t).getTime());
  const values = points.map((p) => p.kw);
  const max = Math.max(...values, 1);
  const x = scaleLinear([times[0], times[times.length - 1]], [0, innerW]);
  const y = scaleLinear([0, max], [innerH, 0]);

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height: String(height) });
  const g = el("g", { transform: `translate(${pad.left},${pad.top})` });
  for (let i = 0; i <= 4; i += 1) {
    const value = (max / 4) * i;
    const yy = y(value);
    g.appendChild(
      el("line", {
        x1: 0,
        x2: innerW,
        y1: yy,
        y2: yy,
        stroke: "currentColor",
        "stroke-opacity": "0.12",
      }),
    );
  }
  g.appendChild(
    el("path", {
      d: linePath(points.map((p, i) => [x(times[i]), y(p.kw)])),
      fill: "none",
      stroke: "#4da3ff",
      "stroke-width": "1.5",
    }),
  );
  svg.appendChild(g);
  host.appendChild(svg);
}

function renderAdvice() {
  const host = document.getElementById("advice-body");
  const subtitle = document.getElementById("advice-subtitle");
  host.textContent = "";
  const advice = state.advice;
  if (!advice) {
    host.appendChild(html("p", "empty", "Ingen avtalsdata ännu."));
    return;
  }

  if (advice.notes && advice.notes.length) {
    subtitle.textContent = advice.notes[0];
  } else {
    subtitle.textContent = `Baserat på ${fmt(advice.measured_days, 0)} dygns uppmätt förbrukning`;
  }

  if (advice.contract_costs && advice.contract_costs.length) {
    const table = html("table");
    const head = html("tr");
    for (const label of ["Avtal", "Kostnad", "Öre/kWh", "kWh"]) {
      head.appendChild(html("th", null, label));
    }
    const thead = html("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = html("tbody");
    const cheapest = Math.min(...advice.contract_costs.map((c) => c.total_sek));
    for (const cost of advice.contract_costs) {
      const row = html("tr", cost.total_sek === cheapest ? "best-row" : null);
      row.appendChild(html("td", null, cost.name));
      row.appendChild(html("td", null, `${fmt(cost.total_sek, 0)} kr`));
      row.appendChild(html("td", null, fmt(cost.ore_per_kwh, 1)));
      row.appendChild(html("td", null, fmt(cost.kwh, 0)));
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    host.appendChild(table);
  }

  if (advice.recommendations && advice.recommendations.length) {
    const list = html("div", "advice-list");
    for (const rec of advice.recommendations) {
      const card = html("div", "advice-card");
      card.appendChild(html("div", "advice-title", rec.title));
      card.appendChild(html("div", "advice-detail", rec.detail));
      card.appendChild(
        html("div", "advice-saving", `≈ ${fmt(rec.annual_saving_sek, 0)} kr/år · ${rec.confidence}`),
      );
      if (rec.caveat) card.appendChild(html("div", "muted", rec.caveat));
      list.appendChild(card);
    }
    host.appendChild(list);
  } else if (!advice.contract_costs.length) {
    host.appendChild(
      html(
        "p",
        "empty",
        "Behöver mer mätdata innan avtalsjämförelsen blir meningsfull (minst två dygn).",
      ),
    );
  }
}

function renderRooms() {
  const host = document.getElementById("rooms");
  host.textContent = "";
  if (!state.rooms.length) {
    const box = html("div", "empty-stack");
    box.appendChild(html("p", "empty", "Inga rum konfigurerade."));
    box.appendChild(
      html(
        "p",
        "muted",
        "Rum läggs inte till i den här panelen. Skapa filen hemopt.yaml i Home Assistants " +
          "config-mapp (samma plats som configuration.yaml).",
      ),
    );
    const steps = html("ol", "setup-steps");
    for (const text of [
      "Settings → Add-ons → File editor (eller Studio Code Server) → Install/Start",
      "Öppna /config/ (roten där configuration.yaml ligger)",
      "Skapa ny fil: hemopt.yaml",
      "Kopiera rum-delen från config.exempel.yaml i GitHub-repot danebananee/hemopt och byt till dina temperature_entity / climate_entity",
      "Settings → Add-ons → Kostnadsoptimering → Restart",
    ]) {
      steps.appendChild(html("li", null, text));
    }
    box.appendChild(steps);
    box.appendChild(
      html(
        "p",
        "muted",
        "Prioritet här: 3 = håll temperaturen, 1 = får svaja och bär lastflytten. " +
          "Utan rum blir det ingen värmeplan.",
      ),
    );
    host.appendChild(box);
    return;
  }

  const planRooms = new Map((state.plan?.rooms || []).map((r) => [r.key, r]));
  const index = currentIndex();

  for (const room of state.rooms) {
    const planned = planRooms.get(room.key);
    const card = html("div", "room");

    const head = html("div", "room-head");
    const title = html("div");
    title.appendChild(html("div", "room-name", room.name));
    if (room.floor) title.appendChild(html("div", "room-floor", room.floor));
    head.appendChild(title);
    head.appendChild(
      html("div", "room-temp", planned ? `${fmt(planned.setpoint[index], 1)} °C` : "—"),
    );
    card.appendChild(head);

    const spark = html("div", "room-spark");
    card.appendChild(spark);

    const meta = html("div", "room-meta");
    meta.appendChild(html("span", null, `Komfort ${room.comfort_min}–${room.comfort_max} °C`));
    meta.appendChild(html("span", null, `Tröghet ${fmt(room.model.tau_hours, 0)} h`));
    const badge = html(
      "span",
      `badge ${room.model.fitted ? "fitted" : ""}`,
      room.model.fitted ? `R² ${fmt(room.model.r_squared, 2)}` : "Standardmodell",
    );
    meta.appendChild(badge);
    card.appendChild(meta);

    const priorityRow = html("div", "priority-row");
    priorityRow.appendChild(html("label", null, "Prioritet"));
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = "3";
    slider.step = "1";
    slider.value = String(Math.min(3, Math.max(1, room.priority)));
    const labels = { 1: "1 · får svaja", 2: "2 · mellan", 3: "3 · håll temp" };
    const valueLabel = html(
      "span",
      "priority-value",
      labels[slider.value] || String(room.priority),
    );
    slider.addEventListener("input", () => {
      valueLabel.textContent = labels[slider.value] || slider.value;
    });
    slider.addEventListener("change", async () => {
      slider.disabled = true;
      try {
        await postJSON(`/api/rooms/${room.key}/priority`, { priority: Number(slider.value) });
        await refresh();
      } finally {
        slider.disabled = false;
      }
    });
    priorityRow.appendChild(slider);
    priorityRow.appendChild(valueLabel);
    card.appendChild(priorityRow);

    host.appendChild(card);
    if (planned) renderSparkline(spark, planned);
  }
}

function currentIndex() {
  const plan = state.plan;
  if (!plan || !plan.times.length) return 0;
  const now = Date.now();
  const step = plan.step_minutes * 60000;
  for (let i = 0; i < plan.times.length; i += 1) {
    const start = new Date(plan.times[i]).getTime();
    if (now >= start && now < start + step) return i;
  }
  return 0;
}

function renderMeters() {
  const host = document.getElementById("meter-body");
  const subtitle = document.getElementById("meter-subtitle");
  host.textContent = "";

  const meters = state.meters;
  const current = meters?.current || state.status?.total_power_entity || null;

  if (state.status?.booting) {
    subtitle.textContent = "Startar upp…";
    host.appendChild(html("p", "empty", "Väntar på att tillägget ska bli klart."));
    return;
  }

  subtitle.textContent = "Sätts under Configuration (total_power_entity) — inte här.";

  const grid = html("div", "settings-grid readonly-grid");
  const cell = html("div", "readonly-field");
  cell.appendChild(html("span", "label", "Elmätare"));
  cell.appendChild(html("span", "value", current || "Ingen vald"));
  grid.appendChild(cell);
  host.appendChild(grid);

  if (!current) {
    host.appendChild(
      html(
        "p",
        "muted",
        "Skriv t.ex. sensor.p1_meter_active_power under Configuration → Elmätare, spara och starta om.",
      ),
    );
  }

  if (meters && !meters.online) {
    host.appendChild(
      html(
        "p",
        "muted",
        "Home Assistant-API otillgängligt just nu — entitetsvärden syns när tillägget får kontakt.",
      ),
    );
  } else if (meters?.candidates?.length && current) {
    const match = meters.candidates.find((row) => row.entity_id === current);
    const watts = match ? Number(match.value) : null;
    if (watts !== null && !Number.isNaN(watts)) {
      host.appendChild(html("p", "muted", `Nuvarande effekt ${fmt(watts, 0)} W`));
    }
  }
}

function renderNotes() {
  const host = document.getElementById("plan-notes");
  host.textContent = "";
  if (state.status?.booting) {
    host.appendChild(
      html(
        "div",
        "note",
        "Tillägget startar — laddar beräkningsmotor. Panelen svarar redan, " +
          "men planen kommer när uppstarten är klar.",
      ),
    );
  } else if (!state.status?.home_assistant_online) {
    const diag = state.status?.ha_diagnosis || {};
    const detail = [
      diag.status_code != null ? `HTTP ${diag.status_code}` : null,
      diag.token_present === false ? "ingen SUPERVISOR_TOKEN" : null,
      diag.error ? String(diag.error).slice(0, 120) : null,
    ]
      .filter(Boolean)
      .join(" · ");
    host.appendChild(
      html(
        "div",
        "note warn",
        "Tillägget når inte Home Assistant Core-API (det är därför HA/väder/MQTT är röda). " +
          "Loggen visar troligen SUPERVISOR_TOKEN length=0. " +
          "Sätt en Long-lived access token under Configuration → HA-token " +
          "(URL http://homeassistant:8123), eller installera om tillägget. " +
          (detail ? `Senaste probesvar: ${detail}.` : ""),
      ),
    );
  } else if (!(state.status?.rooms_configured > 0) && !(state.rooms || []).length) {
    host.appendChild(
      html(
        "div",
        "note warn",
        "Inga rum i konfigurationen. Lägg hemopt.yaml bredvid configuration.yaml " +
          "(se config.exempel.yaml) och starta om. Spotpriset fungerar ändå.",
      ),
    );
  } else if (state.status?.starting) {
    host.appendChild(
      html(
        "div",
        "note",
        "Samlar in historik och räknar fram den första planen. " +
          "På en Raspberry Pi tar det några minuter första gången.",
      ),
    );
  }
  for (const note of state.plan?.notes || []) {
    host.appendChild(html("div", "note", note));
  }
}

function renderErrors() {
  const card = document.getElementById("error-card");
  const list = document.getElementById("errors");
  const errors = state.status?.errors || [];
  const diag = state.status?.ha_diagnosis;
  list.textContent = "";

  if (diag && !state.status?.home_assistant_online) {
    const parts = [
      `HA-diagnos: ${diag.base_url || "—"}`,
      diag.token_present ? `token ${diag.token_length} tecken` : "token saknas",
      diag.status_code != null ? `HTTP ${diag.status_code}` : null,
      diag.error || diag.message || null,
    ].filter(Boolean);
    list.appendChild(html("li", null, parts.join(" · ")));
  }

  for (const message of errors) list.appendChild(html("li", null, message));
  card.hidden = list.childElementCount === 0;
}

function renderChrome() {
  const status = state.status || {};
  document.getElementById("price-area").textContent = status.price_area || "SE3";
  document.getElementById("control-toggle").checked = Boolean(status.control_enabled);

  const subtitle = document.getElementById("plan-subtitle");
  if (state.plan) {
    subtitle.textContent =
      `${fmt(status.horizon_hours, 0)} timmar framåt · löst på ${fmt(status.solve_seconds, 2)} s` +
      ` · energikostnad ${fmt(status.energy_cost_sek, 0)} kr, effektavgift ${fmt(status.peak_cost_sek, 0)} kr`;
  }

  const dhwSubtitle = document.getElementById("dhw-subtitle");
  dhwSubtitle.textContent = `Beräknad förbrukning ${fmt(status.hot_water_kwh_per_day, 1)} kWh/dygn`;

  document.getElementById("footer-status").textContent = status.last_plan
    ? `Senaste plan ${clockLabel(status.last_plan)} · senaste mätning ${
        status.last_sample ? clockLabel(status.last_sample) : "—"
      }`
    : "Väntar på första planen";
}

function renderAll() {
  renderPills();
  renderKpis();
  renderChrome();
  renderPriceChart();
  renderPlanChart(document.getElementById("plan-chart"), state.plan, state.status?.now);
  renderHotWater(document.getElementById("dhw-chart"), state.plan);
  renderPeaks();
  renderPeakSettings();
  renderHistory();
  renderAdvice();
  renderMeters();
  renderRooms();
  renderNotes();
  renderErrors();
}

/* ------------------------------------------------------------------ boot */

async function refresh() {
  const [status, peaks, rooms, meters, peakSettings, history, advice, prices] =
    await Promise.all([
      getJSON("/api/status").catch(() => null),
      getJSON("/api/peaks").catch(() => null),
      getJSON("/api/rooms").catch(() => []),
      getJSON("/api/meters").catch(() => null),
      getJSON("/api/settings/peaks").catch(() => null),
      getJSON("/api/history?days=14").catch(() => null),
      getJSON("/api/advice").catch(() => null),
      getJSON("/api/prices").catch(() => null),
    ]);
  state.status = status;
  state.peaks = peaks;
  state.rooms = rooms;
  state.meters = meters;
  state.peakSettings = peakSettings;
  state.history = history;
  state.advice = advice;
  state.prices = prices;
  state.plan = await getJSON("/api/plan").catch(() => null);
  renderAll();
}

document.getElementById("replan-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Planerar…";
  try {
    await postJSON("/api/replan");
    await refresh();
  } catch (error) {
    console.error(error);
  } finally {
    button.disabled = false;
    button.textContent = "Planera om";
  }
});

document.getElementById("control-toggle").addEventListener("change", async (event) => {
  await postJSON("/api/control", { enabled: event.currentTarget.checked });
  await refresh();
});

document.getElementById("advice-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Räknar…";
  try {
    state.advice = await postJSON("/api/advice");
    renderAdvice();
  } catch (error) {
    console.error(error);
  } finally {
    button.disabled = false;
    button.textContent = "Räkna om";
  }
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderAll, 150);
});

refresh();
setInterval(refresh, REFRESH_MS);
