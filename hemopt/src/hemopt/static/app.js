const SVG_NS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 20000;

const state = { plan: null, status: null, peaks: null, rooms: [], meters: null };

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

  const pills = [
    ["Home Assistant", status.home_assistant_online],
    ["Spotpris", status.prices_available],
    ["Väderprognos", status.forecast_available],
    ["MQTT", status.mqtt_online],
  ];
  for (const [label, ok] of pills) {
    host.appendChild(html("span", `pill ${ok ? "ok" : "bad"}`, label));
  }
}

function renderKpis() {
  const host = document.getElementById("kpis");
  const status = state.status || {};
  const peaks = state.peaks || {};
  host.textContent = "";

  const cards = [
    {
      label: "Elpris nu",
      value: fmt(status.current_price_sek, 2),
      unit: "SEK/kWh",
      note: `Planerad effekt ${fmt(status.planned_power_kw, 2)} kW`,
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
    : "Effektavgift är avstängd i konfigurationen";

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

  if (!peaks.counted.length) {
    host.appendChild(html("p", "empty", "Inga mätta toppar denna månad ännu."));
    return;
  }

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

function renderRooms() {
  const host = document.getElementById("rooms");
  host.textContent = "";
  if (!state.rooms.length) {
    host.appendChild(html("p", "empty", "Inga rum konfigurerade."));
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
    slider.max = "5";
    slider.step = "1";
    slider.value = String(room.priority);
    const valueLabel = html("span", "priority-value", String(room.priority));
    slider.addEventListener("input", () => {
      valueLabel.textContent = slider.value;
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

  if (!meters) {
    subtitle.textContent = "Husets totala effekt — behövs för att se och kapa effekttoppar.";
    host.appendChild(html("p", "empty", "Kunde inte läsa elmätare just nu."));
    return;
  }

  if (current) {
    const match = (meters.candidates || []).find((row) => row.entity_id === current);
    const watts = match ? Number(match.value) : null;
    subtitle.textContent = watts !== null && !Number.isNaN(watts)
      ? `Nuvarande effekt ${fmt(watts, 0)} W`
      : "Kopplad till husets elmätare";
  } else {
    subtitle.textContent =
      "Ingen elmätare vald — effekttopparna ser bara värmepumpen tills du väljer en.";
  }

  const row = html("div", "meter-row");
  const select = document.createElement("select");
  select.className = "meter-select";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "Ingen elmätare";
  select.appendChild(none);

  const candidates = meters.candidates || [];
  const ids = new Set(candidates.map((row) => row.entity_id));
  if (current && !ids.has(current)) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = current;
    select.appendChild(option);
  }
  for (const candidate of candidates) {
    const option = document.createElement("option");
    option.value = candidate.entity_id;
    const watts = Number(candidate.value);
    option.textContent = Number.isNaN(watts)
      ? candidate.entity_id
      : `${candidate.entity_id} · ${fmt(watts, 0)} W`;
    select.appendChild(option);
  }
  select.value = current || "";
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      await putJSON("/api/meters/total", { entity_id: select.value || null });
      await refresh();
    } catch (error) {
      console.error(error);
    } finally {
      select.disabled = false;
    }
  });
  row.appendChild(select);

  if (!meters.online) {
    row.appendChild(html("span", "muted", "Home Assistant offline"));
  } else if (!candidates.length && !current) {
    row.appendChild(
      html(
        "span",
        "muted",
        "Ingen total-effekt-sensor hittades. HomeWizard P1 heter oftast sensor.p1_meter_active_power.",
      ),
    );
  }
  host.appendChild(row);
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
  list.textContent = "";
  card.hidden = errors.length === 0;
  for (const message of errors) list.appendChild(html("li", null, message));
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
  renderPlanChart(document.getElementById("plan-chart"), state.plan, state.status?.now);
  renderHotWater(document.getElementById("dhw-chart"), state.plan);
  renderPeaks();
  renderMeters();
  renderRooms();
  renderNotes();
  renderErrors();
}

/* ------------------------------------------------------------------ boot */

async function refresh() {
  const [status, peaks, rooms, meters] = await Promise.all([
    getJSON("/api/status").catch(() => null),
    getJSON("/api/peaks").catch(() => null),
    getJSON("/api/rooms").catch(() => []),
    getJSON("/api/meters").catch(() => null),
  ]);
  state.status = status;
  state.peaks = peaks;
  state.rooms = rooms;
  state.meters = meters;
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

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderAll, 150);
});

refresh();
setInterval(refresh, REFRESH_MS);
