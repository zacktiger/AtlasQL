"""Natural language -> GeoFilter, via Claude tool use.

This is a convenience layer over the existing engine, never the engine itself.
Claude's only job is to emit a GeoFilter; it never sees the database, never
writes SQL, and never executes anything. The object it returns goes through the
same Pydantic model and the same server-side validation as a filter built by
hand in the UI, and then through the same SQL builder.

Three things keep that boundary honest:

  * The tool schema is generated from the live metric registry, so the metric
    field is an enum of names that actually exist. Combined with strict tool
    use, a hallucinated metric cannot be represented in the response at all.
  * `tool_choice` is forced, so the model cannot reply with prose instead of a
    filter.
  * The result is re-validated server-side anyway - parsed by the GeoFilter
    model, then checked against the registry - because a schema is a constraint
    on the model, not a guarantee about our own code paths.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import psycopg

from atlasql import config, query
from atlasql.models import GeoFilter

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
TOOL_NAME = "emit_geo_filter"

# Extraction against a fixed schema is not a reasoning-heavy task, and this
# call sits in front of a user waiting to see their query.
EFFORT = "low"

# Thinking is left on. On this model, disabling it can cause a tool call to be
# written into the visible text instead of a tool_use block - the turn succeeds,
# the call never happens, and nothing errors. A forced tool_choice is exactly
# the shape that failure mode ruins, so the latency is worth it; effort is the
# cost lever instead.
MAX_TOKENS = 8192

SYSTEM_PROMPT = """\
You translate a user's geography question into a GeoFilter for AtlasQL, a query
engine over the world's administrative hierarchy.

Rules:
- Emit exactly one call to the {tool} tool. Never answer in prose.
- Use only the metrics listed below. If the question needs a metric that is not
  listed, pick the closest listed one and leave the rest of the filter faithful
  to the question; the server will tell the user what is missing.
- Leave `level` as "auto" unless the user names a tier (countries, states,
  cities). "auto" picks the most detailed level where every metric has data.
- Convert units to the metric's own unit before emitting a value: "40k" is
  40000, "2,000 m" is 2000, "5 million people" is 5000000.
- `sort_by` should be the metric the user ranks by ("highest", "largest",
  "top"). Leave it null when they do not rank.
- `order` is "desc" for highest/most/largest and "asc" for lowest/least.
- `top_n` is 10 unless the user asks for a different count.

Metrics available:
{metrics}

Levels: {levels}
"""


class ParserUnavailable(RuntimeError):
    """No API credentials configured, so the parser cannot run."""


class ParseFailed(RuntimeError):
    """The model did not return a usable filter."""


def _metric_catalogue(registry: dict[str, dict]) -> str:
    lines = []
    for name, info in sorted(registry.items()):
        unit = f" ({info['unit']})" if info.get("unit") else ""
        lines.append(f"- {name}{unit}: {info['label']}. {info.get('description') or ''}".strip())
    return "\n".join(lines)


def build_tool(metric_names: list[str]) -> dict[str, Any]:
    """The GeoFilter tool schema, with metrics pinned to the live registry.

    `strict` is set, which requires every property to be listed in `required`
    and `additionalProperties` to be false. Optional fields are expressed as a
    nullable union rather than by omission, since under strict tool use the
    model must supply every key.
    """
    metric_enum = {"type": "string", "enum": metric_names}
    return {
        "name": TOOL_NAME,
        "description": (
            "Emit the structured filter that answers the user's question. The "
            "filter is shown to the user for review before it runs."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "conditions", "sort_by", "order", "top_n"],
            "properties": {
                "level": {
                    "type": "string",
                    "enum": [*config.LEVELS, "auto"],
                    "description": "Tier to query; 'auto' unless the user names one.",
                },
                "conditions": {
                    "type": "array",
                    "description": "Numeric conditions, all of which must hold.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["metric", "op", "value"],
                        "properties": {
                            "metric": metric_enum,
                            "op": {"type": "string", "enum": [">", "<", ">=", "<=", "=="]},
                            "value": {"type": "number"},
                        },
                    },
                },
                "sort_by": {
                    "anyOf": [metric_enum, {"type": "null"}],
                    "description": "Metric to rank by, or null.",
                },
                "order": {"type": "string", "enum": ["desc", "asc"]},
                "top_n": {"type": "integer", "description": "How many results, 1 to 100."},
            },
        },
    }


# Constructing the SDK client resolves credentials, which measured 371 ms — it
# looks beyond the environment variable for a CLI profile and federated
# credentials. /metadata calls is_configured() on every page load just to decide
# whether to show the natural language box, so paying that per request made the
# frontend's first fetch four times slower than every query it can run.
#
# The client is cached instead. It is reusable and thread-safe, so this also
# gives /parse a warm HTTP connection pool rather than a fresh one per parse.
# The TTL exists for the one case where the answer can change under a running
# process: a developer running `ant auth login` mid-session. In a deployment
# credentials come from the environment and are fixed until the process
# restarts, so this only ever re-resolves an answer that cannot have changed.
_CREDENTIAL_TTL_S = 60.0
_credential_lock = threading.Lock()
_cached_client: tuple[float, Any | None, ParserUnavailable | None] | None = None


def _resolve_client():
    """Construct a client, or the ParserUnavailable explaining why not.

    The SDK constructs happily with no credentials and only fails when it comes
    to build the request headers, so constructing is not a check. The resolved
    attributes are: an unset env var is not proof of nothing, because the SDK
    also picks up an `ant auth login` profile or federated credentials, and all
    of those land here.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        return None, ParserUnavailable("the anthropic package is not installed")

    client = anthropic.Anthropic()
    if not any(
        getattr(client, attribute, None)
        for attribute in ("api_key", "auth_token", "credentials")
    ):
        return None, ParserUnavailable(
            "no Anthropic credentials available; set ANTHROPIC_API_KEY to enable "
            "natural language queries"
        )
    return client, None


