"""Tests for core.pipeline (FR-12, NFR-2, design §5/§8). No network: a scripted LLM
and gradient backgrounds. The CLI and the dashboard both call this same code."""

from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest
from PIL import Image

from config.settings import PROJECT_ROOT, Settings
from core.cache import CachedLLM
from core.excel_loader import load_catalog
from core.llm_client import LLMError
from core.pipeline import OfflineLLM, Services, open_services, run_batch, run_product
from core.scene_generator import (
    PRODUCT_PHOTO_RAILS, PRODUCT_PHOTO_SIZE, SCENE_GUARD_RAILS, SceneGenerator, SceneResult,
)
from core.schemas import BrandPatch, CallUsage, VisionAnalysis
from tests.test_copy_engine import _draft
from tests.test_visual_engine import fake_remover

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)
BACKPACK = CATALOG.by_id(23)

VISION = VisionAnalysis(
    product_type="over-ear headphones", dominant_colors=["#1C1D21", "#C0C0C0"],
    materials_finish=["matte plastic"], mood="sleek",
    visual_selling_points=["VISION-POINT folding hinges"],
    scene_prompt="VISION-SCENE dark slate studio",
)


class ScriptedLLM:
    """Answers by step. `fail_steps` raise LLMError (quota); `crash_on` raises a bug."""

    def __init__(self, vision=VISION, copy=None, fail_steps=(), crash_on: int | None = None):
        self.vision, self.copy = vision, copy or _draft()
        self.fail_steps, self.crash_on = set(fail_steps), crash_on
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.crash_on == len(self.calls):
            raise RuntimeError("unexpected bug")
        if kwargs["step"] in self.fail_steps:
            raise LLMError("daily free quota used up")
        value = self.vision if kwargs["step"] == "vision" else self.copy
        return value, CallUsage(step=kwargs["step"], provider="fake", model=kwargs["model"],
                                prompt_tokens=100, completion_tokens=50, estimated_cost_usd=0.001)


class RecordingScenes(SceneGenerator):
    def __init__(self, cache_dir):
        super().__init__("none", cache_dir=cache_dir)
        self.prompts: list[str] = []

    def generate(self, prompt, size, palette, seed=0, **kwargs):
        self.prompts.append(prompt)
        return super().generate(prompt, size, palette, seed, **kwargs)


class PhotoScenes(RecordingScenes):
    """Paints a blue 'product photo' when one is asked for; scenes stay gradients."""

    def generate(self, prompt, size, palette, seed=0, *, rails=SCENE_GUARD_RAILS):
        if rails == PRODUCT_PHOTO_RAILS:
            self.prompts.append(prompt)
            return SceneResult(Image.new("RGB", size, "blue"), "pollinations")
        return super().generate(prompt, size, palette, seed)


def _services(tmp_path, llm=None) -> Services:
    settings = replace(Settings(), vision_model="vision-m", copy_model="copy-m",
                       output_dir=tmp_path / "output", cache_dir=tmp_path / "cache")
    return Services(settings=settings, llm=llm or ScriptedLLM(),
                    scenes=RecordingScenes(tmp_path), remover=fake_remover)


def _png_bytes(size=(600, 400), color="green") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# One product, end to end
# --------------------------------------------------------------------------- #

def test_p1_one_product_end_to_end_writes_the_brief_folder(tmp_path):
    services = _services(tmp_path)
    run = run_product(HEADPHONES, services)
    assert run.ok and run.error is None
    assert run.folder.parent.parent == tmp_path / "output"
    assert {"copy.json", "metadata.json", "creative_feed.png", "creative_story.png",
            "carousel_slide_1.png"} <= {p.name for p in run.folder.iterdir()}
    result = run.result
    assert result.vision == VISION
    assert [u.step for u in result.usage] == ["vision", "copy"]
    assert result.prompt_versions == {"vision": "vision_v3", "copy": "copy_v2"}
    assert result.image_source == "local"
    assert result.fallbacks == []
    assert result.product_mode == "cutout"


