"""The natural language layer.

These tests drive the parser with a stubbed Anthropic client, so they run
without an API key and without spending anything. What they check is not
whether the model is good at parsing - that is the model's job - but that the
boundary around it holds: forced tool use, a schema built from the live
registry, and server-side re-validation of whatever comes back.

One test does hit the real API, and skips unless ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from atlasql import parser, query
from atlasql.api import app


class FakeToolUse(SimpleNamespace):
    type = "tool_use"


class FakeText(SimpleNamespace):
    type = "text"


def fake_response(tool_input=None, text=None, stop_reason="tool_use"):
    content = []
    if tool_input is not None:
        content.append(FakeToolUse(name=parser.TOOL_NAME, input=tool_input, id="tu_1"))
    if text is not None:
        content.append(FakeText(text=text))
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeClient:
    """Records the request and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def registry(schema):
    metrics = query.registered_metrics(schema)
    if not metrics:
        pytest.skip("no metrics registered")
    return metrics


def install_client(monkeypatch, response) -> FakeClient:
    client = FakeClient(response)
    monkeypatch.setattr(parser, "_client", lambda: client)
    return client


def test_tool_schema_is_built_from_the_live_registry(registry):
    tool = parser.build_tool(sorted(registry))
    schema = tool["input_schema"]
    metric_enum = schema["properties"]["conditions"]["items"]["properties"]["metric"]["enum"]

    # The model can only name metrics that exist, because the schema says so.
    assert metric_enum == sorted(registry)
    assert "unicorn_density" not in metric_enum

    # Strict tool use requires both of these; without them the API rejects it.
    assert tool["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_parse_forces_the_tool_and_never_asks_for_prose(monkeypatch, registry, schema):
    client = install_client(
        monkeypatch,
        fake_response(
            {
                "level": "auto",
                "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 40000}],
                "sort_by": None,
                "order": "desc",
                "top_n": 10,
            }
        ),
    )

    result = parser.parse("rich countries", schema)

    assert result.conditions[0].metric == "gdp_per_capita"
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": parser.TOOL_NAME}
    assert call["model"] == "claude-opus-5"
    # Thinking is left on: with it disabled this model can write a tool call
    # into plain text, which a forced tool_choice would then never receive.
    assert "thinking" not in call or call["thinking"]["type"] != "disabled"


def test_the_prompt_carries_the_real_metric_catalogue(monkeypatch, registry, schema):
    client = install_client(
        monkeypatch,
        fake_response(
            {
                "level": "auto",
                "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 1}],
                "sort_by": None,
                "order": "desc",
                "top_n": 10,
            }
        ),
    )
    parser.parse("anything", schema)
    system = client.calls[0]["system"]
    for name in registry:
        assert name in system


def test_a_hallucinated_metric_is_rejected_server_side(monkeypatch, registry, schema):
    """The schema forbids it, so this is belt and braces - which is the point."""
    install_client(
        monkeypatch,
        fake_response(
            {
                "level": "auto",
                "conditions": [{"metric": "unicorn_density", "op": ">", "value": 1}],
                "sort_by": None,
                "order": "desc",
                "top_n": 10,
            }
        ),
    )
    with pytest.raises(query.UnknownMetricError) as excinfo:
        parser.parse("countries with many unicorns", schema)
    assert excinfo.value.blocking_metric == "unicorn_density"


def test_a_malformed_filter_is_rejected_before_it_reaches_the_engine(
    monkeypatch, registry, schema
):
    install_client(
        monkeypatch,
        fake_response(
            {
                "level": "atlantis",  # not a level
                "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 1}],
                "sort_by": None,
                "order": "desc",
                "top_n": 10,
            }
        ),
    )
    with pytest.raises(Exception) as excinfo:  # pydantic ValidationError
        parser.parse("countries in atlantis", schema)
    assert "level" in str(excinfo.value)


def test_a_refusal_is_reported_not_treated_as_an_empty_filter(
    monkeypatch, registry, schema
):
    install_client(monkeypatch, fake_response(stop_reason="refusal"))
    with pytest.raises(parser.ParseFailed) as excinfo:
        parser.parse("something the model declines", schema)
    assert "declined" in str(excinfo.value)


def test_prose_instead_of_a_tool_call_is_an_error(monkeypatch, registry, schema):
    install_client(monkeypatch, fake_response(text="I'm not sure what you mean."))
    with pytest.raises(parser.ParseFailed) as excinfo:
        parser.parse("???", schema)
    assert "no filter" in str(excinfo.value)


def test_parse_endpoint_does_not_execute_the_filter(monkeypatch, registry, schema):
    """/parse returns a filter for review; running it is a separate call."""
    parsed = {
        "level": "auto",
        "conditions": [{"metric": "gdp_per_capita", "op": ">", "value": 40000}],
        "sort_by": None,
        "order": "desc",
        "top_n": 10,
    }
    install_client(monkeypatch, fake_response(parsed))

    response = TestClient(app).post("/parse", json={"text": "rich countries"})
    assert response.status_code == 200
    body = response.json()

    assert body["filter"]["conditions"][0]["metric"] == "gdp_per_capita"
    # No results anywhere in the response: nothing ran.
    assert "results" not in body
    assert "count" not in body


def test_parse_endpoint_reports_missing_credentials_as_503(monkeypatch, schema):
    def unavailable():
        raise parser.ParserUnavailable("no Anthropic credentials available")

    monkeypatch.setattr(parser, "_client", unavailable)
    response = TestClient(app).post("/parse", json={"text": "rich countries"})
    assert response.status_code == 503
    assert "credentials" in response.json()["detail"]


def test_missing_credentials_are_detected_before_the_request(monkeypatch):
    """The SDK constructs fine with no credentials and only fails on send.

    Constructing a client is therefore not a check, and treating it as one made
    /metadata advertise the feature and /parse answer 500 instead of 503.
    """
    import anthropic

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: SimpleNamespace(
        api_key=None, auth_token=None, credentials=None
    ))
    assert parser.is_configured() is False
    with pytest.raises(parser.ParserUnavailable):
        parser._client()


def test_sdk_failures_map_onto_the_right_status(monkeypatch):
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    auth_error = anthropic.AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )
    assert isinstance(parser._translate(auth_error), parser.ParserUnavailable)

    rate_limited = anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )
    assert isinstance(parser._translate(rate_limited), parser.ParseFailed)

    # The bare TypeError the SDK raises when no credential resolves at all.
    no_creds = TypeError("Could not resolve authentication method. Expected one of ...")
    assert isinstance(parser._translate(no_creds), parser.ParserUnavailable)


def test_parse_endpoint_rejects_empty_text(schema):
    response = TestClient(app).post("/parse", json={"text": "   "})
    assert response.status_code == 422


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live parse needs ANTHROPIC_API_KEY",
)
def test_live_parse_round_trips_a_real_question(schema):
    """The only test that spends money. Skipped unless a key is present."""
    geo_filter = parser.parse(
        "cities above 2000 metres with more than half a million people", schema
    )
    metrics = {c.metric for c in geo_filter.conditions}
    assert "elevation_mean" in metrics
    assert "population" in metrics
    result = query.run(geo_filter, schema)
    assert result.level == "city"
