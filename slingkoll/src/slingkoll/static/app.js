"use strict";

// Slingkoll panel. Relative URLs throughout: Home Assistant serves the panel
// under an ingress path.

const $ = (id) => document.getElementById(id);
const state = { status: null, found: null, series: null, editing: false, busy: false };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
    init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Fel ${response.status}`);
  return data;
}

function toast(text) {
  const node = $("toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (node.hidden = true), 5000);
}

const fmtTemp = (value) => (value === null || value === undefined ? "–" : `${value.toFixed(1)} °C`);

function fmtWhen(epoch) {
  return new Date(epoch * 1000).toLocaleString("sv-SE", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtDuration(seconds) {
  const hours = Math.round(seconds / 3600);
  if (hours < 1) return "under en timme";
  if (hours < 36) return `${hours} timmar`;
  return `${(hours / 24).toFixed(1).replace(".", ",")} dygn`;
}

// --- status ------------------------------------------------------------------

async function refresh() {
  try {
    state.status = await api("api/status");
  } catch (error) {
    $("error-banner").textContent = `Kan inte nå Slingkoll: ${error.message}`;
    $("error-banner").hidden = false;
    return;
  }
  render();
  const run = state.status.run;
  if (run && ["running", "paused"].includes(run.status)) {
    const due = !state.seriesAt || Date.now() - state.seriesAt > (state.status.demo ? 3000 : 60000);
    if (due) {
      state.seriesAt = Date.now();
      state.series = await api("api/series").catch(() => state.series);
      renderLive();
    }
  }
}

function render() {
  const s = state.status;
  const run = s.run;
  const active = run && ["running", "paused"].includes(run.status);

  $("error-banner").hidden = !s.error;
  $("error-banner").textContent = s.error ? `Home Assistant: ${s.error}` : "";
  $("demo-banner").hidden = !s.demo;
  $("foot").textContent = `Slingkoll ${s.version}`;

  const pill = $("status-pill");
  if (active && run.status === "paused") {
    pill.textContent = "Pausat";
    pill.className = "pill paused";
  } else if (active) {
    pill.textContent = "Testet pågår";
    pill.className = "pill running";
  } else if (run && run.result) {
    pill.textContent = "Klart";
    pill.className = "pill done";
  } else {
    pill.textContent = "Redo";
    pill.className = "pill";
  }

  $("running").hidden = !active;
  $("setup").hidden = Boolean(active);
  if (active) renderRun(run);
  renderResult(run, active);
  renderSetupSummary();
  renderHow();

  const events = run ? run.events || [] : [];
  $("log-card").hidden = events.length === 0;
  $("log").replaceChildren(
    ...events.map((event) =>
      el("li", {}, el("time", { text: fmtWhen(event.t) }), event.text),
    ),
  );
}

// --- running test --------------------------------------------------------------

function renderRun(run) {
  const phase = Math.min(run.phase + 1, run.phases);
  $("run-title").textContent = run.status === "paused" ? "Testet är pausat" : "Testet pågår";
  const eta = state.status.now + run.remaining_s;
  $("run-sub").textContent =
    `Fas ${phase} av ${run.phases} (${run.phase_hours} h per fas). ` +
    `Klart ungefär ${fmtWhen(eta)}, om ${fmtDuration(run.remaining_s)}. ` +
    "Testet avslutas tidigare om svaret blir säkert, eller förlängs om det behövs.";
  $("progress-bar").style.width = `${Math.round(run.progress * 100)}%`;
  const pause = $("pause-banner");
  pause.hidden = !run.pause;
  if (run.pause) {
    pause.textContent =
      `${run.pause.reason}. Termostaterna går som vanligt tills rummet är tillbaka ` +
      "inom gränserna, sedan fortsätter testet.";
  }
  renderLive();
}

function renderLive() {
  const s = state.status;
  const run = s && s.run;
  if (!run || !["running", "paused"].includes(run.status)) return;
  const points = (state.series && state.series.points) || [];
  const places = [
    ...run.thermostats.map((t) => ({ ...t, thermostat: true })),
    ...run.extra.map((t) => ({ ...t, thermostat: false })),
  ];
  const cards = places.map((place) => {
    const live = s.live[place.entity_id] || {};
    const command = run.commands[place.entity_id];
    let stateText = place.thermostat ? "Går som vanligt" : "Bara givare";
    let on = false;
    if (place.thermostat && command !== undefined) {
      on = command === 1;
      stateText = on ? "Slingan öppen" : "Slingan stängd";
      if (place.entity_id in run.overrides) stateText += " (skydd)";
    }
    return el(
      "div",
      { class: "room" },
      el(
        "div",
        { class: "room-top" },
        el("span", { class: "room-name", text: place.name, title: place.entity_id }),
        el("span", { class: "room-temp", text: fmtTemp(live.temp) }),
      ),
      el("span", { class: `room-state${on ? " on" : ""}`, text: stateText }),
      spark(points, place.entity_id, place.thermostat),
    );
  });
  $("live-rooms").replaceChildren(...cards);
}

function spark(points, key, thermostat) {
  const width = 200;
  const height = 56;
  const node = svg("svg", { class: "spark", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  const rows = points.filter((p) => p.temps[key] !== null && p.temps[key] !== undefined);
  if (rows.length < 2) return node;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const values = rows.map((p) => p.temps[key]);
  const settings = state.status.settings;
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (hi - lo < 1.5) {
    const mid = (hi + lo) / 2;
    lo = mid - 0.75;
    hi = mid + 0.75;
  }
  const x = (t) => ((t - t0) / span) * width;
  const y = (v) => height - 3 - ((v - lo) / (hi - lo)) * (height - 6);

  if (thermostat) {
    let start = null;
    points.forEach((p, index) => {
      const on = p.cmd && p.cmd[key] === 1;
      if (on && start === null) start = p.t;
      const last = index === points.length - 1;
      if (start !== null && (!on || last)) {
        node.append(svg("rect", { class: "band", x: x(start), y: 0, width: Math.max(1, x(p.t) - x(start)), height }));
        start = null;
      }
    });
  }
  for (const limit of [settings.min_temp, settings.max_temp]) {
    if (limit > lo && limit < hi) {
      node.append(svg("line", { class: "limit", x1: 0, x2: width, y1: y(limit), y2: y(limit) }));
    }
  }
  const path = rows.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.temps[key]).toFixed(1)}`).join("");
  node.append(svg("path", { class: "line", d: path }));
  return node;
}

