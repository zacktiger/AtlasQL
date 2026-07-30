"""The `GeoFilter` contract.

This is the single input type of the query engine. The structured UI, natural
language parsing and any future input path all emit this same model, and the
SQL builder accepts nothing else. Adding an input path means producing a
GeoFilter; it never means touching the engine.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["continent", "country", "state", "county", "city"]
LevelOrAuto = Literal["continent", "country", "state", "county", "city", "auto"]
Operator = Literal[">", "<", ">=", "<=", "=="]


class Condition(BaseModel):
    metric: str
    op: Operator
    value: float


class GeoFilter(BaseModel):
    level: LevelOrAuto = "auto"
    conditions: list[Condition] = Field(min_length=1)
    sort_by: str | None = None
    order: Literal["asc", "desc"] = "desc"
    top_n: int = Field(default=10, ge=1, le=100)


class MetricValue(BaseModel):
    value: float
    # Which vintage this number came from. Metrics that are not time series
    # still carry the year of the release they were taken from, so a result is
    # never a mix of vintages without saying so.
    year: int


class ResultRow(BaseModel):
    region_id: int
    name: str
    level: Level
    parent_name: str | None
    metrics: dict[str, MetricValue]


class QueryResult(BaseModel):
    level: Level
    # "auto" when the engine picked the level from coverage, "explicit" when the
    # caller named it.
    level_chosen_by: Literal["auto", "explicit"]
    count: int
    results: list[ResultRow]
    applied_filter: GeoFilter
