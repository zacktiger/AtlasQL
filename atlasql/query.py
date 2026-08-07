"""GeoFilter -> parameterized SQL.

Three rules hold here and are worth stating plainly, because the rest of the
system depends on them:

  * Every value the caller supplies is a bound parameter. Nothing user-derived
    is concatenated into SQL. The only parts of the statement assembled in
    Python are the number of joins and their generated aliases; operators come
    from a closed Literal set and metric names are checked against the live
    registry before they are used, and even then travel as parameters.
  * Coverage is enforced, never guessed. If no level has data for every metric
    the query mentions, the caller gets an error naming the blocking metric
    instead of a silently degraded level or an empty table.
  * A region missing a value for any metric in the query is excluded, which is
    the default the plan settles on. It follows from the joins being inner.
"""

from __future__ import annotations

import logging
import threading
import time

import psycopg
from psycopg import sql

from atlasql import config, db
from atlasql.models import GeoFilter, Level, MetricValue, QueryResult, ResultRow

log = logging.getLogger(__name__)

# Explicit whitelist. `op` is already constrained by the Literal on Condition;
# this makes the SQL text depend on a fixed table rather than on caller input.
_OPERATORS = {">": ">", "<": "<", ">=": ">=", "<=": "<=", "==": "="}


class QueryError(Exception):
    """A query that cannot be answered, with enough detail to say why."""

    def __init__(self, message: str, *, blocking_metric: str | None = None, **detail):
        super().__init__(message)
        self.message = message
        self.blocking_metric = blocking_metric
        self.detail = detail

    def as_dict(self) -> dict:
        return {
            "error": self.message,
            "blocking_metric": self.blocking_metric,
            **self.detail,
        }


class UnknownMetricError(QueryError):
    pass


class NoLevelWithCoverageError(QueryError):
    pass


def registered_metrics(conn: psycopg.Connection) -> dict[str, dict]:
    """The live metric registry, keyed by metric name."""
    rows = conn.execute(
        """
        SELECT metric_name, label, unit, description, source
        FROM metric_definitions
        ORDER BY metric_name
        """
    ).fetchall()
    return {row["metric_name"]: dict(row) for row in rows}


def coverage_map(conn: psycopg.Connection) -> dict[str, dict[str, float]]:
    """{metric_name: {level: coverage_pct}} from metric_availability."""
    rows = conn.execute(
        "SELECT metric_name, level, coverage_pct FROM metric_availability"
    ).fetchall()
    coverage: dict[str, dict[str, float]] = {}
    for row in rows:
        coverage.setdefault(row["metric_name"], {})[row["level"]] = row["coverage_pct"]
    return coverage


def _requested_metrics(geo_filter: GeoFilter) -> list[str]:
    """Every metric the query touches: the conditions, plus sort_by if it adds one."""
    metrics = [c.metric for c in geo_filter.conditions]
    if geo_filter.sort_by and geo_filter.sort_by not in metrics:
        metrics.append(geo_filter.sort_by)
    return metrics


def validate_metrics(conn: psycopg.Connection, geo_filter: GeoFilter) -> None:
    """Reject unknown metrics before anything else happens.

    This is also the server-side re-validation the natural language path relies
    on: whatever produced the GeoFilter, its metrics must exist in the live
    registry.
    """
    known = registered_metrics(conn)
    unknown = [m for m in _requested_metrics(geo_filter) if m not in known]
    if unknown:
        raise UnknownMetricError(
            f"unknown metric(s): {', '.join(sorted(unknown))}",
            blocking_metric=sorted(unknown)[0],
            known_metrics=sorted(known),
        )