// --- results -------------------------------------------------------------------

function renderResult(run, active) {
  const result = run && run.result;
  const show = Boolean(
    result && (active ? result.hours >= run.phase_hours * 6 : true) && run.status !== "aborted",
  );
  $("result").hidden = !show;
  if (!show) return;
  const settled = result.settled;
  if (active) {
    $("result-title").textContent = "Preliminärt resultat";
    $("result-sub").textContent =
      `Efter ${Math.round(result.hours)} timmar. Svaren blir säkrare för varje fas.`;
  } else {
    $("result-title").textContent = run.floor ? `Resultat, ${run.floor.toLowerCase()}` : "Resultat";
    $("result-sub").textContent =
      `Test ${fmtWhen(run.started)} – ${fmtWhen(run.ended)}, ${Math.round(result.hours)} timmars mätning.` +
      (settled ? "" : " Alla svar blev inte säkra; kör gärna om testet en kallare dag.");
  }

  const verdicts = result.thermostats || [];
  const wrong = verdicts.filter((v) => v.status === "wrong");
  const fixes = result.fixes || [];
  const fixBox = $("fixes");
  if (!active && fixes.length) {
    fixBox.replaceChildren(
      el(
        "div",
        { class: "fixes" },
        el("h3", { text: "Så rättar du" }),
        el("ol", {}, fixes.map((fix) => el("li", { text: fix.text }))),
        el("p", {
          class: "small",
          text:
            "Ändra kopplingen i LK:s app (vilken kanal på centralen som hör till vilken rumsgivare), " +
            "eller flytta ställdonen mellan slingorna på fördelaren. Kör sedan testet igen för att kontrollera.",
        }),
      ),
    );
  } else if (!active && settled && wrong.length === 0) {
    fixBox.replaceChildren(
      el("div", { class: "fixes good" }, el("h3", { text: "Alla termostater styr sitt eget rum. Inget att rätta." })),
    );
  } else {
    fixBox.replaceChildren();
  }

  const marks = { ok: "✓", wrong: "!", unclear: "?", none: "–", pending: "…", no_variation: "–" };
  $("verdicts").replaceChildren(
    ...verdicts.map((v) => {
      let what;
      if (v.status === "ok") what = el("span", {}, "Styr ", el("strong", { text: "sitt eget rum" }));
      else if (v.status === "wrong") what = el("span", {}, "Styr slingan i ", el("strong", { text: v.heats_name }));
      else if (v.status === "unclear") what = el("span", {}, "Troligen ", el("strong", { text: v.heats_name }), ", inte säkert än");
      else if (active) what = el("span", { class: "muted", text: "Ingen tydlig effekt än" });
      else what = el("span", { text: v.text });
      if (v.also && v.also.length && ["ok", "wrong"].includes(v.status)) {
        what.append(` (värmer också ${v.also.join(", ")})`);
      }
      const tag = v.confidence
        ? el("span", {
            class: `tag ${v.confidence === "säker" ? "sure" : v.confidence === "trolig" ? "likely" : ""}`,
            text: v.confidence,
          })
        : el("span");
      return el(
        "li",
        { class: `verdict ${v.status}`, title: v.text },
        el("span", { class: "mark", text: marks[v.status] || "·" }),
        el("span", { class: "who", text: v.name }),
        el("span", { class: "what" }, what),
        tag,
      );
    }),
  );
  $("result-notes").replaceChildren(...(result.notes || []).map((note) => el("li", { text: note })));
  renderMatrix(result);
}