def test_p2_vision_findings_are_used_by_the_copy_and_the_scene(tmp_path):
    # FR-3: "Copy and scene prompts must USE these"
    services = _services(tmp_path)
    run = run_product(HEADPHONES, services)
    vision_call, copy_call = services.llm.calls
    assert "VISION-POINT" in copy_call["user"]
    assert "VISION-SCENE" in services.scenes.prompts[0]
    assert run.result.brand.brand_colors[:2] == ["#1C1D21", "#C0C0C0"]  # product, not backdrop


def test_p3_vision_and_copy_use_their_own_models(tmp_path):
    services = _services(tmp_path)
    run_product(HEADPHONES, services)
    vision_call, copy_call = services.llm.calls
    assert (vision_call["model"], copy_call["model"]) == ("vision-m", "copy-m")
    assert vision_call["image"] is not None and copy_call["image"] is None


def test_p4_with_the_llm_down_the_ad_is_still_made(tmp_path):
    # NFR-2: a failing AI service downgrades quality but never stops the pipeline.
    services = _services(tmp_path, ScriptedLLM(fail_steps={"vision", "copy"}))
    run = run_product(HEADPHONES, services)
    assert run.ok and (run.folder / "copy.json").exists()
    assert [f.split(":")[0] for f in run.result.fallbacks] == ["vision", "copy"]
    assert run.result.usage == []


def test_p5_a_failed_vision_step_does_not_turn_backdrop_colours_into_brand_colours(tmp_path):
    services = _services(tmp_path, ScriptedLLM(fail_steps={"vision"}))
    run = run_product(HEADPHONES, services)
    assert "vision" not in run.result.brand.sources


def test_p6_photo_warnings_reach_the_result(tmp_path):
    wrong = VISION.model_copy(update={"photo_matches_product": False,
                                      "photo_shows": "an iced coffee"})
    run = run_product(HEADPHONES, _services(tmp_path, ScriptedLLM(vision=wrong)))
    assert any("iced coffee" in w for w in run.result.warnings)
    meta = json.loads((run.folder / "metadata.json").read_text("utf-8"))
    assert any("iced coffee" in w for w in meta["warnings"])


def test_p7_an_uploaded_photo_replaces_the_catalog_photo(tmp_path):
    services = _services(tmp_path)
    run = run_product(HEADPHONES, services, upload=_png_bytes((600, 400)))
    assert run.result.image_source == "upload"
    assert services.llm.calls[0]["image"].size == (600, 400)


def test_p7_an_image_link_that_does_not_work_is_reported_not_silent(tmp_path):
    # Dashboard "Image URL": a broken link fell back to the catalog photo and the
    # only sign was "Photo: local" in small print.
    run = run_product(HEADPHONES, _services(tmp_path), image_url="ftp://example.com/photo.jpg")
    assert run.ok and run.result.image_source == "local"
    [note] = [f for f in run.result.fallbacks if f.startswith("photo:")]
    assert "ftp://example.com/photo.jpg" in note and "catalog" in note


def test_p7_the_ai_can_paint_the_product_photo(tmp_path):
    # Brief, dashboard: "use the default reference image, let AI generate a new one, or
    # upload a custom image". The vision step then looks at the AI photo.
    services = _services(tmp_path)
    services.scenes = PhotoScenes(tmp_path)
    run = run_product(HEADPHONES, services, ai_photo=True)
    assert run.ok and run.result.image_source == "ai"
    assert services.llm.calls[0]["image"].size == PRODUCT_PHOTO_SIZE
    assert HEADPHONES.name in services.scenes.prompts[0]
    assert any("AI-generated" in w for w in run.result.warnings)   # a human must check it


