// AtlasQL query builder.
//
// Everything selectable here comes from GET /metadata: the levels, the metrics,
// their units and their per-level coverage. Nothing about the data is hardcoded,
// so a metric added by an ETL job shows up after a reload with no frontend
// change. The only thing this file knows about the engine is the shape of a
// GeoFilter, which is the same object the API accepts from any input path.

import { createGlobe } from "./globe.js";

const OPERATORS = [
  { value: ">", label: ">" },
  { value: ">=", label: "≥" },
  { value: "<", label: "<" },
  { value: "<=", label: "≤" },
  { value: "==", label: "=" },
];

// Levels are a closed set fixed by the schema, so naming their plurals here is
// safe; the metrics and the levels themselves still come from /metadata.
const PLURALS = {
  continent: "continents",
  country: "countries",
  state: "states",
  county: "counties",
  city: "cities",
};

// Simplification tolerance in degrees, sent to /geometry. The overview is what
// arrives with a result; the detail version is fetched only once the user has
// zoomed far enough in for the difference to be visible, because it is roughly
// five times the payload.
const OVERVIEW_TOLERANCE = 0.05;
const DETAIL_TOLERANCE = 0.005;
const DETAIL_ZOOM_FACTOR = 4;

let metadata = null;
let conditionSeq = 0;
let globe = null;
let lastResult = null;
let detailRequested = false;

const $ = (id) => document.getElementById(id);

// A query builder is five bound controls (level, conditions, sort, order,
// top-n) plus the behaviour that keeps them consistent with /metadata. The
// single-query panel and each side of the compare panel are three instances
// of exactly this, never three copies of the logic - that would let them
// drift out of sync with what GeoFilter actually accepts.
function createPanel({ level, conditions, addConditionBtn, sortBy, order, topN, levelHint }) {
  function addCondition(metricName) {
    const id = ++conditionSeq;
    const row = document.createElement("div");
    row.className = "condition";
    row.dataset.id = id;

    const metric = document.createElement("select");
    metric.className = "metric";
    for (const entry of metadata.metrics) {
      metric.appendChild(new Option(entry.label, entry.name));
    }
    if (metricName) metric.value = metricName;

    const op = document.createElement("select");
    op.className = "op";
    for (const entry of OPERATORS) op.appendChild(new Option(entry.label, entry.value));

    const value = document.createElement("input");
    value.type = "number";
    value.className = "value";
    value.step = "any";
    value.placeholder = "value";

    const unit = document.createElement("span");
    unit.className = "unit";

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "×";
    remove.title = "Remove this condition";
    remove.onclick = () => {
      row.remove();
      updateHints();
    };

    const hint = document.createElement("span");
    hint.className = "hint coverage";

    metric.onchange = updateHints;
    row.append(metric, op, value, unit, remove, hint);
    conditions.appendChild(row);
    updateHints();
  }

  // Coverage is shown next to every metric so the reason a query will be
  // refused is visible before it is run, rather than only in the error.
  function updateHints() {
    if (!metadata) return;
    const threshold = metadata.coverage_threshold_pct;
    const lvl = level.value;

    for (const row of conditions.querySelectorAll(".condition")) {
      const metric = metricByName(row.querySelector(".metric").value);
      row.querySelector(".unit").textContent = metric?.unit ?? "";
      const hint = row.querySelector(".coverage");
      if (!metric) continue;

      const levels = metric.levels_with_data;
      if (levels.length === 0) {
        hint.textContent = "no level has enough data for this metric";
        hint.classList.add("warn");
      } else if (lvl !== "auto" && !levels.includes(lvl)) {
        // "No data here" and "some data, but too little" are different
        // situations: one is a property of the world, the other is a
        // threshold call. Reporting 0.0% for the first reads like a broken
        // import.
        const pct = metric.coverage_pct[lvl] ?? 0;
        hint.textContent =
          pct === 0
            ? `not measured at ${lvl} level — available at: ${levels.join(", ")}`
            : `only ${pct.toFixed(1)}% of ${lvl}s have this — available at: ${levels.join(", ")}`;
        hint.classList.add("warn");
      } else {
        hint.textContent = `available at: ${levels.join(", ")}`;
        hint.classList.remove("warn");
      }
    }

    levelHint.textContent =
      lvl === "auto"
        ? `the most detailed level where every metric clears ${threshold}% coverage`
        : `metrics below ${threshold}% coverage at this level are refused, not silently dropped`;
  }

  function buildFilter() {
    const conditionList = [];
    for (const row of conditions.querySelectorAll(".condition")) {
      const raw = row.querySelector(".value").value;
      if (raw === "") continue;
      conditionList.push({
        metric: row.querySelector(".metric").value,
        op: row.querySelector(".op").value,
        value: Number(raw),
      });
    }
    const filter = {
      level: level.value,
      conditions: conditionList,
      order: order.value,
      top_n: Number(topN.value),
    };
    const sortByValue = sortBy.value;
    if (sortByValue) filter.sort_by = sortByValue;
    return filter;
  }

  // A parsed filter is shown in the same form the user would have filled in
  // by hand, so a misparse is visible and editable before anything runs.
  // This is also why /parse and /query are separate calls.
  function applyFilter(filter) {
    level.value = filter.level ?? "auto";
    order.value = filter.order ?? "desc";
    topN.value = filter.top_n ?? 10;
    sortBy.value = filter.sort_by ?? "";

    conditions.innerHTML = "";
    for (const condition of filter.conditions ?? []) {
      addCondition(condition.metric);
      const row = conditions.lastElementChild;
      row.querySelector(".op").value = condition.op;
      row.querySelector(".value").value = condition.value;
    }
    if (!filter.conditions?.length) addCondition();
    updateHints();
  }

  function populateSelectors() {
    level.innerHTML = "";
    level.appendChild(new Option("Auto (pick the most detailed level with data)", "auto"));
    for (const entry of metadata.levels) {
      level.appendChild(
        new Option(`${titleCase(entry.name)} (${entry.region_count.toLocaleString()})`, entry.name)
      );
    }

    sortBy.innerHTML = "";
    sortBy.appendChild(new Option("First condition's metric", ""));
    for (const metric of metadata.metrics) {
      sortBy.appendChild(new Option(metric.label, metric.name));
    }
  }

  level.onchange = updateHints;
  addConditionBtn.onclick = () => addCondition();

  return { addCondition, updateHints, buildFilter, applyFilter, populateSelectors };
}