def resolve_level(
    conn: psycopg.Connection,
    geo_filter: GeoFilter,
    threshold: float | None = None,
) -> tuple[Level, str]:
    """Decide which level to run at, or refuse and say what blocked it.

    With `level="auto"`, the most granular level where every requested metric
    clears the coverage threshold wins. With an explicit level, that level is
    held to the same threshold: a city query naming GDP per capita is a
    mistake worth reporting, not a query worth running against 0% coverage.
    """
    threshold = config.COVERAGE_THRESHOLD_PCT if threshold is None else threshold
    metrics = _requested_metrics(geo_filter)
    coverage = coverage_map(conn)

    def blocking(level: str) -> tuple[str, float] | None:
        """The worst metric at this level, if any falls short."""
        worst: tuple[str, float] | None = None
        for metric in metrics:
            pct = coverage.get(metric, {}).get(level, 0.0)
            if pct < threshold and (worst is None or pct < worst[1]):
                worst = (metric, pct)
        return worst

    if geo_filter.level != "auto":
        level = geo_filter.level
        worst = blocking(level)
        if worst is not None:
            metric, pct = worst
            available = _levels_with_data(coverage, metric, threshold)
            elsewhere = (
                f"; it is available at: {', '.join(available)}"
                if available
                else "; it is not available at any level"
            )
            # A metric that is not measured at this level and one that is
            # measured too thinly are different answers. Reporting "0.0%
            # coverage" for the first invites the reader to think the import
            # broke, when in fact no such data exists at that level.
            message = (
                f"{metric} is not measured at {level} level{elsewhere}"
                if pct == 0
                else (
                    f"{metric} covers only {pct:.1f}% of {level}s, below the "
                    f"{threshold:.0f}% threshold required to query it there{elsewhere}"
                )
            )
            raise NoLevelWithCoverageError(
                message,
                blocking_metric=metric,
                level=level,
                coverage_pct=pct,
                threshold_pct=threshold,
                available_levels=available,
            )
        return level, "explicit"

    # Most granular first.
    for level in reversed(config.LEVELS):
        if blocking(level) is None and region_count(conn, level) > 0:
            return level, "auto"  # type: ignore[return-value]

    # Nothing cleared. Report the metric that fell short at the most levels,
    # with the best coverage it manages anywhere, so the caller learns whether
    # the metric is thin or simply absent.
    best_per_metric = {
        metric: max(coverage.get(metric, {}).values(), default=0.0) for metric in metrics
    }
    metric, pct = min(best_per_metric.items(), key=lambda kv: kv[1])
    raise NoLevelWithCoverageError(
        f"no level has data for every requested metric: {metric} reaches at most "
        f"{pct:.1f}% coverage, below the {threshold:.0f}% threshold",
        blocking_metric=metric,
        coverage_by_metric=best_per_metric,
        threshold_pct=threshold,
    )


def _levels_with_data(
    coverage: dict[str, dict[str, float]], metric: str, threshold: float
) -> list[str]:
    """The levels where `metric` clears the threshold, most general first."""
    per_level = coverage.get(metric, {})
    return [level for level in config.LEVELS if per_level.get(level, 0.0) >= threshold]


# How many regions a level has, cached in the process.
#
# This number is used for exactly one thing: choosing between two SQL shapes
# that return identical rows (see `_prefilter`). A stale count can therefore
# only produce a slightly worse query plan, never a wrong answer — which is why
# it needs no invalidation hook, unlike the basemap cache, where staleness would
# mean showing boundaries an ETL run has already replaced.
_LEVEL_COUNT_TTL_S = 300.0
_level_counts: dict[str, tuple[float, int]] = {}
_level_counts_lock = threading.Lock()


def region_count(conn: psycopg.Connection, level: str) -> int:
    now = time.monotonic()
    with _level_counts_lock:
        cached = _level_counts.get(level)
        if cached is not None and now - cached[0] < _LEVEL_COUNT_TTL_S:
            return cached[1]

    count = conn.execute(
        "SELECT count(*) AS n FROM regions WHERE level = %s", (level,)
    ).fetchone()["n"]

    with _level_counts_lock:
        _level_counts[level] = (now, count)
    return count


def _forget_level_counts() -> None:
    """Drop the cached counts. For tests that add regions mid-session."""
    with _level_counts_lock:
        _level_counts.clear()


