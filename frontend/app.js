// AtlasQL query builder.
//
// Everything selectable here comes from GET /metadata: the levels, the metrics,
// their units and their per-level coverage. Nothing about the data is hardcoded,
// so a metric added by an ETL job shows up after a reload with no frontend
// change. The only thing this file knows about the engine is the shape of a
// GeoFilter, which is the same object the API accepts from any input path.

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

let metadata = null;
let conditionSeq = 0;

const $ = (id) => document.getElementById(id);

async function loadMetadata() {
  const response = await fetch("/metadata");
  if (!response.ok) throw new Error(`/metadata returned ${response.status}`);
  metadata = await response.json();

  const level = $("level");
  level.innerHTML = "";
  level.appendChild(new Option("Auto (pick the most detailed level with data)", "auto"));
  for (const entry of metadata.levels) {
    level.appendChild(
      new Option(`${titleCase(entry.name)} (${entry.region_count.toLocaleString()})`, entry.name)
    );
  }
  level.onchange = updateHints;

  const sortBy = $("sort-by");
  sortBy.innerHTML = "";
  sortBy.appendChild(new Option("First condition's metric", ""));
  for (const metric of metadata.metrics) {
    sortBy.appendChild(new Option(metric.label, metric.name));
  }

  addCondition();
  updateHints();
}

function metricByName(name) {
  return metadata.metrics.find((m) => m.name === name);
}

function titleCase(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

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
  $("conditions").appendChild(row);
  updateHints();
}

// Coverage is shown next to every metric so the reason a query will be
// refused is visible before it is run, rather than only in the error.
function updateHints() {
  if (!metadata) return;
  const threshold = metadata.coverage_threshold_pct;
  const level = $("level").value;

  for (const row of document.querySelectorAll(".condition")) {
    const metric = metricByName(row.querySelector(".metric").value);
    row.querySelector(".unit").textContent = metric?.unit ?? "";
    const hint = row.querySelector(".coverage");
    if (!metric) continue;

    const levels = metric.levels_with_data;
    if (levels.length === 0) {
      hint.textContent = "no level has enough data for this metric";
      hint.classList.add("warn");
    } else if (level !== "auto" && !levels.includes(level)) {
      // "No data here" and "some data, but too little" are different
      // situations: one is a property of the world, the other is a threshold
      // call. Reporting 0.0% for the first reads like a broken import.
      const pct = metric.coverage_pct[level] ?? 0;
      hint.textContent =
        pct === 0
          ? `not measured at ${level} level — available at: ${levels.join(", ")}`
          : `only ${pct.toFixed(1)}% of ${level}s have this — available at: ${levels.join(", ")}`;
      hint.classList.add("warn");
    } else {
      hint.textContent = `available at: ${levels.join(", ")}`;
      hint.classList.remove("warn");
    }
  }

  $("level-hint").textContent =
    level === "auto"
      ? `the most detailed level where every metric clears ${threshold}% coverage`
      : `metrics below ${threshold}% coverage at this level are refused, not silently dropped`;
}

function buildFilter() {
  const conditions = [];
  for (const row of document.querySelectorAll(".condition")) {
    const raw = row.querySelector(".value").value;
    if (raw === "") continue;
    conditions.push({
      metric: row.querySelector(".metric").value,
      op: row.querySelector(".op").value,
      value: Number(raw),
    });
  }
  const filter = {
    level: $("level").value,
    conditions,
    order: $("order").value,
    top_n: Number($("top-n").value),
  };
  const sortBy = $("sort-by").value;
  if (sortBy) filter.sort_by = sortBy;
  return filter;
}

function showError(message, detail) {
  $("error-message").textContent = message;
  $("error-detail").textContent = detail ?? "";
  $("error").hidden = false;
  $("results-panel").hidden = true;
}

function renderResults(body) {
  const metrics = [...new Set(body.applied_filter.conditions.map((c) => c.metric))];
  if (body.applied_filter.sort_by && !metrics.includes(body.applied_filter.sort_by)) {
    metrics.push(body.applied_filter.sort_by);
  }

  const head = $("results-head");
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

  const tbody = $("results-body");
  tbody.innerHTML = "";
  body.results.forEach((row, index) => {
    const tr = document.createElement("tr");
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

  const noun = body.count === 1 ? body.level : PLURALS[body.level] ?? `${body.level}s`;
  $("results-summary").textContent =
    `${body.count} ${noun} — level chosen ` +
    `${body.level_chosen_by === "auto" ? "automatically" : "by you"}`;
  $("results-panel").hidden = false;
  $("error").hidden = true;
}

function formatNumber(value) {
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 1 : 3;
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

async function runQuery() {
  const filter = buildFilter();
  $("filter-json").textContent = JSON.stringify(filter, null, 2);

  if (filter.conditions.length === 0) {
    showError("Add at least one condition with a value.");
    return;
  }

  $("status").textContent = "running…";
  try {
    const response = await fetch("/query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(filter),
    });
    const body = await response.json();

    if (!response.ok) {
      // A refusal names the metric responsible. Surfacing it verbatim is the
      // point: an honest "no" beats an empty table.
      const detail = body.blocking_metric
        ? `Blocking metric: ${body.blocking_metric}`
        : JSON.stringify(body);
      showError(body.error ?? "The API rejected this query.", detail);
      return;
    }
    renderResults(body);
  } catch (error) {
    showError("Could not reach the API.", String(error));
  } finally {
    $("status").textContent = "";
  }
}

$("add-condition").onclick = () => addCondition();
$("run").onclick = runQuery;
$("level").onchange = updateHints;

loadMetadata().catch((error) => showError("Could not load /metadata.", String(error)));
