"""LLM client: one OpenAI-compatible client for Gemini and OpenRouter (ADR-003).

Every call goes through `complete_json`, which guarantees:
- **Validated output** (NFR-4): the reply is parsed as JSON and validated
  against a Pydantic schema; one "repair" retry feeds the error back.
- **Resilience** (NFR-2): timeouts, 5xx and 429 are retried with backoff
  (429 honours `Retry-After`); auth/bad-request errors are not retried.
  When a provider is exhausted, the next fallback model is tried.
- **Rate limiting** (NFR-7): requests are spaced to stay under the
  per-minute cap of free tiers.
- **Usage accounting** (FR-14): tokens, estimated cost and latency.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar
from urllib.parse import urlparse

import httpx
from PIL import Image
from pydantic import BaseModel, ValidationError

from config.settings import Settings
from core.schemas import CallUsage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3            # per provider, for retryable errors
BACKOFF_BASE_S = 3.0        # waits 3 s, then 6 s (live: 503 bursts last a few seconds)
MAX_RETRY_AFTER_S = 60.0    # never wait longer than this for a 429
IMAGE_MAX_SIDE = 1024       # images are downscaled before sending (fewer tokens)
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Every provider and model failed for this call."""


@dataclass(frozen=True)
class _Provider:
    base_url: str
    api_key: str
    model: str

    @property
    def name(self) -> str:
        return urlparse(self.base_url).hostname or self.base_url


class _Retryable(Exception):
    def __init__(self, message: str, wait_s: float | None = None):
        super().__init__(message)
        self.wait_s = wait_s


class _Fatal(Exception):
    """Don't retry on this provider (e.g. 401, 400); move to the next one."""


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._http = httpx.Client(timeout=settings.llm_timeout_s, transport=transport)
        self._sleep = sleep
        self._min_interval = (
            60.0 / settings.max_requests_per_min if settings.max_requests_per_min > 0 else 0.0
        )
        self._last_request_at: float | None = None

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ public

    def complete_json(
        self,
        *,
        step: str,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        image: Image.Image | None = None,
        temperature: float = 0.4,
    ) -> tuple[T, CallUsage]:
        """Ask for a JSON answer matching `schema`. Returns (validated model, usage).

        Raises:
            LLMError: when the primary and every fallback model failed.
        """
        if not self._settings.llm_api_key:
            raise LLMError("LLM is not configured: set LLM_API_KEY in .env")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_content(user, image)},
        ]
        usage = CallUsage(step=step, provider="", model=model)
        started = time.monotonic()
        failures: list[str] = []

        for provider in self._providers(model):
            try:
                result = self._ask_provider(provider, messages, schema, temperature, usage)
            except (_Fatal, _Retryable, ValueError, ValidationError) as exc:
                failures.append(f"{provider.model}@{provider.name}: {exc}")
                logger.warning("LLM %s failed on %s: %s", step, provider.model, exc)
                continue
            usage.provider, usage.model = provider.name, provider.model
            usage.latency_s = round(time.monotonic() - started, 2)
            usage.estimated_cost_usd = self._estimate_cost(usage)
            return result, usage

        raise LLMError(f"all LLM providers failed for {step!r}: " + " | ".join(failures))

    # ----------------------------------------------------------------- private

    def _providers(self, model: str) -> list[_Provider]:
        """Order: requested model → other models on the same key → fallback provider."""
        s = self._settings
        providers = [_Provider(s.llm_base_url, s.llm_api_key, model)]
        providers += [_Provider(s.llm_base_url, s.llm_api_key, m)
                      for m in s.llm_fallback_models if m != model]
        if s.fallback_api_key:
            providers += [_Provider(s.fallback_base_url, s.fallback_api_key, m)
                          for m in s.fallback_models]
        return providers

    def _ask_provider(self, provider, messages, schema, temperature, usage):
        """One provider: request, validate, and if invalid one repair round."""
        text = self._post_with_retry(provider, messages, temperature, usage)
        try:
            return schema.model_validate(parse_json_text(text))
        except (ValueError, ValidationError) as exc:
            logger.info("Invalid JSON from %s, asking for a repair: %s", provider.model, exc)
            repair = [
                *messages,
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "Your previous reply was not valid for the required JSON format. "
                    f"Error: {str(exc)[:1500]}\n"
                    "Reply again with ONLY the corrected JSON object, no other text."
                )},
            ]
            text = self._post_with_retry(provider, repair, temperature, usage)
            return schema.model_validate(parse_json_text(text))

    def _post_with_retry(self, provider, messages, temperature, usage) -> str:
        last: _Retryable | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._post(provider, messages, temperature, usage)
            except _Retryable as exc:
                last = exc
                if attempt < MAX_ATTEMPTS - 1:
                    wait = exc.wait_s if exc.wait_s is not None else BACKOFF_BASE_S * 2 ** attempt
                    logger.info("Retrying %s in %.1fs: %s", provider.model, wait, exc)
                    self._sleep(wait)
        assert last is not None
        raise last

    def _post(self, provider, messages, temperature, usage) -> str:
        self._respect_rate_limit()
        body = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        url = provider.base_url.rstrip("/") + "/chat/completions"
        try:
            response = self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {provider.api_key}"}
            )
        except httpx.TimeoutException as exc:
            # An overloaded model tends to time out again: move to the next model
            # instead of waiting LLM_TIMEOUT_S three more times.
            raise _Fatal(f"timed out after {self._settings.llm_timeout_s:.0f}s") from exc
        except httpx.TransportError as exc:  # DNS, connection resets
            raise _Retryable(f"network error: {exc!r}") from exc

        if response.status_code in RETRYABLE_STATUS:
            raise _Retryable(f"HTTP {response.status_code}", _retry_after(response))
        if response.status_code >= 400:
            raise _Fatal(f"HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise _Retryable(f"response is not JSON: {response.text[:200]}") from exc
        if not isinstance(payload, dict):
            raise _Retryable(f"unexpected response body: {str(payload)[:200]}")
        tokens = payload.get("usage")
        if isinstance(tokens, dict):  # usage is informative: never lose an answer over it
            usage.prompt_tokens += _as_int(tokens.get("prompt_tokens"))
            usage.completion_tokens += _as_int(tokens.get("completion_tokens"))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _Retryable(f"malformed response: {str(payload)[:200]}") from exc
        if not content:
            raise _Retryable("empty response")
        return content

    def _respect_rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None and self._min_interval:
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _estimate_cost(self, usage: CallUsage) -> float:
        # Free tier → the actual cost is $0; the estimate shows the cost at a
        # reference paid price, i.e. what the system would cost at scale.
        tokens = usage.prompt_tokens + usage.completion_tokens
        return round(tokens / 1_000_000 * self._settings.reference_price_per_mtok, 6)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def parse_json_text(text: str) -> object:
    """Parse JSON from an LLM reply that may be fenced (```json) or wrapped in prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise ValueError(f"no JSON object in reply: {text[:120]!r}") from None


def _user_content(text: str, image: Image.Image | None) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": text}]
    if image is not None:
        parts.append({"type": "image_url", "image_url": {"url": image_to_data_url(image)}})
    return parts


def image_to_data_url(image: Image.Image) -> str:
    """Downscale, flatten transparency onto white, and encode as a JPEG data URL."""
    img = image.copy()
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait from a Retry-After header, clamped to [0, MAX_RETRY_AFTER_S]."""
    raw = response.headers.get("Retry-After")
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER_S)) if raw else None
    except ValueError:  # e.g. an HTTP date: fall back to our own backoff
        return None


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