function renderMatrix(result) {
  const rooms = result.rooms || [];
  const rows = result.thermostats.filter((t) => result.matrix[t.key]);
  if (!rows.length || !rooms.length) {
    $("matrix").replaceChildren(el("p", { class: "empty", text: "Ingen tabell än." }));
    return;
  }
  let max = 0.05;
  for (const t of rows) for (const cell of Object.values(result.matrix[t.key])) max = Math.max(max, cell.effect);
  const head = el("tr", {}, el("th", { text: "Termostat \\ rum" }), rooms.map((r) => el("th", { text: r.name })));
  const body = rows.map((t) =>
    el(
      "tr",
      {},
      el("th", { text: t.name }),
      rooms.map((r) => {
        const cell = result.matrix[t.key][r.key];
        if (!cell) return el("td", { text: "" });
        const share = Math.max(0, Math.min(1, cell.effect / max));
        const cls = [r.key === t.key ? "own" : "", r.key === t.heats ? "best" : ""].join(" ");
        return el("td", {
          class: cls,
          title: `${t.name} → ${r.name}: ${cell.effect.toFixed(2)} °C/h (z ${cell.z})`,
          style: `background: color-mix(in srgb, var(--heat) ${Math.round(share * 85)}%, var(--line-2))`,
          text: cell.effect >= 0.05 ? cell.effect.toFixed(1) : "",
        });
      }),
    ),
  );
  $("matrix").replaceChildren(el("table", { class: "matrix" }, el("thead", {}, head), el("tbody", {}, body)));
}

// --- setup ---------------------------------------------------------------------

function renderSetupSummary() {
  const s = state.status.settings;
  const parts = [];
  parts.push(s.thermostats.length ? `${s.thermostats.length} termostater` : "Inga termostater valda");
  if (s.floors.length) {
    const count = (floor) => s.thermostats.filter((t) => t.floor === floor).length;
    parts.push(s.floors.map((floor) => `${count(floor)} på ${floor.toLowerCase()}`).join(", "));
  }
  if (s.extra_sensors.length) parts.push(`${s.extra_sensors.length} extra givare`);
  parts.push(s.supply_entity ? "framledning mäts" : "ingen framledningsgivare");
  parts.push(s.hp_setpoint_entity ? "värmepumpen kan höjas vid behov" : "värmepumpen styrs inte");
  parts.push(`${s.min_temp}–${s.max_temp} °C i rummen`);
  $("setup-summary").textContent = parts.join(" · ");
  $("edit-btn").textContent = state.editing ? "Stäng" : "Ändra";
}

function renderHow() {
  const s = state.status.settings;
  const n = s.test_count;
  renderFloorChoice(s);
  const phases = Math.max(8, 4 * Math.ceil((n + 1) / 4));
  const hours = phases * s.phase_hours;
  const items = [
    (s.test_floor
      ? `Bara ${s.test_floor.toLowerCase()} testas; övriga termostater går som vanligt. Testa nästa våning när den här är klar. `
      : "") +
      `Termostaterna ställs omväxlande på ${s.open_setpoint} °C (slingan öppen) och ${s.closed_setpoint} °C (stängd), ` +
      `i ${phases} faser à ${s.phase_hours} timmar – ungefär ${fmtDuration(hours * 3600)}. Hälften av slingorna är på åt gången.`,
    "Slingkoll ser vilka rum som blir varmare när en viss termostat är på, och räknar ut vilken slinga som hör till vilket rum.",
    `Rummen hålls mellan ${s.min_temp} och ${s.max_temp} °C. Blir ett rum kallare eller varmare pausas testet och allt går som vanligt tills det har hämtat sig.`,
  ];
  if (s.hp_setpoint_entity && s.supply_entity) {
    items.push("Är golvvärmevattnet för svalt höjs värmepumpens börvärde lite under testet.");
  }
  if (s.hemopt_switch_entity) items.push("hemopt:s styrning stängs av under testet och slås på igen efteråt.");
  items.push("När testet är klart, eller om du avbryter, får alla termostater tillbaka sina gamla börvärden.");
  items.push("Bäst resultat när det är kallt ute. Låt dörrarna vara som vanligt och elda inte i kaminen under testet.");
  $("how").replaceChildren(...items.map((text) => el("li", { text })));
  $("start-btn").disabled = n < 2 || state.busy;
}