def _client():
    """The cached Anthropic client, or raise ParserUnavailable."""
    global _cached_client
    now = time.monotonic()
    with _credential_lock:
        cached = _cached_client
        if cached is not None and now - cached[0] < _CREDENTIAL_TTL_S:
            if cached[2] is not None:
                raise cached[2]
            return cached[1]

    client, unavailable = _resolve_client()

    with _credential_lock:
        _cached_client = (now, client, unavailable)
    if unavailable is not None:
        raise unavailable
    return client


def _forget_client() -> None:
    """Drop the cached client, so a test can change the environment."""
    global _cached_client
    with _credential_lock:
        _cached_client = None


def _translate(exc: Exception) -> Exception:
    """Map an SDK failure onto ours, so the API answers with the right status.

    Anything about credentials is the deployment's problem to fix and belongs
    in ParserUnavailable (503); everything else is this request failing and
    belongs in ParseFailed (502).
    """
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ParserUnavailable(f"Anthropic rejected the credentials: {exc}")
    # Raised while building headers when no credential resolved at all.
    if isinstance(exc, TypeError) and "authentication method" in str(exc):
        return ParserUnavailable(
            "no Anthropic credentials available; set ANTHROPIC_API_KEY to enable "
            "natural language queries"
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ParseFailed("the parser is rate limited; try again shortly")
    if isinstance(exc, anthropic.APIConnectionError):
        return ParseFailed(f"could not reach the Anthropic API: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        return ParseFailed(f"the Anthropic API returned {exc.status_code}: {exc}")
    return exc


def _extract_tool_input(response) -> dict:
    """Pull the filter out of the response, or say what came back instead."""
    # Safety classifiers can decline a request; that arrives as a normal
    # response with an empty or partial body rather than an exception.
    if getattr(response, "stop_reason", None) == "refusal":
        raise ParseFailed("the request was declined by the model's safety system")

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return dict(block.input)

    text = " ".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    raise ParseFailed(f"the model returned no filter{': ' + text if text else ''}")


def parse(text: str, conn: psycopg.Connection | None = None) -> GeoFilter:
    """Turn a natural language question into a validated GeoFilter.

    Does not execute anything. The caller gets a filter to show the user.
    """
    if conn is None:
        from atlasql import db

        with db.connect() as owned:
            return parse(text, owned)

    registry = query.registered_metrics(conn)
    if not registry:
        raise ParseFailed("no metrics are registered, so nothing can be parsed")

    tool = build_tool(sorted(registry))
    system = SYSTEM_PROMPT.format(
        tool=TOOL_NAME,
        metrics=_metric_catalogue(registry),
        levels=", ".join(config.LEVELS),
    )

    client = _client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            output_config={"effort": EFFORT},
            tools=[tool],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": text}],
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as one of ours below
        raise _translate(exc) from exc

    raw = _extract_tool_input(response)
    log.info("parsed %r -> %s", text, json.dumps(raw, sort_keys=True))

    # Re-validation, in this order on purpose: the Pydantic model rejects a
    # malformed shape, then the registry check rejects a metric that does not
    # exist. Neither trusts the tool schema to have held.
    geo_filter = GeoFilter.model_validate(raw)
    query.validate_metrics(conn, geo_filter)
    return geo_filter


def is_configured() -> bool:
    """Whether a live parse could run, for /metadata to advertise."""
    try:
        _client()
    except ParserUnavailable:
        return False
    return True