const singlePanel = createPanel({
  level: $("level"),
  conditions: $("conditions"),
  addConditionBtn: $("add-condition"),
  sortBy: $("sort-by"),
  order: $("order"),
  topN: $("top-n"),
  levelHint: $("level-hint"),
});
const comparePanelA = createPanel({
  level: $("level-a"),
  conditions: $("conditions-a"),
  addConditionBtn: $("add-condition-a"),
  sortBy: $("sort-by-a"),
  order: $("order-a"),
  topN: $("top-n-a"),
  levelHint: $("level-hint-a"),
});
const comparePanelB = createPanel({
  level: $("level-b"),
  conditions: $("conditions-b"),
  addConditionBtn: $("add-condition-b"),
  sortBy: $("sort-by-b"),
  order: $("order-b"),
  topN: $("top-n-b"),
  levelHint: $("level-hint-b"),
});

async function loadMetadata() {
  const response = await fetch("/metadata");
  if (!response.ok) throw new Error(`/metadata returned ${response.status}`);
  metadata = await response.json();

  for (const panel of [singlePanel, comparePanelA, comparePanelB]) {
    panel.populateSelectors();
  }

  // The natural language box only appears when the server has credentials for
  // it — better than offering a button that always fails.
  $("nl-panel").hidden = !metadata.natural_language_enabled;

  singlePanel.addCondition();
  singlePanel.updateHints();
  comparePanelA.addCondition();
  comparePanelA.updateHints();
  comparePanelB.addCondition();
  comparePanelB.updateHints();
}

// Compare mode swaps which builder(s) and result area are visible; it does
// not change what a query is or how it runs. Natural language parsing stays
// tied to the single-query panel - which side of a comparison a parsed
// filter should land on is ambiguous, so this keeps the ambiguity out
// entirely rather than guessing.
function setCompareMode(comparing) {
  $("nl-panel").hidden = comparing || !metadata?.natural_language_enabled;
  $("builder").hidden = comparing;
  $("compare-panel").hidden = !comparing;
  $("compare-error").hidden = true;
  $("compare-results").hidden = comparing ? $("compare-results").hidden : true;
  $("error").hidden = true;
  $("results-panel").hidden = comparing || !lastResult;
}