def build_sql(
    geo_filter: GeoFilter, level: str, region_count: int = 0
) -> tuple[sql.Composed, dict]:
    """Compose the SELECT and its bound parameters.

    One lateral join per metric picks that region's most recent value, so a
    metric that later grows several vintages cannot multiply result rows.

    Two shapes here are about cost, not meaning. Both were measured against the
    loaded database, and the numbers are the reason each is written this way:

      * The parent name is joined *after* the LIMIT. It is display text for the
        rows that survive, so looking it up for every candidate is work thrown
        away; joining it last runs it top_n times instead of tens of thousands.
        Unconditionally a win or a wash: city 85 ms -> 63 ms, country 2.2 ->
        1.8 ms.
      * On a big level each condition also contributes an EXISTS pre-filter on
        `metrics`, which lets the planner range-scan idx_metrics_name_value
        straight to the regions that could qualify instead of walking every
        region at the level. See `_prefilter` for why it cannot change the
        answer.

    The pre-filter is applied only above `PREFILTER_MIN_REGIONS`, because it is
    not free and only pays for itself once there are many regions to skip. At
    34,026 cities it takes a two-condition query from 63 ms to 24 ms and a
    three-condition one from 84 ms to 46 ms; at 258 countries the same shape
    costs 2.3 ms -> 6.7 ms, since scanning every country is cheaper than the
    index range it would save. The threshold sits between the state tier (4,596,
    where it still loses) and the city tier (where it wins clearly), so each
    level gets the shape that suits its size and a future county tier picks up
    the fast path on its own.
    """
    metrics = _requested_metrics(geo_filter)
    aliases = metric_aliases(geo_filter)
    prefilter = region_count >= config.PREFILTER_MIN_REGIONS
    params: dict[str, object] = {"level": level, "top_n": geo_filter.top_n}

    joins: list[sql.Composed] = []
    for metric, alias in aliases.items():
        key = f"metric_{alias}"
        params[key] = metric
        joins.append(
            sql.SQL(
                """
                JOIN LATERAL (
                    SELECT value, year FROM metrics
                    WHERE region_id = r.id AND metric_name = {metric} AND value IS NOT NULL
                    ORDER BY year DESC
                    LIMIT 1
                ) AS {alias} ON TRUE
                """
            ).format(metric=sql.Placeholder(key), alias=sql.Identifier(alias))
        )

    wheres: list[sql.Composed] = [sql.SQL("r.level = {}").format(sql.Placeholder("level"))]
    for i, condition in enumerate(geo_filter.conditions):
        key = f"value_{i}"
        params[key] = condition.value
        wheres.append(
            sql.SQL("{alias}.value {op} {value}").format(
                alias=sql.Identifier(aliases[condition.metric]),
                op=sql.SQL(_OPERATORS[condition.op]),
                value=sql.Placeholder(key),
            )
        )
        if prefilter:
            wheres.append(
                _prefilter(
                    alias=aliases[condition.metric],
                    metric_key=f"metric_{aliases[condition.metric]}",
                    op=condition.op,
                    value_key=key,
                )
            )

    sort_metric = geo_filter.sort_by or geo_filter.conditions[0].metric
    inner_selected = sql.SQL(", ").join(
        sql.SQL("{alias}.value AS {value_col}, {alias}.year AS {year_col}").format(
            alias=sql.Identifier(alias),
            value_col=sql.Identifier(f"{alias}_value"),
            year_col=sql.Identifier(f"{alias}_year"),
        )
        for alias in aliases.values()
    )
    outer_selected = sql.SQL(", ").join(
        sql.SQL("t.{value_col}, t.{year_col}").format(
            value_col=sql.Identifier(f"{alias}_value"),
            year_col=sql.Identifier(f"{alias}_year"),
        )
        for alias in aliases.values()
    )

    order = sql.SQL("DESC" if geo_filter.order == "desc" else "ASC")
    sort_value_col = sql.Identifier(f"{aliases[sort_metric]}_value")

    statement = sql.SQL(
        """
        SELECT t.id, t.name, t.level, p.name AS parent_name, {outer_selected}
        FROM (
            SELECT r.id, r.name, r.level, r.parent_id, {inner_selected}
            FROM regions r
            {joins}
            WHERE {wheres}
            ORDER BY {sort_alias}.value {order}, r.name ASC
            LIMIT {top_n}
        ) AS t
        LEFT JOIN regions p ON p.id = t.parent_id
        ORDER BY t.{sort_value_col} {order}, t.name ASC
        """
    ).format(
        outer_selected=outer_selected,
        inner_selected=inner_selected,
        joins=sql.SQL(" ").join(joins),
        wheres=sql.SQL(" AND ").join(wheres),
        sort_alias=sql.Identifier(aliases[sort_metric]),
        sort_value_col=sort_value_col,
        order=order,
        top_n=sql.Placeholder("top_n"),
    )
    return statement, params


