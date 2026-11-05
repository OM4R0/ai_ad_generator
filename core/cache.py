"""On-disk cache of LLM answers (NFR-7, design §9).

Why: the Gemini free tier allows 20 requests per day per model, so a rerun must
not pay again for an answer we already have. The video is recorded from cache.

`CachedLLM` wraps the LLM client and has the same `complete_json` interface, so
the vision and copy steps don't know (or care) whether an answer is live or replayed.

The key is a SHA-256 of everything that shapes the answer: step, model, schema,
temperature, the full prompt text and the image pixels. The prompt text already
contains the product row, the brand, the vision findings, the prompt version and
the variation number, so changing any of those is automatically a miss.

Only successful answers are stored: a failed call (LLMError) is never cached, so a
bad day (quota used up) can't freeze the template fallback into the results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypeVar

from PIL import Image
from pydantic import BaseModel, ValidationError

from core.schemas import CallUsage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

CACHE_FORMAT = 1  # bump to invalidate every stored entry after a format change


class JsonLLM(Protocol):
    def complete_json(self, **kwargs) -> tuple[BaseModel, CallUsage]: ...


class CachedLLM:
    """An LLM client that replays answers from `cache_dir/llm/<step>/<key>.json`.

    `refresh=True` ignores stored answers (but still saves the new ones).
    """

    def __init__(self, client: JsonLLM, cache_dir: Path, *, refresh: bool = False) -> None:
        self._client = client
        self._dir = Path(cache_dir) / "llm"
        self.refresh = refresh
        self.hits = 0
        self.misses = 0

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
        path = self._dir / _safe(step) / f"{cache_key(step, model, system, user, schema, image, temperature)}.json"
        if not self.refresh:
            stored = _read(path, schema)
            if stored is not None:
                self.hits += 1
                return stored

        self.misses += 1
        value, usage = self._client.complete_json(  # LLMError propagates: never cached
            step=step, model=model, system=system, user=user,
            schema=schema, image=image, temperature=temperature,
        )
        _write(path, value, usage)
        return value, usage


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def cache_key(step: str, model: str, system: str, user: str, schema: type[BaseModel],
              image: Image.Image | None, temperature: float) -> str:
    """Stable SHA-256 of everything that can change the answer."""
    parts = {
        "format": CACHE_FORMAT, "step": step, "model": model, "schema": schema.__name__,
        "temperature": temperature, "system": system, "user": user,
        "image": image_fingerprint(image) if image is not None else None,
    }
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def image_fingerprint(image: Image.Image) -> str:
    """Hash of the pixels: the same photo loaded twice gives the same key."""
    digest = hashlib.sha256(f"{image.mode}:{image.size}".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def _read(path: Path, schema: type[T]) -> tuple[T, CallUsage] | None:
    """The stored (answer, usage), or None if missing, corrupt or out of date."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        value = schema.model_validate(entry["value"])
        usage = CallUsage.model_validate(entry["usage"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        logger.info("Ignoring unusable cache entry %s: %s", path.name, exc)
        return None
    return value, usage.model_copy(update={"cached": True, "latency_s": 0.0})


def _write(path: Path, value: BaseModel, usage: CallUsage) -> None:
    """Write atomically: a crash mid-write leaves the old file or none, never half a file."""
    entry = {
        "format": CACHE_FORMAT,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value": value.model_dump(mode="json"),
        "usage": usage.model_dump(mode="json"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # a full or read-only disk must not lose a live answer
        logger.warning("Could not save cache entry %s: %s", path.name, exc)


def _safe(step: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in step) or "step"