async function parseText() {
  const text = $("nl-text").value.trim();
  if (!text) return;

  const status = $("nl-status");
  const button = $("parse");
  status.textContent = "parsing…";
  status.classList.remove("warn");
  button.disabled = true;

  try {
    const response = await fetch("/parse", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const body = await response.json();
    if (!response.ok) {
      status.textContent = body.detail ?? "Could not parse that question.";
      status.classList.add("warn");
      return;
    }
    singlePanel.applyFilter(body.filter);
    $("filter-json").textContent = JSON.stringify(body.filter, null, 2);
    status.textContent = "Filled in below — check it, edit anything, then run.";
  } catch (error) {
    status.textContent = `Could not reach the parser. ${error}`;
    status.classList.add("warn");
  } finally {
    button.disabled = false;
  }
}

function metricByName(name) {
  return metadata.metrics.find((m) => m.name === name);
}

function titleCase(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function showError(message, detail) {
  $("error-message").textContent = message;
  $("error-detail").textContent = detail ?? "";
  $("error").hidden = false;
  $("results-panel").hidden = true;
}

// Shared by the single-query table and each side of the compare table: same
// columns, same formatting, same "-" for a metric a region lacks. onRowClick
// is only wired up in single-query mode, where a row selection also drives
// the globe; the compare tables are read-only lists with nothing to select.
function renderTable(body, { head, tbody, onRowClick }) {
  const metrics = [...new Set(body.applied_filter.conditions.map((c) => c.metric))];
  if (body.applied_filter.sort_by && !metrics.includes(body.applied_filter.sort_by)) {
    metrics.push(body.applied_filter.sort_by);
  }

  head.innerHTML = "";
  for (const column of ["#", titleCase(body.level), "Within"]) {
    const th = document.createElement("th");
    th.textContent = column;
    head.appendChild(th);
  }
  for (const name of metrics) {
    const metric = metricByName(name);
    const th = document.createElement("th");
    th.className = "numeric";
    th.textContent = metric ? `${metric.label}${metric.unit ? ` (${metric.unit})` : ""}` : name;
    head.appendChild(th);
  }

  tbody.innerHTML = "";
  body.results.forEach((row, index) => {
    const tr = document.createElement("tr");
    tr.dataset.regionId = row.region_id;
    // Selecting from the table flies the globe to the region; selecting from
    // the globe highlights the row. One selection, two views of it.
    if (onRowClick) tr.onclick = () => onRowClick(row.region_id);
    for (const text of [index + 1, row.name, row.parent_name ?? "—"]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    for (const name of metrics) {
      const td = document.createElement("td");
      td.className = "numeric";
      const entry = row.metrics[name];
      td.textContent = entry ? formatNumber(entry.value) : "—";
      // The vintage travels with each value, so a column mixing years says so.
      if (entry) td.title = `${entry.value} (${entry.year})`;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
}

function summarize(body) {
  const noun = body.count === 1 ? body.level : PLURALS[body.level] ?? `${body.level}s`;
  return (
    `${body.count} ${noun} — level chosen ` +
    `${body.level_chosen_by === "auto" ? "automatically" : "by you"}`
  );
}

function renderResults(body) {
  renderTable(body, {
    head: $("results-head"),
    tbody: $("results-body"),
    onRowClick: (regionId) => selectRegion(regionId),
  });
  $("results-summary").textContent = summarize(body);
  $("results-panel").hidden = false;
  $("error").hidden = true;

  // After the panel is visible, so the canvas has a size to measure.
  lastResult = body;
  showOnMap(body);
}

// ---------------------------------------------------------------- the map --
//
// The globe is a second view of the same result set, not a second query path:
// it draws region ids /query already returned, and geometry is fetched by id.
// Nothing here can produce a different answer from the table beside it.

function setUpGlobe() {
  globe = createGlobe($("globe"), {
    // Clicking a region on the map should not also move the camera - the user
    // is already looking at the thing they clicked.
    onSelect: (regionId) => selectRegion(regionId, { fly: false }),
    onHover: showTooltip,
    onCamera: maybeRefineDetail,
  });

  fetch("/geometry/basemap?level=country&tolerance=0.25")
    .then((response) => (response.ok ? response.json() : null))
    .then((collection) => collection && globe.setBasemap(collection))
    .catch((error) => {
      // Context outlines are not the answer to anything; results still draw.
      console.warn("basemap unavailable, drawing results without context", error);
    });

  $("fit-results").onclick = () => globe.fitResults();
  $("whole-globe").onclick = () => globe.home();
}

async function fetchGeometry(ids, tolerance) {
  try {
    const response = await fetch(
      `/geometry?ids=${ids.join(",")}&tolerance=${tolerance}`
    );
    if (!response.ok) return null;
    return await response.json();
  } catch (error) {
    console.warn("geometry unavailable", error);
    return null;
  }
}

// The metric the fill encodes is the one the results are ranked by, so the
// darkest region is always the top row. Any other choice would put the map and
// the table in disagreement.
function colorMetric(filter) {
  return filter.sort_by || filter.conditions[0]?.metric;
}

async function showOnMap(body) {
  detailRequested = false;
  const metric = colorMetric(body.applied_filter);
  const values = new Map(
    body.results.map((row) => [row.region_id, row.metrics[metric]?.value])
  );
  updateLegend(metric, values);

  if (!body.results.length) {
    globe.setResults([], values);
    return;
  }
  const collection = await fetchGeometry(
    body.results.map((row) => row.region_id),
    OVERVIEW_TOLERANCE
  );
  if (!collection) return;
  globe.setResults(collection.features, values);
  // Frames the answer: one country fills the view, a scattered set pulls back
  // far enough to hold all of it, and either way zooming out reaches the globe.
  globe.fitResults();
}

// Zoomed in past the point where the overview outline looks like a polygon
// rather than a coastline, fetch the same regions at full detail once.
function maybeRefineDetail(zoomFactor) {
  if (detailRequested || zoomFactor < DETAIL_ZOOM_FACTOR || !lastResult) return;
  detailRequested = true;
  fetchGeometry(
    lastResult.results.map((row) => row.region_id),
    DETAIL_TOLERANCE
  ).then((collection) => collection && globe.refineResults(collection.features));
}

function updateLegend(metricName, values) {
  const numbers = [...values.values()].filter(Number.isFinite);
  const legend = $("map-legend");
  legend.hidden = numbers.length === 0;
  if (!numbers.length) return;
  const metric = metricByName(metricName);
  $("legend-label").textContent = metric?.label ?? metricName;
  $("legend-low").textContent = formatNumber(Math.min(...numbers));
  $("legend-high").textContent = formatNumber(Math.max(...numbers));
}

function selectRegion(regionId, { fly = true } = {}) {
  for (const tr of $("results-body").querySelectorAll("tr")) {
    tr.classList.toggle("selected", Number(tr.dataset.regionId) === regionId);
  }
  globe.select(regionId, { fly });
  if (!fly && regionId != null) {
    $("results-body")
      .querySelector(`tr[data-region-id="${regionId}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }
}

function showTooltip(feature, position) {
  const tip = $("globe-tooltip");
  if (!feature) {
    tip.hidden = true;
    return;
  }
  const row = lastResult?.results.find(
    (r) => r.region_id === feature.properties.region_id
  );

  tip.textContent = "";
  const name = document.createElement("strong");
  name.textContent = feature.properties.name;
  tip.appendChild(name);
  if (feature.properties.parent_name) {
    const parent = document.createElement("div");
    parent.className = "tooltip-metric";
    parent.textContent = feature.properties.parent_name;
    tip.appendChild(parent);
  }
  for (const [metricName, entry] of Object.entries(row?.metrics ?? {})) {
    const line = document.createElement("div");
    line.className = "tooltip-metric";
    const metric = metricByName(metricName);
    line.textContent =
      `${metric?.label ?? metricName}: ${formatNumber(entry.value)}` +
      `${metric?.unit ? ` ${metric.unit}` : ""}`;
    tip.appendChild(line);
  }

  tip.hidden = false;
  // Flip to the other side of the cursor rather than letting the tooltip run
  // off the map and get clipped by the panel's overflow.
  const map = tip.parentElement.getBoundingClientRect();
  const flip = position.x + tip.offsetWidth + 24 > map.width;
  tip.style.left = `${flip ? position.x - tip.offsetWidth - 14 : position.x + 14}px`;
  tip.style.top = `${Math.min(position.y + 14, map.height - tip.offsetHeight - 8)}px`;
}

function formatNumber(value) {
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 1 : 3;
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

// Runs one GeoFilter against /query. Used directly by the single-query path
// and twice (in parallel) by compare mode - two calls to the one execution
// path, never a second one.
async function runOne(filter) {
  const response = await fetch("/query", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(filter),
  });
  const body = await response.json();
  if (!response.ok) return { ok: false, body };
  return { ok: true, body };
}

async function runQuery() {
  const filter = singlePanel.buildFilter();
  $("filter-json").textContent = JSON.stringify(filter, null, 2);

  if (filter.conditions.length === 0) {
    showError("Add at least one condition with a value.");
    return;
  }

  $("status").textContent = "running…";
  try {
    const result = await runOne(filter);
    if (!result.ok) {
      // A refusal names the metric responsible. Surfacing it verbatim is the
      // point: an honest "no" beats an empty table.
      const detail = result.body.blocking_metric
        ? `Blocking metric: ${result.body.blocking_metric}`
        : JSON.stringify(result.body);
      showError(result.body.error ?? "The API rejected this query.", detail);
      return;
    }
    renderResults(result.body);
  } catch (error) {
    showError("Could not reach the API.", String(error));
  } finally {
    $("status").textContent = "";
  }
}

function showCompareError(message) {
  $("compare-error-message").textContent = message;
  $("compare-error").hidden = false;
  $("compare-results").hidden = true;
}

// One rejected side names itself rather than falling back to a partial or
// empty comparison - the same "fail loudly" rule as the single-query path,
// applied per side instead of once.
function describeRejection(label, body) {
  const detail = body.blocking_metric ? ` (blocking metric: ${body.blocking_metric})` : "";
  return `${label}: ${body.error ?? "the API rejected this query"}${detail}`;
}

async function runCompare() {
  const filterA = comparePanelA.buildFilter();
  const filterB = comparePanelB.buildFilter();
  $("filter-json").textContent = JSON.stringify({ a: filterA, b: filterB }, null, 2);
  $("compare-status").textContent = "";

  if (filterA.conditions.length === 0 || filterB.conditions.length === 0) {
    showCompareError("Add at least one condition with a value to both Query A and Query B.");
    return;
  }

  $("compare-status").textContent = "running…";
  $("compare-error").hidden = true;
  try {
    const [resultA, resultB] = await Promise.all([runOne(filterA), runOne(filterB)]);
    if (!resultA.ok) {
      showCompareError(describeRejection("Query A", resultA.body));
      return;
    }
    if (!resultB.ok) {
      showCompareError(describeRejection("Query B", resultB.body));
      return;
    }

    renderTable(resultA.body, { head: $("compare-head-a"), tbody: $("compare-body-a") });
    $("compare-summary-a").textContent = `Query A — ${summarize(resultA.body)}`;
    renderTable(resultB.body, { head: $("compare-head-b"), tbody: $("compare-body-b") });
    $("compare-summary-b").textContent = `Query B — ${summarize(resultB.body)}`;

    $("compare-results").hidden = false;
    $("compare-error").hidden = true;
  } catch (error) {
    showCompareError(`Could not reach the API. ${error}`);
  } finally {
    $("compare-status").textContent = "";
  }
}

$("run").onclick = runQuery;
$("run-compare").onclick = runCompare;
$("compare-toggle").onchange = (event) => setCompareMode(event.target.checked);
$("parse").onclick = parseText;
$("nl-text").addEventListener("keydown", (event) => {
  // Enter parses; Shift+Enter is a newline.
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    parseText();
  }
});

setUpGlobe();
loadMetadata().catch((error) => showError("Could not load /metadata.", String(error)));
