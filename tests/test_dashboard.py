"""Tests for core.dashboard: the dashboard's logic, kept out of app.py so it can be
tested without a browser (FR-10, FR-13, FR-14, ADR-004)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from config.settings import PROJECT_ROOT, Settings
from core.brand_manager import patch_from_pdf, resolve_brand
from core.dashboard import (
    Variant, add_variant, brand_patch_from_form, cost_rows, make_pdf_extractor, merge_patches,
    next_variation, product_choices, product_label, rtl_block, services_for,
)
from core.excel_loader import load_catalog
from core.llm_client import LLMError
from core.pipeline import ProductRun
from core.schemas import BrandPatch, BrandPdfDraft, CallUsage, CampaignResult, Language

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)
SAMPLE_PDF = PROJECT_ROOT / "data" / "sample_brand_guidelines.pdf"


# --------------------------------------------------------------------------- #
# Arabic on the page (FR-10: "the dashboard renders Arabic RTL")
# --------------------------------------------------------------------------- #

def test_d1_arabic_text_is_shown_right_to_left():
    html = rtl_block("حقيبة مقاومة للماء", "rtl")
    assert 'dir="rtl"' in html and "text-align:right" in html
    assert "حقيبة مقاومة للماء" in html


def test_d1_english_text_stays_left_to_right():
    html = rtl_block("Shop now", "ltr")
    assert 'dir="ltr"' in html and "text-align:left" in html


def test_d1_ai_text_cannot_inject_html_into_the_page():
    # The copy comes from an LLM: it is data, never markup.
    html = rtl_block('<script>alert(1)</script> & "quotes"', "ltr")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp;" in html


def test_d1_line_breaks_are_kept():
    assert "line one<br>line two" in rtl_block("line one\nline two", "ltr")


# --------------------------------------------------------------------------- #
# Product picker
# --------------------------------------------------------------------------- #

def test_d2_demo_products_come_first_in_their_order_then_the_rest():
    ids = [p.id for p in product_choices(CATALOG.products, (13, 2, 99))]
    assert ids[:2] == [13, 2]                   # 99 isn't in the catalog: ignored
    assert sorted(ids) == list(range(1, 31))    # every product, once


def test_d2_a_product_label_says_id_name_and_language():
    assert product_label(HEADPHONES) == "#13 · AeroSonic Pro ANC Wireless Headphones · English"


# --------------------------------------------------------------------------- #
# Brand editor → a manual BrandPatch
# --------------------------------------------------------------------------- #

BASE = resolve_brand(HEADPHONES, vision_colors=["#1C1D21", "#C0C0C0"])


def _form(**changes):
    values = dict(voice=BASE.brand_voice, colors=list(BASE.brand_colors),
                  market=BASE.target_market, language=BASE.language)
    values.update(changes)
    return brand_patch_from_form(BASE, **values)


def test_d3_an_untouched_form_is_no_edit_at_all():
    assert _form() is None


def test_d3_colour_pickers_return_lowercase_hex_which_is_still_no_change():
    assert _form(colors=[c.lower() for c in BASE.brand_colors]) is None


def test_d3_only_the_changed_field_goes_into_the_patch():
    patch = _form(voice="Calm and technical.")
    assert patch.brand_voice == "Calm and technical."
    assert patch.brand_colors == [] and patch.target_market is None and patch.language is None


def test_d3_a_changed_colour_sends_the_whole_palette():
    patch = _form(colors=["#ff0000", *BASE.brand_colors[1:]])
    assert patch.brand_colors == ["#FF0000", *BASE.brand_colors[1:]]


def test_d3_clearing_a_field_never_erases_it():
    assert _form(voice="   ", market="") is None


def test_d3_the_language_can_be_changed():
    assert _form(language="ar").language is Language.AR


def test_d3_a_new_edit_is_added_to_the_earlier_edits():
    earlier = BrandPatch(brand_colors=["#FF0000"], brand_voice="Calm.")
    merged = merge_patches(earlier, BrandPatch(brand_voice="Bold."))
    assert merged.brand_voice == "Bold." and merged.brand_colors == ["#FF0000"]


def test_d3_merging_with_nothing_keeps_what_there_is():
    edit = BrandPatch(brand_voice="Calm.")
    assert merge_patches(None, None) is None
    assert merge_patches(None, edit) == edit and merge_patches(edit, None) == edit


# --------------------------------------------------------------------------- #
# Cost tracker (FR-14)
# --------------------------------------------------------------------------- #

def test_d4_cost_rows_show_each_call_and_who_really_answered():
    usage = [CallUsage(step="vision", provider="gemini", model="gemini-a", served_model="gemini-a",
                       prompt_tokens=1000, completion_tokens=200, estimated_cost_usd=0.0012),
             CallUsage(step="copy", provider="openrouter.ai", model="openrouter/free",
                       served_model="vendor/x:free", prompt_tokens=2000, completion_tokens=800,
                       estimated_cost_usd=0.0028, cached=True)]
    result = CampaignResult(product=HEADPHONES, brand=BASE, usage=usage)
    rows = cost_rows(result)
    assert [r["step"] for r in rows] == ["vision", "copy"]
    assert rows[1]["answered by"] == "vendor/x:free"
    assert rows[1]["tokens"] == 2800 and rows[1]["from cache"] is True
    assert rows[0]["est. cost (USD)"] == pytest.approx(0.0012)


def test_d4_no_ai_calls_means_no_rows():
    assert cost_rows(CampaignResult(product=HEADPHONES, brand=BASE)) == []


# --------------------------------------------------------------------------- #
# Regenerate keeps earlier variants in the session (ADR-004)
# --------------------------------------------------------------------------- #

def _run() -> ProductRun:
    return ProductRun(HEADPHONES)


def test_d5_the_first_generation_is_variation_0_then_1_2_3():
    history: list[Variant] = []
    assert next_variation(history) == 0
    history = add_variant(history, _run(), 0)
    history = add_variant(history, _run(), next_variation(history))
    assert [v.variation for v in history] == [0, 1]
    assert next_variation(history) == 2


def test_d5_only_the_last_few_variants_are_kept():
    history: list[Variant] = []
    for i in range(7):
        history = add_variant(history, _run(), i, limit=5)
    assert [v.variation for v in history] == [2, 3, 4, 5, 6]


def test_d5_adding_a_variant_does_not_change_the_old_list():
    history = add_variant([], _run(), 0)
    add_variant(history, _run(), 1)
    assert len(history) == 1


# --------------------------------------------------------------------------- #
# Brand PDF read by the LLM (FR-4)
# --------------------------------------------------------------------------- #

class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.answer, CallUsage(step=kwargs["step"], provider="fake", model=kwargs["model"])


def test_d6_the_extractor_sends_the_pdf_text_and_returns_only_what_it_found():
    llm = FakeLLM(BrandPdfDraft(brand_voice="Confident, precise.", language="English"))
    raw = make_pdf_extractor(llm, "copy-m")("Brand voice: confident.")
    assert raw == {"brand_voice": "Confident, precise.", "language": "English", "brand_colors": []}
    call = llm.calls[0]
    assert call["step"] == "brand_pdf" and call["model"] == "copy-m"
    assert "Brand voice: confident." in call["user"] and call["schema"] is BrandPdfDraft


def test_d6_with_the_sample_pdf_hex_codes_in_the_pdf_beat_the_llm_colours():
    llm = FakeLLM(BrandPdfDraft(brand_voice="Confident, precise and quietly futuristic.",
                                brand_colors=["#000000"], target_market="Remote professionals"))
    extraction = patch_from_pdf(SAMPLE_PDF, extractor=make_pdf_extractor(llm, "copy-m"))
    assert extraction.llm_used
    assert extraction.patch.brand_voice.startswith("Confident")
    assert extraction.patch.brand_colors[:2] == ["#0B1F3A", "#00C2D1"]   # written in the PDF


def test_d6_an_llm_failure_still_gives_the_pdf_colours_and_a_warning():
    extractor = make_pdf_extractor(FakeLLM(error=LLMError("quota")), "copy-m")
    extraction = patch_from_pdf(SAMPLE_PDF, extractor=extractor)
    assert not extraction.llm_used
    assert extraction.patch.brand_colors[0] == "#0B1F3A"
    assert any("quota" in w for w in extraction.warnings)


# --------------------------------------------------------------------------- #
# Services live as long as the app (one HTTP client, not one per click)
# --------------------------------------------------------------------------- #

def test_d7_offline_services_make_no_network_calls(tmp_path):
    settings = replace(Settings(), cache_dir=tmp_path)
    services = services_for(settings, ai=False)
    assert services.scenes.provider == "none"
    with pytest.raises(LLMError, match="offline"):
        services.llm.complete_json(step="copy", model="m", system="s", user="u",
                                   schema=BrandPdfDraft)


def test_d7_asking_twice_returns_the_same_services(tmp_path):
    settings = replace(Settings(), cache_dir=tmp_path, llm_api_key="k",
                       vision_model="m", copy_model="m")
    assert services_for(settings, ai=True) is services_for(settings, ai=True)
    assert services_for(settings, ai=True) is not services_for(settings, ai=False)