def test_p7_the_ai_photo_stays_the_same_for_every_new_angle(tmp_path):
    services = _services(tmp_path)
    services.scenes = PhotoScenes(tmp_path)
    seeds = []
    real = services.scenes.generate
    services.scenes.generate = lambda *a, **k: seeds.append((a[3] if len(a) > 3 else k.get("seed", 0), k.get("rails"))) or real(*a, **k)
    run_product(HEADPHONES, services, ai_photo=True, variation=2)
    assert (0, PRODUCT_PHOTO_RAILS) in seeds            # the product photo: always seed 0


def test_p7_without_the_image_service_the_ai_photo_falls_back_to_the_catalog_photo(tmp_path):
    run = run_product(HEADPHONES, _services(tmp_path), ai_photo=True)   # provider "none"
    assert run.ok and run.result.image_source == "local"
    assert any(f.startswith("photo:") and "AI photo" in f for f in run.result.fallbacks)


def _brand_with_logo(tmp_path, logo_url: str):
    """A brand config for AeroSonic Audio (#13) with a logo_url, in its own folder."""
    brands = tmp_path / "brands"
    brands.mkdir()
    (brands / "aerosonic.json").write_text(json.dumps(
        {"brand_name": "AeroSonic Audio", "logo_url": logo_url}), encoding="utf-8")
    services = _services(tmp_path)
    services.settings = replace(services.settings, brands_dir=brands)
    return services


def test_p8_the_logo_in_the_brand_config_is_drawn_on_the_ads(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (300, 80), (20, 60, 230, 255)).save(logo)
    run = run_product(HEADPHONES, _brand_with_logo(tmp_path, str(logo)), write=False)
    box = run.creatives.layouts["feed_a"].text_boxes[0]
    pixel = run.creatives.feed_variants[0].getpixel((box[0] + 5, box[1] + 5))
    assert all(abs(a - b) <= 40 for a, b in zip(pixel, (20, 60, 230)))
    assert not any(f.startswith("logo:") for f in run.result.fallbacks)


def test_p8_a_logo_that_cannot_be_loaded_falls_back_to_the_wordmark_and_says_so(tmp_path):
    run = run_product(HEADPHONES, _brand_with_logo(tmp_path, "assets/missing-logo.png"),
                      write=False)
    assert run.ok
    [note] = [f for f in run.result.fallbacks if f.startswith("logo:")]
    assert "missing-logo.png" in note and "wordmark" in note


def test_p8_manual_brand_edits_win(tmp_path):
    run = run_product(HEADPHONES, _services(tmp_path),
                      manual_patch=BrandPatch(brand_colors=["#FF0000"]))
    assert run.result.brand.brand_colors[0] == "#FF0000"


def test_p9_timings_are_recorded_per_step(tmp_path):
    run = run_product(HEADPHONES, _services(tmp_path))
    assert {"image", "vision", "copy", "visuals", "total"} <= set(run.result.timings_s)
    assert all(v >= 0 for v in run.result.timings_s.values())


def test_p10_arabic_product_gets_rtl_copy(tmp_path):
    run = run_product(BACKPACK, _services(tmp_path))
    assert run.result.copy_.direction == "rtl"


def test_p11_without_writing_nothing_lands_on_disk(tmp_path):
    # The dashboard previews first and saves only the chosen variant (ADR-004).
    run = run_product(HEADPHONES, _services(tmp_path), write=False)
    assert run.ok and run.folder is None and run.creatives is not None
    assert not (tmp_path / "output").exists()


# --------------------------------------------------------------------------- #
# Failures stay inside one product
# --------------------------------------------------------------------------- #

def test_p12_a_missing_photo_fails_that_product_only(tmp_path):
    lost = HEADPHONES.model_copy(update={"id": 98, "image_path": tmp_path / "gone.jpg",
                                         "image_url": None})
    runs = run_batch([lost, BACKPACK], _services(tmp_path))
    assert [r.ok for r in runs] == [False, True]
    assert "image" in runs[0].error.lower() and runs[0].folder is None


