"""Tests for core.llm_client (NFR-2/3/4, FR-14). No network: a fake HTTP server."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel

from config.settings import Settings
from core.llm_client import LLMClient, LLMError, parse_json_text


class Answer(BaseModel):
    word: str
    count: int


def _settings(**overrides) -> Settings:
    base = Settings(
        llm_base_url="https://primary.test/v1/", llm_api_key="primary-key",
        vision_model="m-primary", copy_model="m-primary",
        fallback_base_url="https://fallback.test/v1", fallback_api_key="",
        fallback_models=[], max_requests_per_min=0,
        reference_input_price_per_mtok=1.0, reference_output_price_per_mtok=1.0,
    )
    return replace(base, **overrides)


def _reply(content: str, prompt_tokens: int = 100, completion_tokens: int = 50) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


class FakeServer:
    """Plays back a list of responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)


def _client(server: FakeServer, **settings) -> tuple[LLMClient, list[float]]:
    sleeps: list[float] = []
    client = LLMClient(_settings(**settings), transport=httpx.MockTransport(server),
                       sleep=sleeps.append)
    return client, sleeps


def _ask(client: LLMClient, **kw):
    return client.complete_json(step="test", model="m-primary", system="sys",
                                user="Give me JSON", schema=Answer, **kw)


# --------------------------------------------------------------------------- #

def test_success_returns_validated_model_and_usage():
    server = FakeServer(_reply('{"word": "hi", "count": 2}', 1000, 500))
    client, _ = _client(server)
    answer, usage = _ask(client)
    assert answer == Answer(word="hi", count=2)
    assert (usage.prompt_tokens, usage.completion_tokens) == (1000, 500)
    assert usage.model == "m-primary" and usage.step == "test"
    assert usage.estimated_cost_usd == pytest.approx(1500 / 1_000_000 * 1.0)
    request = server.requests[0]
    assert str(request.url) == "https://primary.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer primary-key"
    assert server.body(0)["model"] == "m-primary"


def test_input_and_output_tokens_are_priced_separately():
    # Output tokens cost ~6x more than input tokens on paid plans (FR-14).
    server = FakeServer(_reply('{"word": "hi", "count": 2}', 1000, 500))
    client, _ = _client(server, reference_input_price_per_mtok=2.0,
                        reference_output_price_per_mtok=10.0)
    _, usage = _ask(client)
    assert usage.estimated_cost_usd == pytest.approx((1000 * 2.0 + 500 * 10.0) / 1_000_000)


def test_image_is_sent_as_a_jpeg_data_url():
    server = FakeServer(_reply('{"word": "x", "count": 1}'))
    client, _ = _client(server)
    _ask(client, image=Image.new("RGBA", (2000, 1000), (255, 0, 0, 128)))
    parts = server.body(0)["messages"][1]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize("text", [
    '```json\n{"word": "a", "count": 1}\n```',
    'Sure! Here it is: {"word": "a", "count": 1} Hope that helps.',
])
def test_json_is_extracted_from_fenced_or_chatty_replies(text):
    assert parse_json_text(text) == {"word": "a", "count": 1}


def test_rate_limit_429_waits_for_retry_after_then_succeeds():
    server = FakeServer(
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow down"}),
        _reply('{"word": "ok", "count": 1}'),
    )
    client, sleeps = _client(server)
    answer, _ = _ask(client)
    assert answer.word == "ok"
    assert 7 in sleeps


def test_5xx_and_connection_errors_are_retried():
    server = FakeServer(
        httpx.ConnectError("reset"),
        httpx.Response(503, json={"error": "busy"}),
        _reply('{"word": "ok", "count": 1}'),
    )
    client, _ = _client(server)
    assert _ask(client)[0].word == "ok"
    assert len(server.requests) == 3


def test_timeout_moves_to_the_next_model_without_retrying():
    # Seen live: an overloaded model times out repeatedly; 3 x 60 s retries wasted 3 minutes.
    server = FakeServer(httpx.ReadTimeout("slow"), _reply('{"word": "next", "count": 1}'))
    client, sleeps = _client(server, llm_fallback_models=["m-second"])
    answer, usage = _ask(client)
    assert answer.word == "next" and usage.model == "m-second"
    assert len(server.requests) == 2 and sleeps == []


def test_same_provider_fallback_models_use_the_primary_credentials():
    # Seen live: gemini-2.5-flash returned 404 "no longer available to new users".
    server = FakeServer(httpx.Response(404, json={"error": "gone"}),
                        _reply('{"word": "ok", "count": 1}'))
    client, _ = _client(server, llm_fallback_models=["m-second"])
    _, usage = _ask(client)
    assert usage.model == "m-second" and usage.provider == "primary.test"
    second = server.requests[1]
    assert str(second.url).startswith("https://primary.test/")
    assert second.headers["Authorization"] == "Bearer primary-key"
    assert json.loads(second.content)["model"] == "m-second"


def test_fallback_order_is_primary_then_same_provider_then_other_provider():
    server = FakeServer(httpx.Response(404), httpx.Response(404),
                        _reply('{"word": "last", "count": 1}'))
    client, _ = _client(server, llm_fallback_models=["m-second"],
                        fallback_api_key="fb-key", fallback_models=["fb:free"])
    _, usage = _ask(client)
    assert [json.loads(r.content)["model"] for r in server.requests] == [
        "m-primary", "m-second", "fb:free"]


