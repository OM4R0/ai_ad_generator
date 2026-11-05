"""OpenRouter as the backup for Gemini (ADR-003). No network: a fake HTTP server.

The response shapes below were seen live on 2026-11-05 (O4, O10) or follow
OpenRouter's documented error format (O5, O6). See docs/edge-cases.md, section O.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel

from config.settings import Settings
from core.llm_client import LLMClient, LLMError, parse_json_text

GEMINI_KEY = "gemini-secret-111"
OPENROUTER_KEY = "sk-or-secret-222"
OK = '{"word": "ok", "count": 1}'


class Answer(BaseModel):
    word: str
    count: int


def _settings(**overrides) -> Settings:
    base = Settings(
        llm_base_url="https://gemini.test/v1beta/openai/", llm_api_key=GEMINI_KEY,
        vision_model="gemini-a", copy_model="gemini-a",
        fallback_base_url="https://openrouter.test/api/v1", fallback_api_key=OPENROUTER_KEY,
        fallback_models=["or-a:free", "or-b:free"],
        max_requests_per_min=0, reference_price_per_mtok=1.0,
    )
    return replace(base, **overrides)


def _reply(content, model: str | None = None) -> httpx.Response:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    if model:
        body["model"] = model
    return httpx.Response(200, json=body)


def _gemini_daily_quota() -> httpx.Response:
    return httpx.Response(429, json={"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "48624s"},
    ]}})


def _upstream_busy(model: str = "or-a:free") -> httpx.Response:
    # Seen live 2026-11-05 on google/gemma-4-31b-it:free
    return httpx.Response(429, json={"error": {
        "message": "Provider returned error", "code": 429,
        "metadata": {"raw": f"{model} is temporarily rate-limited upstream. Please retry shortly",
                     "provider_name": "Google AI Studio",
                     "limit_source": "upstream_provider_shared_pool"}}})


def _free_daily_cap() -> httpx.Response:
    # OpenRouter's documented reply once an account's free requests for the day are used up
    return httpx.Response(429, json={"error": {
        "message": "Rate limit exceeded: free-models-per-day. "
                   "Add 10 credits to unlock 1000 free model requests per day",
        "code": 429,
        "metadata": {"headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                 "X-RateLimit-Reset": "1762387200000"}}}})


class FakeServer:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    @property
    def models(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]


def _client(server: FakeServer, **settings) -> tuple[LLMClient, list[float]]:
    sleeps: list[float] = []
    client = LLMClient(_settings(**settings), transport=httpx.MockTransport(server),
                       sleep=sleeps.append)
    return client, sleeps


def _ask(client: LLMClient):
    return client.complete_json(step="copy", model="gemini-a", system="sys",
                                user="Give me JSON", schema=Answer)


# --------------------------------------------------------------------------- #
# The hand-over from Gemini to OpenRouter
# --------------------------------------------------------------------------- #

def test_o1_gemini_daily_quota_hands_over_to_openrouter_and_stays_there():
    server = FakeServer(_gemini_daily_quota(), _reply(OK), _reply(OK))
    client, sleeps = _client(server)
    _, usage = _ask(client)
    assert (usage.provider, usage.model) == ("openrouter.test", "or-a:free")
    _ask(client)  # Gemini is known to be out for today: not asked again
    assert server.hosts == ["gemini.test", "openrouter.test", "openrouter.test"]
    assert sleeps == []


def test_o2_each_key_is_sent_only_to_its_own_provider():
    server = FakeServer(httpx.Response(401, json={"error": "bad key"}), _reply(OK))
    client, _ = _client(server)
    _ask(client)
    gemini, openrouter = server.requests
    assert gemini.url.host == "gemini.test"
    assert gemini.headers["Authorization"] == f"Bearer {GEMINI_KEY}"
    assert openrouter.url.host == "openrouter.test"
    assert openrouter.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"


def test_o3_openrouter_alone_is_enough_when_there_is_no_gemini_key():
    server = FakeServer(_reply(OK))
    client, _ = _client(server, llm_api_key="")
    _, usage = _ask(client)
    assert usage.provider == "openrouter.test"
    assert server.hosts == ["openrouter.test"]  # never sends a keyless request to Gemini


def test_o3_with_no_key_at_all_the_error_names_both_settings():
    client, _ = _client(FakeServer(), llm_api_key="", fallback_api_key="")
    with pytest.raises(LLMError, match="LLM_API_KEY.*FALLBACK_API_KEY"):
        _ask(client)


# --------------------------------------------------------------------------- #
# OpenRouter's own limits
# --------------------------------------------------------------------------- #

def test_o4_a_busy_shared_free_pool_moves_to_the_next_model_without_waiting():
    server = FakeServer(_upstream_busy(), _reply(OK))
    client, sleeps = _client(server, llm_api_key="")
    _, usage = _ask(client)
    assert usage.model == "or-b:free"
    assert server.models == ["or-a:free", "or-b:free"]  # not retried 3 times
    assert sleeps == []


def test_o4_a_busy_model_is_tried_again_on_the_next_call():
    # The shared pool frees up within minutes: unlike a daily cap, don't drop the model.
    server = FakeServer(_upstream_busy(), _reply(OK), _reply(OK))
    client, _ = _client(server, llm_api_key="")
    _ask(client)
    _ask(client)
    assert server.models == ["or-a:free", "or-b:free", "or-a:free"]


def test_o5_the_daily_free_cap_skips_every_openrouter_model_for_the_session():
    server = FakeServer(_free_daily_cap())
    client, sleeps = _client(server, llm_api_key="")
    with pytest.raises(LLMError, match="daily"):
        _ask(client)
    assert server.models == ["or-a:free"]  # the cap is per ACCOUNT: or-b:free would fail too
    assert sleeps == []
    with pytest.raises(LLMError):
        _ask(client)
    assert len(server.requests) == 1        # no request at all the second time


def test_o5_the_daily_cap_on_openrouter_does_not_block_gemini():
    server = FakeServer(_upstream_busy(), _free_daily_cap(), _reply(OK))
    client, _ = _client(server, llm_api_key="")
    with pytest.raises(LLMError):
        _ask(client)
    client._settings = _settings()          # the Gemini key is back (e.g. a new session)
    _, usage = _ask(client)
    assert usage.provider == "gemini.test"


# --------------------------------------------------------------------------- #
# Reply shapes that differ from Gemini's
# --------------------------------------------------------------------------- #

def test_o6_a_bad_request_inside_a_200_reply_moves_on_without_retrying():
    bad = httpx.Response(200, json={"error": {
        "code": 400, "message": "or-a:free does not support response_format"}})
    server = FakeServer(bad, _reply(OK))
    client, sleeps = _client(server, llm_api_key="")
    _, usage = _ask(client)
    assert usage.model == "or-b:free" and sleeps == []


def test_o6_a_server_error_inside_a_200_reply_is_retried():
    busy = httpx.Response(200, json={"error": {"code": 502, "message": "upstream error"}})
    server = FakeServer(busy, _reply(OK))
    client, sleeps = _client(server, llm_api_key="")
    _, usage = _ask(client)
    assert usage.model == "or-a:free" and sleeps == [3.0]


def test_o6_the_message_inside_a_200_error_reaches_the_final_error():
    bad = httpx.Response(200, json={"error": {"code": 400, "message": "no image input here"}})
    client, _ = _client(FakeServer(bad), llm_api_key="", fallback_models=["or-a:free"])
    with pytest.raises(LLMError, match="no image input here"):
        _ask(client)


def test_o7_content_given_as_a_list_of_text_parts_is_joined():
    parts = [{"type": "text", "text": '{"word": "hi",'}, {"type": "text", "text": ' "count": 2}'}]
    client, _ = _client(FakeServer(_reply(parts)), llm_api_key="")
    answer, _ = _ask(client)
    assert answer == Answer(word="hi", count=2)


@pytest.mark.parametrize("content", [{"word": "hi"}, 42, [{"type": "image"}]])
def test_o8_content_that_is_not_text_is_an_llm_error_not_a_crash(content):
    server = FakeServer(*[_reply(content) for _ in range(3)])
    client, _ = _client(server, llm_api_key="", fallback_models=["or-a:free"])
    with pytest.raises(LLMError):
        _ask(client)


def test_o9_reasoning_text_with_braces_before_the_json_is_ignored():
    text = '<think>They want {word, count}; keep it short.</think>\n{"word": "hi", "count": 1}'
    assert parse_json_text(text) == {"word": "hi", "count": 1}


def test_o10_the_model_that_really_answered_is_recorded():
    # Seen live: "openrouter/free" answered from nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
    server = FakeServer(_reply(OK, model="vendor/real-model:free"))
    client, _ = _client(server, llm_api_key="", fallback_models=["openrouter/free"])
    _, usage = _ask(client)
    assert usage.model == "openrouter/free"
    assert usage.served_model == "vendor/real-model:free"


def test_o10_served_model_defaults_to_the_requested_one():
    client, _ = _client(FakeServer(_reply(OK)), llm_api_key="")
    _, usage = _ask(client)
    assert usage.served_model == "or-a:free"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", [401, 403, 404])
def test_o11_an_error_that_echoes_the_api_key_never_leaks_it(caplog, status):
    # The error text ends up in logs and in metadata.json (which may be committed).
    echo = httpx.Response(status, json={"error": {"message": f"Invalid key {OPENROUTER_KEY}"}})
    client, _ = _client(FakeServer(echo), llm_api_key="", fallback_models=["or-a:free"])
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as err:
        _ask(client)
    assert OPENROUTER_KEY not in str(err.value)
    assert OPENROUTER_KEY not in caplog.text


def test_o11_the_mask_replaces_the_key_where_the_server_text_is_kept():
    echo = httpx.Response(404, json={"error": {"message": f"Invalid key {OPENROUTER_KEY}"}})
    client, _ = _client(FakeServer(echo), llm_api_key="", fallback_models=["or-a:free"])
    with pytest.raises(LLMError, match=r"Invalid key \*\*\*"):
        _ask(client)


# --------------------------------------------------------------------------- #
# A rejected key is a provider problem, not a model problem
# --------------------------------------------------------------------------- #

def _gemini_bad_key() -> httpx.Response:
    # Seen live 2026-11-05 (hand-over test with a deliberately invalid Gemini key)
    return httpx.Response(400, json=[{"error": {
        "code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}])


@pytest.mark.parametrize("rejection", [
    _gemini_bad_key, lambda: httpx.Response(401, json={"error": {"message": "Invalid credentials"}})])
def test_o12_a_rejected_key_skips_every_model_on_that_provider(rejection):
    server = FakeServer(rejection(), _reply(OK), _reply(OK))
    client, _ = _client(server, llm_fallback_models=["gemini-b", "gemini-c"])
    _ask(client)
    _ask(client)
    # Not gemini-b and gemini-c with the same bad key, and not again on the 2nd call
    assert server.hosts == ["gemini.test", "openrouter.test", "openrouter.test"]


def test_o12_a_plain_bad_request_still_tries_the_next_model_on_the_same_key():
    server = FakeServer(httpx.Response(400, json={"error": {"message": "bad schema"}}), _reply(OK))
    client, _ = _client(server, llm_fallback_models=["gemini-b"])
    _, usage = _ask(client)
    assert usage.model == "gemini-b"