def test_p13_an_unexpected_bug_in_one_product_does_not_stop_the_batch(tmp_path):
    runs = run_batch([HEADPHONES, BACKPACK], _services(tmp_path, ScriptedLLM(crash_on=1)))
    assert [r.ok for r in runs] == [False, True]
    assert "RuntimeError" in runs[0].error


def test_p14_the_batch_reports_progress(tmp_path):
    seen = []
    run_batch([HEADPHONES, BACKPACK], _services(tmp_path),
              on_progress=lambda i, total, run: seen.append((i, total, run.product.id)))
    assert seen == [(1, 2, 13), (2, 2, 23)]


# --------------------------------------------------------------------------- #
# Cache and regenerate (NFR-7)
# --------------------------------------------------------------------------- #

def test_p15_a_rerun_spends_no_requests(tmp_path):
    fake = ScriptedLLM()
    services = _services(tmp_path, CachedLLM(fake, tmp_path / "cache"))
    run_product(HEADPHONES, services)
    again = run_product(HEADPHONES, services)
    assert len(fake.calls) == 2                       # not 4
    assert all(u.cached for u in again.result.usage)


def test_p15_replayed_answers_are_priced_at_todays_reference_price(tmp_path):
    # Live run 2026-11-07: answers cached while no price was set said "$0" forever,
    # because the estimate was frozen into the cache entry at call time.
    fake = ScriptedLLM()                      # every answer: 100 prompt + 50 completion tokens
    services = _services(tmp_path, CachedLLM(fake, tmp_path / "cache"))
    run_product(HEADPHONES, services)
    services.settings = replace(services.settings, reference_input_price_per_mtok=2.0,
                                reference_output_price_per_mtok=10.0)
    again = run_product(HEADPHONES, services)
    assert all(u.cached for u in again.result.usage)
    expected = (100 * 2.0 + 50 * 10.0) / 1_000_000
    assert [u.estimated_cost_usd for u in again.result.usage] == [pytest.approx(expected)] * 2


def test_p16_regenerate_asks_only_for_new_copy(tmp_path):
    fake = ScriptedLLM()
    services = _services(tmp_path, CachedLLM(fake, tmp_path / "cache"))
    run_product(HEADPHONES, services)
    run_product(HEADPHONES, services, variation=1)
    assert [c["step"] for c in fake.calls] == ["vision", "copy", "copy"]  # vision replayed
    assert "variation 1" in fake.calls[-1]["user"]


# --------------------------------------------------------------------------- #
# Building the services
# --------------------------------------------------------------------------- #

def test_p17_offline_mode_makes_no_ai_calls_and_says_why(tmp_path):
    services = _services(tmp_path, CachedLLM(OfflineLLM(), tmp_path / "cache"))
    run = run_product(HEADPHONES, services)
    assert run.ok and all("offline" in f for f in run.result.fallbacks)


def test_p17_offline_mode_still_reuses_answers_already_in_the_cache(tmp_path):
    live = _services(tmp_path, CachedLLM(ScriptedLLM(), tmp_path / "cache"))
    run_product(HEADPHONES, live)
    offline = _services(tmp_path, CachedLLM(OfflineLLM(), tmp_path / "cache"))
    run = run_product(HEADPHONES, offline)
    assert run.result.fallbacks == [] and run.result.vision == VISION


def test_p18_open_services_offline_uses_no_network(tmp_path):
    settings = replace(Settings(), cache_dir=tmp_path)
    with open_services(settings, offline=True, remover=fake_remover) as services:
        assert services.scenes.provider == "none"
        with pytest.raises(LLMError, match="offline"):
            services.llm.complete_json(step="copy", model="m", system="s", user="u",
                                       schema=VisionAnalysis)


def test_p18_open_services_closes_the_http_client(tmp_path):
    settings = replace(Settings(), cache_dir=tmp_path, llm_api_key="k", vision_model="m",
                       copy_model="m")
    with open_services(settings) as services:
        client = services.llm._client
    assert client.closed