def test_invalid_json_gets_one_repair_attempt_with_the_error():
    server = FakeServer(
        _reply('{"word": "ok"}'),                    # missing "count"
        _reply('{"word": "ok", "count": 3}'),
    )
    client, _ = _client(server)
    answer, usage = _ask(client)
    assert answer.count == 3
    repair_prompt = server.body(1)["messages"][-1]["content"]
    assert "count" in repair_prompt                   # the validation error is fed back
    assert usage.prompt_tokens == 200                 # both calls are counted


def test_primary_exhausted_falls_back_to_the_next_model():
    server = FakeServer(
        httpx.Response(500), httpx.Response(500), httpx.Response(500),
        _reply('{"word": "from fallback", "count": 1}'),
    )
    client, _ = _client(server, fallback_api_key="fb-key", fallback_models=["fb-model:free"])
    answer, usage = _ask(client)
    assert answer.word == "from fallback"
    assert usage.model == "fb-model:free" and usage.provider == "fallback.test"
    assert server.requests[-1].headers["Authorization"] == "Bearer fb-key"


def test_auth_error_is_not_retried_on_the_same_provider():
    server = FakeServer(httpx.Response(401, json={"error": "bad key"}))
    client, _ = _client(server)
    with pytest.raises(LLMError, match="401"):
        _ask(client)
    assert len(server.requests) == 1


def test_everything_failing_raises_llm_error_with_a_summary():
    server = FakeServer(_reply("not json at all"), _reply("still not json"))
    client, _ = _client(server)
    with pytest.raises(LLMError, match="m-primary"):
        _ask(client)


def test_requests_are_spaced_to_respect_the_per_minute_limit():
    server = FakeServer(_reply('{"word": "a", "count": 1}'), _reply('{"word": "b", "count": 1}'))
    client, sleeps = _client(server, max_requests_per_min=30)  # ≥ 2 s apart
    _ask(client)
    _ask(client)
    assert any(s > 1.5 for s in sleeps)


def test_not_configured_raises_a_clear_error():
    client = LLMClient(_settings(llm_api_key=""), transport=httpx.MockTransport(FakeServer()))
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        _ask(client)


def test_503_backoff_gives_an_overloaded_model_time_to_recover():
    # Live: three models answered 503 within seconds of each other.
    server = FakeServer(httpx.Response(503), _reply('{"word": "ok", "count": 1}'))
    client, sleeps = _client(server)
    _ask(client)
    assert sleeps and sleeps[0] >= 3


# --- Consistency review (C3-C5): crashes that escaped the error handling --- #

def test_c3_a_non_object_response_body_is_an_llm_error_not_a_crash():
    server = FakeServer(*[httpx.Response(200, json=[1, 2, 3]) for _ in range(3)])
    client, _ = _client(server)
    with pytest.raises(LLMError):
        _ask(client)


def test_c3_malformed_usage_field_does_not_lose_a_valid_answer():
    body = {"choices": [{"message": {"content": '{"word": "ok", "count": 1}'}}], "usage": "n/a"}
    client, _ = _client(FakeServer(httpx.Response(200, json=body)))
    answer, usage = _ask(client)
    assert answer.word == "ok" and usage.prompt_tokens == 0


def test_c4_negative_retry_after_never_means_a_negative_sleep():
    server = FakeServer(httpx.Response(429, headers={"Retry-After": "-5"}),
                        _reply('{"word": "ok", "count": 1}'))
    client, sleeps = _client(server)
    _ask(client)
    assert sleeps and all(s >= 0 for s in sleeps)


def test_c5_client_closes_its_http_connection():
    with LLMClient(_settings(), transport=httpx.MockTransport(FakeServer())) as client:
        assert not client.closed
    assert client.closed



# --- Live run: Gemini free tier = 20 requests/day/model (HTTP 429) --------- #

def _quota(delay: str, per_day: bool = True) -> httpx.Response:
    quota_id = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "PerMinute"
    return httpx.Response(429, json=[{"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ]}}])


def test_daily_quota_exhausted_skips_the_model_at_once():
    server = FakeServer(_quota("48624s"), _reply('{"word": "ok", "count": 1}'))
    client, sleeps = _client(server, llm_fallback_models=["m-second"])
    _, usage = _ask(client)
    assert usage.model == "m-second"
    assert len(server.requests) == 2 and sleeps == []       # no retry, no 13-hour wait


def test_an_exhausted_model_is_not_called_again_in_the_same_session():
    server = FakeServer(_quota("48624s"), _reply('{"word": "a", "count": 1}'),
                        _reply('{"word": "b", "count": 1}'))
    client, _ = _client(server, llm_fallback_models=["m-second"])
    _ask(client)
    _ask(client)
    assert [json.loads(r.content)["model"] for r in server.requests] == [
        "m-primary", "m-second", "m-second"]


def test_a_short_per_minute_quota_is_waited_out_and_retried():
    server = FakeServer(_quota("7s", per_day=False), _reply('{"word": "ok", "count": 1}'))
    client, sleeps = _client(server)
    _, usage = _ask(client)
    assert usage.model == "m-primary" and 7 in sleeps
