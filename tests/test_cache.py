"""Tests for core.cache (NFR-7, design §9): LLM answers are replayed from disk.

Why it matters: the Gemini free tier allows 20 requests per day per model, so a
rerun must not spend requests on answers we already have.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image
from pydantic import BaseModel

from core.cache import CachedLLM
from core.llm_client import LLMError
from core.schemas import CallUsage


class Answer(BaseModel):
    word: str
    count: int


class OtherAnswer(BaseModel):
    word: str
    count: int


class FakeLLM:
    """Plays back answers (or exceptions) and records every call that reached it."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, CallUsage(step=kwargs["step"], provider="fake", model=kwargs["model"],
                               served_model="fake/real-model", prompt_tokens=100,
                               completion_tokens=50, latency_s=12.0)


HI = Answer(word="hi", count=1)
CALL = dict(step="copy", model="m", system="sys", user="Write the ad for product #13",
            schema=Answer, temperature=0.8)


def _cached(tmp_path, *results, **kw) -> tuple[CachedLLM, FakeLLM]:
    fake = FakeLLM(*results)
    return CachedLLM(fake, tmp_path, **kw), fake


def _files(tmp_path):
    return [p for p in tmp_path.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- #

def test_k1_the_second_identical_call_is_served_from_disk(tmp_path):
    cache, fake = _cached(tmp_path, HI)
    first, live = cache.complete_json(**CALL)
    second, replay = cache.complete_json(**CALL)
    assert first == second == HI
    assert len(fake.calls) == 1                       # one real request, not two
    assert not live.cached and replay.cached
    assert replay.latency_s == 0.0                    # a replay takes no time ...
    assert (replay.prompt_tokens, replay.completion_tokens) == (100, 50)  # ... but keeps its tokens
    assert replay.served_model == "fake/real-model"
    assert (cache.hits, cache.misses) == (1, 1)


def test_k1_the_cache_survives_a_new_run(tmp_path):
    cache, _ = _cached(tmp_path, HI)
    cache.complete_json(**CALL)
    next_run, fake = _cached(tmp_path)                # a new process, same folder
    answer, usage = next_run.complete_json(**CALL)
    assert answer == HI and usage.cached and fake.calls == []


@pytest.mark.parametrize("change", [
    {"user": "Write the ad for product #14"},          # another product / brand / vision / variation
    {"system": "sys v2"},                              # another prompt version
    {"model": "other-model"},
    {"step": "vision"},
    {"temperature": 0.2},
    {"schema": OtherAnswer},
])
def test_k2_any_change_to_what_shapes_the_answer_is_a_miss(tmp_path, change):
    cache, fake = _cached(tmp_path, HI, OtherAnswer(word="new", count=2))
    cache.complete_json(**CALL)
    cache.complete_json(**{**CALL, **change})
    assert len(fake.calls) == 2


def test_k3_the_image_is_part_of_the_key_by_its_pixels(tmp_path):
    red = Image.new("RGB", (40, 40), "red")
    cache, fake = _cached(tmp_path, HI, HI, HI)
    cache.complete_json(**CALL, image=red)
    cache.complete_json(**CALL, image=Image.new("RGB", (40, 40), "red"))   # same pixels: hit
    assert len(fake.calls) == 1
    cache.complete_json(**CALL, image=Image.new("RGB", (40, 40), "blue"))  # other photo: miss
    cache.complete_json(**CALL)                                            # no photo: miss
    assert len(fake.calls) == 3


def test_k4_a_failed_call_is_never_cached(tmp_path):
    # Otherwise one bad day (quota out) would freeze the template fallback forever.
    cache, fake = _cached(tmp_path, LLMError("quota"), HI)
    with pytest.raises(LLMError):
        cache.complete_json(**CALL)
    answer, usage = cache.complete_json(**CALL)
    assert answer == HI and not usage.cached and len(fake.calls) == 2


def test_k5_a_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path):
    cache, fake = _cached(tmp_path, HI, HI)
    cache.complete_json(**CALL)
    [entry] = _files(tmp_path)
    entry.write_text("{ half a fi", encoding="utf-8")   # e.g. the PC died mid-write
    answer, _ = cache.complete_json(**CALL)
    assert answer == HI and len(fake.calls) == 2
    json.loads(entry.read_text(encoding="utf-8"))        # repaired


def test_k6_a_cached_answer_that_no_longer_fits_the_schema_is_a_miss(tmp_path):
    cache, fake = _cached(tmp_path, HI, HI)
    cache.complete_json(**CALL)
    [entry] = _files(tmp_path)
    data = json.loads(entry.read_text(encoding="utf-8"))
    data["value"] = {"word": "old shape"}                # "count" became required later
    entry.write_text(json.dumps(data), encoding="utf-8")
    answer, _ = cache.complete_json(**CALL)
    assert answer == HI and len(fake.calls) == 2


def test_k7_refresh_skips_the_stored_answer_and_replaces_it(tmp_path):
    cache, _ = _cached(tmp_path, HI)
    cache.complete_json(**CALL)
    fresh, fake = _cached(tmp_path, Answer(word="fresh", count=2), refresh=True)
    answer, usage = fresh.complete_json(**CALL)
    assert answer.word == "fresh" and not usage.cached and len(fake.calls) == 1
    later, _ = _cached(tmp_path)
    assert later.complete_json(**CALL)[0].word == "fresh"


def test_k8_arabic_is_stored_readable_not_escaped(tmp_path):
    cache, _ = _cached(tmp_path, Answer(word="مرحبا", count=1))
    cache.complete_json(**CALL)
    [entry] = _files(tmp_path)
    assert "مرحبا" in entry.read_text(encoding="utf-8")


def test_k9_no_temporary_files_are_left_behind(tmp_path):
    cache, _ = _cached(tmp_path, HI, Answer(word="b", count=2))
    cache.complete_json(**CALL)
    cache.complete_json(**{**CALL, "user": "other"})
    assert [p.suffix for p in _files(tmp_path)] == [".json", ".json"]


def test_k10_answers_are_grouped_by_step_for_easy_inspection(tmp_path):
    cache, _ = _cached(tmp_path, HI)
    cache.complete_json(**CALL)
    [entry] = _files(tmp_path)
    assert entry.parent.name == "copy"