def _prefilter(*, alias: str, metric_key: str, op: str, value_key: str) -> sql.Composed:
    """A redundant EXISTS that narrows the driving set. Never changes it.

    The lateral join takes a region's most recent non-null value and the outer
    WHERE tests that one value. This adds "and the region has *some* row for
    this metric passing the same test", which is implied by it: if the latest
    value passes, that row is itself a witness. So no region the real filter
    would keep can be excluded here.

    It can over-include — an older vintage passing where the latest does not —
    and those rows are then rejected by the outer WHERE exactly as before. The
    pre-filter is a hint to the planner, never the decision. Because it is
    written from the same `condition.op` and the same bound value as the real
    test, the two cannot drift apart.
    """
    probe = sql.Identifier(f"pre_{alias}")
    return sql.SQL(
        """
        EXISTS (
            SELECT 1 FROM metrics AS {probe}
            WHERE {probe}.region_id = r.id
              AND {probe}.metric_name = {metric}
              AND {probe}.value {op} {value}
        )
        """
    ).format(
        probe=probe,
        metric=sql.Placeholder(metric_key),
        op=sql.SQL(_OPERATORS[op]),
        value=sql.Placeholder(value_key),
    )


def metric_aliases(geo_filter: GeoFilter) -> dict[str, str]:
    """Column alias per requested metric. One definition, used to build the
    statement and to read the rows back, so the two cannot drift apart."""
    return {metric: f"m{i}" for i, metric in enumerate(_requested_metrics(geo_filter))}


def run(geo_filter: GeoFilter, conn: psycopg.Connection | None = None) -> QueryResult:
    """Validate, resolve the level, and execute. The one execution path."""
    if conn is None:
        with db.connect() as owned:
            return run(geo_filter, owned)

    validate_metrics(conn, geo_filter)
    level, chosen_by = resolve_level(conn, geo_filter)
    statement, params = build_sql(geo_filter, level, region_count(conn, level))

    # The planner costs this shape at ~580,000 on the city tier, well past
    # jit_above_cost, so Postgres JIT-compiles it — 172 ms of compilation for
    # ~130 ms of execution. That trade is right for the ETL's aggregate over
    # 8.5 million river segments and wrong for every query on this path, which
    # is short by design. LOCAL scopes it to this transaction, so the pooled
    # connection goes back unchanged and the ETL still gets its JIT.
    conn.execute("SET LOCAL jit = off")
    rows = conn.execute(statement, params).fetchall()

    aliases = metric_aliases(geo_filter)
    results = [
        ResultRow(
            region_id=row["id"],
            name=row["name"],
            level=row["level"],
            parent_name=row["parent_name"],
            metrics={
                metric: MetricValue(
                    value=row[f"{alias}_value"], year=row[f"{alias}_year"]
                )
                for metric, alias in aliases.items()
            },
        )
        for row in rows
    ]
    log.info(
        "query at %s level (%s): %d conditions -> %d results",
        level,
        chosen_by,
        len(geo_filter.conditions),
        len(results),
    )
    return QueryResult(
        level=level,
        level_chosen_by=chosen_by,
        count=len(results),
        results=results,
        applied_filter=geo_filter,
    )
