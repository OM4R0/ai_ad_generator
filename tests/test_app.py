"""Tests for app.py, the Streamlit dashboard (FR-10, FR-13, FR-14, ADR-004).

`AppTest` runs the real app.py with no browser. The AI and the background remover
are fakes (same as the pipeline tests), so these tests use no quota and no network.
"""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT, Settings
from core import dashboard
from core.pipeline import Services
from core.scene_generator import SceneGenerator
from core.schemas import BrandPdfDraft, CallUsage
from tests.test_pipeline import VISION, PhotoScenes, ScriptedLLM
from tests.test_visual_engine import fake_remover

APP = str(PROJECT_ROOT / "app.py")
SAMPLE_PDF = PROJECT_ROOT / "data" / "sample_brand_guidelines.pdf"


class DashboardLLM(ScriptedLLM):
    """ScriptedLLM that also answers the brand-PDF step."""

    def complete_json(self, **kwargs):
        if kwargs["step"] == "brand_pdf":
            self.calls.append(kwargs)
            return (BrandPdfDraft(brand_voice="Confident, precise and quietly futuristic."),
                    CallUsage(step="brand_pdf", provider="fake", model=kwargs["model"]))
        return super().complete_json(**kwargs)


class ReplayLLM(DashboardLLM):
    """Every answer comes from the cache (costs nothing, so it must not count)."""

    def complete_json(self, **kwargs):
        value, usage = super().complete_json(**kwargs)
        return value, usage.model_copy(update={"cached": True})


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Build an AppTest with fake services. Returns (app, llm, settings).
    Each test gets its own rate limiter: the real one is shared by the whole server."""
    def build(llm=None, limiter=None, scenes=None, **settings_changes):
        settings = replace(Settings(), output_dir=tmp_path / "output", cache_dir=tmp_path / "cache",
                           llm_api_key="test-key", vision_model="vision-m", copy_model="copy-m",
                           fallback_api_key="", fallback_models=[])
        settings = replace(settings, **settings_changes)
        llm = llm or DashboardLLM()
        services = Services(settings=settings, llm=llm,
                            scenes=scenes or SceneGenerator("none", cache_dir=tmp_path),
                            remover=fake_remover)
        limiter = limiter or dashboard.RateLimiter()
        monkeypatch.setattr(dashboard, "load_settings", lambda: settings)
        monkeypatch.setattr(dashboard, "services_for", lambda s, *, ai, remover=None: services)
        monkeypatch.setattr(dashboard, "ai_limiter", lambda: limiter)
        app = AppTest.from_file(APP, default_timeout=120)
        app.run()
        return app, llm, settings
    return build


def _generate(app, product_id=13):
    app.selectbox(key="product").set_value(product_id).run()
    app.button(key="generate").click().run()
    return app


def _variants(app, product_id=13):
    return app.session_state["variants"].get(product_id, [])


def _png(size=(600, 400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "green").save(buffer, format="PNG")
    return buffer.getvalue()


def _no_crash(app):
    assert not app.exception, [e.value for e in app.exception]


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #

def test_a1_the_app_starts_and_offers_every_product_demo_first(make_app):
    app, _, _ = make_app()
    _no_crash(app)
    picker = app.selectbox(key="product")
    assert len(picker.options) == 30
    assert picker.options[0].startswith("#2 ·")              # ADR-005 demo order
    assert any("Generate" in i.value for i in app.info)      # tells you what to do


def test_a2_without_a_key_the_ai_switch_is_off_and_says_why(make_app):
    app, _, _ = make_app(llm_api_key="")
    _no_crash(app)
    switch = app.toggle(key="use_ai")
    assert switch.value is False and switch.disabled
    assert any("template" in c.value for c in app.sidebar.caption)


# --------------------------------------------------------------------------- #
# Generating and showing an ad
# --------------------------------------------------------------------------- #

def test_a3_generate_shows_feed_a_and_b_the_story_and_every_carousel_card(make_app):
    app, llm, _ = make_app()
    _generate(app, 13)
    _no_crash(app)
    assert not app.error
    run = _variants(app)[-1].run
    # photo + feed A/B + story + carousel cards + Instagram preview
    assert len(app.image) == 1 + 2 + 1 + len(run.creatives.carousel) + 1
    assert [c["step"] for c in llm.calls] == ["vision", "copy"]


def _captions(app) -> list[str]:
    return [caption for image in app.image for caption in image.captions]


def test_a3_the_product_photo_is_shown_side_by_side_with_the_ads(make_app):
    # Brief, "Live Preview": product image side-by-side with generated ad creatives.
    app, _, _ = make_app()
    _generate(app, 13)
    captions = _captions(app)
    photo = next(i for i, c in enumerate(captions) if c.startswith("Product photo"))
    assert captions[photo + 1].startswith("A ·") and captions[photo + 2].startswith("B ·")


def test_a3_the_instagram_preview_tab_shows_the_ad_inside_a_post(make_app):
    # Bonus: "Social Mockup Preview".
    app, _, _ = make_app()
    _generate(app, 13)
    assert "Instagram preview" in [tab.label for tab in app.tabs]
    assert any("Instagram" in c for c in _captions(app))


def test_a4_arabic_copy_is_shown_right_to_left(make_app):
    app, _, _ = make_app()
    _generate(app, 23)
    _no_crash(app)
    assert any('dir="rtl"' in md.value for md in app.markdown)


def test_a5_photo_warnings_are_shown_to_the_user(make_app):
    wrong = VISION.model_copy(update={"photo_matches_product": False,
                                      "photo_shows": "an iced coffee"})
    app, _, _ = make_app(DashboardLLM(vision=wrong))
    _generate(app, 13)
    assert any("iced coffee" in w.value for w in app.warning)


def test_a6_the_cost_tab_lists_every_ai_call(make_app):
    app, _, _ = make_app()
    _generate(app, 13)
    [table] = app.dataframe
    assert list(table.value["step"]) == ["vision", "copy"]


# --------------------------------------------------------------------------- #
# Regenerate (ADR-004: earlier variants stay in the session)
# --------------------------------------------------------------------------- #

def test_a7_regenerate_keeps_the_first_variant_and_asks_for_a_new_angle(make_app):
    app, llm, _ = make_app()
    _generate(app, 13)
    app.button(key="regenerate").click().run()
    _no_crash(app)
    assert [v.variation for v in _variants(app)] == [0, 1]
    assert "variation 1" in llm.calls[-1]["user"]


def test_a8_each_product_keeps_its_own_variants(make_app):
    app, _, _ = make_app()
    _generate(app, 13)
    app.selectbox(key="product").set_value(23).run()
    assert _variants(app, 23) == [] and any("Generate" in i.value for i in app.info)
    app.selectbox(key="product").set_value(13).run()
    assert len(_variants(app, 13)) == 1 and len(app.image) > 0


# --------------------------------------------------------------------------- #
# Photo source (FR-13: image source choice)
# --------------------------------------------------------------------------- #

def test_a9_an_uploaded_photo_replaces_the_catalog_photo(make_app):
    app, llm, _ = make_app()
    app.radio(key="image_source").set_value("Upload a photo").run()
    app.file_uploader(key="upload").set_value(("clean.png", _png((600, 400)), "image/png")).run()
    _generate(app, 23)
    _no_crash(app)
    assert _variants(app, 23)[-1].run.result.image_source == "upload"
    assert llm.calls[0]["image"].size == (600, 400)


def test_a9_the_ai_can_paint_the_product_photo(make_app, tmp_path):
    app, _, _ = make_app(scenes=PhotoScenes(tmp_path))
    app.radio(key="image_source").set_value("AI-generated photo").run()
    _generate(app, 13)
    _no_crash(app)
    assert _variants(app)[-1].run.result.image_source == "ai"
    assert any("AI-generated" in w.value for w in app.warning)     # check before publishing


def test_a10_a_broken_upload_shows_an_error_not_a_crash(make_app):
    app, _, _ = make_app()
    app.radio(key="image_source").set_value("Upload a photo").run()
    app.file_uploader(key="upload").set_value(("bad.png", b"not an image", "image/png")).run()
    _generate(app, 13)
    _no_crash(app)
    assert any("image" in e.value.lower() for e in app.error)
    assert _variants(app) == []
    assert not app.button(key="generate").disabled      # a failure never leaves it locked


# --------------------------------------------------------------------------- #
# Brand editor + PDF (FR-4, FR-13)
# --------------------------------------------------------------------------- #

def test_a11_an_untouched_brand_form_never_overrides_the_vision_colours(make_app):
    # Trap: the form is first filled before the vision step has run. If its old
    # values were sent back on the next click, they'd replace the product colours.
    app, _, _ = make_app()
    _generate(app, 13)
    app.button(key="regenerate").click().run()
    brand = _variants(app)[-1].run.result.brand
    assert "manual" not in brand.sources
    assert brand.brand_colors[:2] == VISION.dominant_colors


def test_a12_a_colour_edit_is_used_in_the_next_generation(make_app):
    app, _, _ = make_app()
    _generate(app, 13)
    app.color_picker(key=_key(app, "color_0")).set_value("#ff0000").run()
    app.button(key="regenerate").click().run()
    assert _variants(app)[-1].run.result.brand.brand_colors[0] == "#FF0000"


def _read_pdf(app, product_id=13, data=None):
    app.selectbox(key="product").set_value(product_id).run()
    app.file_uploader(key="brand_pdf").set_value(
        ("guidelines.pdf", data or SAMPLE_PDF.read_bytes(), "application/pdf")).run()
    app.button(key="read_pdf").click().run()
    return app


def test_a12_a_colour_edit_stays_on_every_later_regenerate(make_app):
    # Trap: after one regenerate the form shows the edited colour, so it no longer
    # "differs" from the result; the edit must still be sent next time.
    app, _, _ = make_app()
    _generate(app, 13)
    app.color_picker(key=_key(app, "color_0")).set_value("#ff0000").run()
    app.button(key="regenerate").click().run()
    app.button(key="regenerate").click().run()
    assert [v.run.result.brand.brand_colors[0] for v in _variants(app)] == ["#1C1D21", "#FF0000", "#FF0000"]


def test_a13_reading_the_brand_pdf_fills_the_brand(make_app):
    app, llm, _ = make_app()
    _read_pdf(app, 13)
    _no_crash(app)
    assert any(c["step"] == "brand_pdf" for c in llm.calls)
    assert app.session_state["pdf_patches"]["AeroSonic Audio"].brand_colors[0] == "#0B1F3A"
    assert app.color_picker(key=_key(app, "color_0")).value.upper() == "#0B1F3A"
    _generate(app, 13)
    brand = _variants(app)[-1].run.result.brand
    assert brand.brand_colors[0] == "#0B1F3A" and "pdf" in brand.sources


def test_a13_a_pdf_read_for_one_brand_is_not_used_for_another_brand(make_app):
    app, _, _ = make_app()
    _read_pdf(app, 13)                     # AeroSonic Audio's guidelines
    _generate(app, 23)                     # UrbanNomad Gear
    assert "pdf" not in _variants(app, 23)[-1].run.result.brand.sources



def test_a14_a_file_that_is_not_a_pdf_shows_an_error(make_app):
    app, _, _ = make_app()
    _read_pdf(app, 13, data=b"hello")
    _no_crash(app)
    assert any("pdf" in e.value.lower() for e in app.error)


# --------------------------------------------------------------------------- #
# Save and export
# --------------------------------------------------------------------------- #

def test_a15_save_writes_the_chosen_variant_to_the_output_folder(make_app):
    app, _, settings = make_app()
    _generate(app, 13)
    app.button(key="save").click().run()
    _no_crash(app)
    assert len(list(settings.output_dir.glob("*/*/metadata.json"))) == 1
    assert any("Saved" in s.value for s in app.success)


def test_a16_nothing_is_written_until_save_is_pressed(make_app):
    app, _, settings = make_app()
    _generate(app, 13)
    assert not settings.output_dir.exists()


def test_a17_a_zip_download_is_offered_after_generating(make_app):
    app, _, _ = make_app()
    assert not app.get("download_button")
    _generate(app, 13)
    assert len(app.get("download_button")) == 1


def _key(app, prefix: str) -> str:
    """The brand form's widget keys end with a version that changes when the brand
    it shows changes (e.g. after the vision step); the app stores the current one."""
    return f"{prefix}_{app.session_state['brand_form_key']}"


# --------------------------------------------------------------------------- #
# Double clicks and the AI rate limit (5 new AI ads per 10 minutes)
# --------------------------------------------------------------------------- #

def test_a18_the_buttons_are_locked_while_an_ad_is_being_made(make_app, monkeypatch):
    # A second click during a generation would start a second one and spend quota.
    from streamlit.delta_generator import DeltaGenerator
    events: list[tuple[str, bool | None]] = []
    real_button = DeltaGenerator.button

    def spy(self, label, *args, **kwargs):
        if kwargs.get("key") == "generate":
            events.append(("drawn", bool(kwargs.get("disabled"))))
        return real_button(self, label, *args, **kwargs)

    monkeypatch.setattr(DeltaGenerator, "button", spy)
    llm = DashboardLLM()
    ask = llm.complete_json
    llm.complete_json = lambda **kw: events.append(("ai", None)) or ask(**kw)
    app, _, _ = make_app(llm)
    _generate(app, 13)
    _no_crash(app)
    first_ai_call = events.index(("ai", None))
    assert [e for e in events[:first_ai_call] if e[0] == "drawn"][-1] == ("drawn", True)
    assert events[-1] == ("drawn", False)                   # unlocked when it's done
    assert not app.button(key="generate").disabled


def test_a19_after_the_limit_generate_says_when_to_retry_and_calls_no_ai(make_app):
    app, llm, _ = make_app(limiter=dashboard.RateLimiter(limit=1, window_s=600))
    _generate(app, 13)
    calls = len(llm.calls)
    app.button(key="regenerate").click().run()
    _no_crash(app)
    assert len(llm.calls) == calls                            # no new request
    assert len(_variants(app)) == 1
    assert any("limit" in w.value.lower() and "min" in w.value for w in app.warning)
    assert not app.button(key="generate").disabled


def test_a19_answers_from_the_cache_do_not_count(make_app):
    app, _, _ = make_app(ReplayLLM(), limiter=dashboard.RateLimiter(limit=1, window_s=600))
    _generate(app, 13)
    app.button(key="regenerate").click().run()
    assert len(_variants(app)) == 2


def test_a19_with_ai_off_the_limit_does_not_apply(make_app):
    app, _, _ = make_app(limiter=dashboard.RateLimiter(limit=1, window_s=600))
    _generate(app, 13)
    app.toggle(key="use_ai").set_value(False).run()
    app.button(key="regenerate").click().run()
    assert len(_variants(app)) == 2 and not app.warning