function renderFloorChoice(s) {
  const box = $("floor-choice");
  if (!s.floors.length) {
    box.replaceChildren();
    return;
  }
  const count = (floor) => (floor ? s.thermostats.filter((t) => t.floor === floor).length : s.thermostats.length);
  const choose = async (floor) => {
    if ((floor || null) === (s.test_floor || null)) return;
    try {
      await api("api/settings", { method: "PUT", body: { test_floor: floor } });
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  };
  const options = [...s.floors.map((floor) => [floor, floor]), [null, "Alla våningar"]];
  box.replaceChildren(
    el("span", { class: "muted small", text: "Testa:" }),
    el(
      "div",
      { class: "segmented", role: "group", "aria-label": "Våning att testa" },
      options.map(([floor, label]) =>
        el("button", {
          type: "button",
          "aria-pressed": String((floor || null) === (s.test_floor || null)),
          text: `${label} (${count(floor)})`,
          onclick: () => choose(floor),
        }),
      ),
    ),
  );
}

async function openEditor() {
  if (state.editing) {
    state.editing = false;
    $("settings-form").hidden = true;
    renderSetupSummary();
    return;
  }
  try {
    state.found = await api("api/discover");
  } catch (error) {
    toast(error.message);
    return;
  }
  state.editing = true;
  buildForm();
  $("settings-form").hidden = false;
  renderSetupSummary();
}

function buildForm() {
  const s = state.status.settings;
  const found = state.found;
  const form = $("settings-form");
  const chosen = new Map(s.thermostats.map((t) => [t.entity_id, t.name]));
  const extra = new Map(s.extra_sensors.map((t) => [t.entity_id, t.name]));
  const floorOf = new Map([...s.thermostats, ...s.extra_sensors].map((t) => [t.entity_id, t.floor || ""]));
  const floorNames = [...new Set(["Övervåning", "Bottenvåning", ...s.floors])];

  const pickRow = (group, item, checked, name) =>
    el(
      "label",
      { class: "pick" },
      el("input", { type: "checkbox", "data-group": group, "data-entity": item.entity_id, checked }),
      el("input", { type: "text", value: name, "data-name-for": item.entity_id, "aria-label": `Namn på ${item.entity_id}` }),
      el("input", {
        type: "text",
        list: "floor-names",
        placeholder: "Våning",
        value: floorOf.get(item.entity_id) || "",
        "data-floor-for": item.entity_id,
        "aria-label": `Våning för ${item.entity_id}`,
      }),
      el("small", { text: item.entity_id }),
    );

  const kindRows = s.floors.length
    ? s.floors.map((floor, index) =>
        el(
          "label",
          { class: "field" },
          el("span", { text: floor }),
          el(
            "select",
            { id: `f-kind-${index}`, "data-floor": floor },
            el("option", { value: "slow", selected: (s.floor_kinds[floor] || s.floor) === "slow", text: "Betongplatta – 3 timmar per fas" }),
            el("option", { value: "fast", selected: (s.floor_kinds[floor] || s.floor) === "fast", text: "Trägolv på bjälklag/spånskiva – 2 timmar per fas" }),
          ),
        ),
      )
    : null;

  const select = (id, label, options, value, hint) =>
    el(
      "label",
      { class: "field" },
      el("span", { text: label }),
      el(
        "select",
        { id },
        el("option", { value: "", text: "– ingen –" }),
        options.map((o) => el("option", { value: o.entity_id, selected: o.entity_id === value, text: `${o.name} (${o.entity_id})` })),
        value && !options.some((o) => o.entity_id === value) ? el("option", { value, selected: true, text: value }) : null,
      ),
      hint ? el("small", { class: "muted", text: hint }) : null,
    );

  const thermos = found.thermostats.length
    ? found.thermostats.map((t) => pickRow("thermo", t, chosen.has(t.entity_id), chosen.get(t.entity_id) || t.name))
    : [el("p", { class: "empty", text: "Hittade inga termostater (climate) i Home Assistant." })];

  form.replaceChildren(
    el(
      "fieldset",
      {},
      el("legend", { text: "Termostater som ska testas" }),
      el("p", {
        class: "muted small",
        text:
          "Namnet är rummet där termostaten sitter. Våningen anger vilka rum slingan kan ligga i: " +
          "en slinga räknas bara mot rum på samma våning. Har våningarna egna fördelningsskåp, ange dem.",
      }),
      el("datalist", { id: "floor-names" }, floorNames.map((name) => el("option", { value: name }))),
      thermos,
    ),
    el(
      "fieldset",
      {},
      el("legend", { text: "Extra givare (valfritt)" }),
      el("p", {
        class: "muted small",
        text: "Temperaturgivare i rum utan egen termostat, till exempel hall eller badrum. Då syns det om en slinga egentligen värmer ett sådant rum.",
      }),
      found.sensors.length
        ? found.sensors.map((t) => pickRow("extra", t, extra.has(t.entity_id), extra.get(t.entity_id) || t.name))
        : el("p", { class: "empty", text: "Inga andra temperaturgivare hittades." }),
    ),
    el(
      "fieldset",
      {},
      el("legend", { text: "Värmepump" }),
      el(
        "div",
        { class: "field-row" },
        select("f-supply", "Framledning till golvet", found.supply, s.supply_entity, "Visar om det finns varmt vatten i slingorna."),
        select("f-hp", "Värmepumpens rumsbörvärde", found.hp_setpoint, s.hp_setpoint_entity, "Höjs lite om vattnet är för svalt."),
        select("f-outdoor", "Utetemperatur", found.outdoor, s.outdoor_entity),
        select("f-hemopt", "hemopt:s styrning", found.hemopt_switch, s.hemopt_switch_entity, "Stängs av under testet."),
      ),
    ),
    el(
      "fieldset",
      {},
      el("legend", { text: "Under testet" }),
      el(
        "div",
        { class: "field-row" },
        el("label", { class: "field" }, el("span", { text: "Lägsta rumstemperatur" }), el("input", { type: "number", id: "f-min", step: "0.5", value: s.min_temp })),
        el("label", { class: "field" }, el("span", { text: "Högsta rumstemperatur" }), el("input", { type: "number", id: "f-max", step: "0.5", value: s.max_temp })),
      ),
      kindRows
        ? el("div", { class: "field-row" }, kindRows)
        : [
            el(
              "label",
              { class: "radio" },
              el("input", { type: "radio", name: "floor", value: "slow", checked: s.floor === "slow" }),
              el("span", {}, el("strong", { text: "Betongplatta någonstans i huset" }), " – 3 timmar per fas"),
            ),
            el(
              "label",
              { class: "radio" },
              el("input", { type: "radio", name: "floor", value: "fast", checked: s.floor === "fast" }),
              el("span", {}, el("strong", { text: "Bara trägolv på bjälklag eller spånskiva" }), " – 2 timmar per fas"),
            ),
          ],
    ),
    el("div", { class: "actions" }, el("button", { type: "submit", class: "btn primary", text: "Spara" })),
  );
}

async function saveSettings(event) {
  event.preventDefault();
  const form = $("settings-form");
  const collect = (group) =>
    [...form.querySelectorAll(`input[data-group="${group}"]:checked`)].map((box) => {
      const entity = box.dataset.entity;
      const name = form.querySelector(`input[data-name-for="${CSS.escape(entity)}"]`).value.trim();
      const floor = form.querySelector(`input[data-floor-for="${CSS.escape(entity)}"]`).value.trim();
      return { entity_id: entity, name: name || entity, floor };
    });
  const floor = form.querySelector('input[name="floor"]:checked');
  const floorKinds = {};
  form.querySelectorAll("select[data-floor]").forEach((box) => (floorKinds[box.dataset.floor] = box.value));
  const body = {
    thermostats: collect("thermo"),
    extra_sensors: collect("extra"),
    supply_entity: $("f-supply").value || null,
    hp_setpoint_entity: $("f-hp").value || null,
    outdoor_entity: $("f-outdoor").value || null,
    hemopt_switch_entity: $("f-hemopt").value || null,
    min_temp: Number($("f-min").value),
    max_temp: Number($("f-max").value),
    floor: floor ? floor.value : state.status.settings.floor,
    floor_kinds: floorKinds,
  };
  try {
    await api("api/settings", { method: "PUT", body });
    state.editing = false;
    form.hidden = true;
    toast("Sparat.");
    await refresh();
  } catch (error) {
    toast(error.message);
  }
}

// --- quick check ----------------------------------------------------------------

async function quickCheck() {
  const button = $("quick-btn");
  button.disabled = true;
  $("quick").replaceChildren(el("p", { class: "empty", text: "Hämtar historik…" }));
  try {
    const result = await api("api/quick-check", { method: "POST", body: { days: 7 } });
    const flagged = result.symptoms.filter((s) => s.flag === "cold" || s.flag === "warm");
    const hints = result.thermostats.filter((v) => v.status === "wrong" && v.confidence !== "osäker");
    const summary = flagged.length
      ? `${flagged.length} termostater beter sig som om slingorna är förväxlade. Kör testet för att få säkert besked.`
      : "Inga tydliga tecken på förväxlade slingor i historiken. Bara testet ger säkert besked.";
    $("quick").replaceChildren(
      el("p", { text: summary }),
      el(
        "ul",
        { class: "symptoms" },
        result.symptoms.map((s) => el("li", { class: s.flag }, el("strong", { text: s.name }), el("span", { text: s.text }))),
      ),
      hints.length
        ? el(
            "p",
            { class: "muted small" },
            "Historiken antyder: ",
            hints.map((v) => `${v.name} → ${v.heats_name}`).join(", "),
            ". Det är en indikation, inte ett svar.",
          )
        : null,
    );
  } catch (error) {
    $("quick").replaceChildren(el("p", { class: "empty", text: error.message }));
  } finally {
    button.disabled = false;
  }
}

// --- actions -------------------------------------------------------------------

// The Home Assistant app blocks confirm() inside add-on pages, so questions
// are asked on the page itself, right under the button that was pressed.
function ask(button, text, yesLabel) {
  document.querySelectorAll(".ask").forEach((node) => node.remove());
  return new Promise((resolve) => {
    const finish = (answer) => {
      box.remove();
      resolve(answer);
    };
    const box = el(
      "div",
      { class: "ask banner warn", role: "alertdialog" },
      el("p", { text }),
      el(
        "div",
        { class: "actions" },
        el("button", { type: "button", class: "btn primary", text: yesLabel, onclick: () => finish(true) }),
        el("button", { type: "button", class: "btn ghost", text: "Nej", onclick: () => finish(false) }),
      ),
    );
    button.closest(".actions").after(box);
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

async function startTest() {
  const s = state.status.settings;
  const button = $("start-btn");
  const ok = await ask(
    button,
    `Starta testet med ${s.thermostats.length} termostater? Slingkoll styr termostaterna ` +
      "i ett till tre dygn och återställer dem när testet är klart.",
    "Ja, starta",
  );
  if (!ok) return;
  state.busy = true;
  button.disabled = true;
  button.textContent = "Startar…";
  try {
    await api("api/test/start", { method: "POST", body: {} });
    state.series = null;
    state.seriesAt = 0;
    toast("Testet har startat.");
  } catch (error) {
    toast(error.message);
  } finally {
    state.busy = false;
    button.textContent = "Starta testet";
    await refresh();
  }
}

async function stopTest(analyse) {
  const button = analyse ? $("finish-btn") : $("abort-btn");
  const text = analyse
    ? "Avsluta testet nu? Termostaterna återställs och du får det resultat som finns hittills."
    : "Avbryta testet? Termostaterna återställs och inget resultat sparas.";
  if (!(await ask(button, text, analyse ? "Ja, avsluta" : "Ja, avbryt"))) return;
  button.disabled = true;
  try {
    await api("api/test/stop", { method: "POST", body: { analyse } });
    toast("Termostaterna är återställda.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
  await refresh();
}

$("edit-btn").addEventListener("click", openEditor);
$("settings-form").addEventListener("submit", saveSettings);
$("quick-btn").addEventListener("click", quickCheck);
$("start-btn").addEventListener("click", startTest);
$("finish-btn").addEventListener("click", () => stopTest(true));
$("abort-btn").addEventListener("click", () => stopTest(false));

(async function loop() {
  await refresh();
  const demo = state.status && state.status.demo;
  setTimeout(loop, demo ? 2000 : 15000);
})();
