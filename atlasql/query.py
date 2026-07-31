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
        if blocking(level) is None and _level_has_regions(conn, level):
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


def _level_has_regions(conn: psycopg.Connection, level: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM regions WHERE level = %s LIMIT 1", (level,)
        ).fetchone()
    )


def build_sql(geo_filter: GeoFilter, level: str) -> tuple[sql.Composed, dict]:
    """Compose the SELECT and its bound parameters.

    One lateral join per metric picks that region's most recent value, so a
    metric that later grows several vintages cannot multiply result rows.
    """
    metrics = _requested_metrics(geo_filter)
    aliases = {metric: f"m{i}" for i, metric in enumerate(metrics)}
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

    sort_metric = geo_filter.sort_by or geo_filter.conditions[0].metric
    selected = sql.SQL(", ").join(
        sql.SQL("{alias}.value AS {value_col}, {alias}.year AS {year_col}").format(
            alias=sql.Identifier(alias),
            value_col=sql.Identifier(f"{alias}_value"),
            year_col=sql.Identifier(f"{alias}_year"),
        )
        for alias in aliases.values()
    )

    statement = sql.SQL(
        """
        SELECT r.id, r.name, r.level, p.name AS parent_name, {selected}
        FROM regions r
        LEFT JOIN regions p ON p.id = r.parent_id
        {joins}
        WHERE {wheres}
        ORDER BY {sort_alias}.value {order}, r.name ASC
        LIMIT {top_n}
        """
    ).format(
        selected=selected,
        joins=sql.SQL(" ").join(joins),
        wheres=sql.SQL(" AND ").join(wheres),
        sort_alias=sql.Identifier(aliases[sort_metric]),
        order=sql.SQL("DESC" if geo_filter.order == "desc" else "ASC"),
        top_n=sql.Placeholder("top_n"),
    )
    return statement, params


def run(geo_filter: GeoFilter, conn: psycopg.Connection | None = None) -> QueryResult:
    """Validate, resolve the level, and execute. The one execution path."""
    if conn is None:
        with db.connect() as owned:
            return run(geo_filter, owned)

    validate_metrics(conn, geo_filter)
    level, chosen_by = resolve_level(conn, geo_filter)
    statement, params = build_sql(geo_filter, level)
    rows = conn.execute(statement, params).fetchall()

    metrics = _requested_metrics(geo_filter)
    aliases = {metric: f"m{i}" for i, metric in enumerate(metrics)}
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
