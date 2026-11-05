"""Tests for core.copy_engine (FR-5/6/7/9, spec §8). Uses a fake LLM client."""

from __future__ import annotations

import pytest

from config.settings import PROJECT_ROOT
from core.brand_manager import resolve_brand
from core.copy_engine import (
    COPY_PROMPT,
    HASHTAG_LIMITS,
    HOOK_MAX_CHARS,
    find_cliches,
    generate_copy,
    template_copy,
)
from core.excel_loader import load_catalog
from core.llm_client import LLMError
from core.schemas import AdCopy, AdCopyDraft, CallUsage, Language, VisionAnalysis

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)
OUD = CATALOG.by_id(10)
VISION = VisionAnalysis(
    product_type="over-ear headphones", dominant_colors=["#1C1D21"],
    materials_finish=["matte black synthetic leather"], mood="sleek, technical",
    visual_selling_points=["plush ergonomic ear cushions"], scene_prompt="dark studio",
)


def _variant(angle: str, hook: str = "Silence the open office. -42 dB ANC.") -> dict:
    return {"angle": angle, "headline": "60 hours. One charge.", "hook": hook,
            "primary_text": "Body text.", "bullets": ["a", "b", "c"], "cta": "Shop now"}


def _draft(**overrides) -> AdCopyDraft:
    data = {
        "variants": [_variant("pain_point"), _variant("emotional")],
        "carousel": [{"title": "Hook"}, {"title": "Feature"}, {"title": "Buy"}],
        "captions": {p: {"text": "Caption", "hashtags": ["#a", "#b"]}
                     for p in ("instagram", "tiktok", "facebook", "linkedin")},
    }
    data.update(overrides)
    return AdCopyDraft.model_validate(data)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result, CallUsage(step=kwargs["step"], provider="fake", model=kwargs["model"])


def _generate(product, client, vision=VISION, **kw):
    return generate_copy(product, resolve_brand(product), vision, client, model="copy-m", **kw)


# --------------------------------------------------------------------------- #

def test_prompt_contains_catalog_facts_brand_voice_and_vision_findings():
    client = FakeClient(result=_draft())
    _generate(HEADPHONES, client)
    call = client.calls[0]
    prompt = call["system"] + call["user"]
    assert HEADPHONES.usp in prompt and HEADPHONES.features[0] in prompt
    assert HEADPHONES.price_range in prompt and HEADPHONES.target_audience in prompt
    assert resolve_brand(HEADPHONES).brand_voice in prompt
    assert "matte black synthetic leather" in prompt        # multimodal: vision is used
    assert "plush ergonomic ear cushions" in prompt
    assert "Elevate" in prompt                              # banned phrases are listed
    assert call["schema"] is AdCopyDraft and call["model"] == "copy-m"
    assert call["image"] is None                            # the image was analysed in step 3


def test_arabic_prompt_uses_the_arabic_guide_and_register():
    client = FakeClient(result=_draft())
    _generate(OUD, client)
    prompt = client.calls[0]["system"] + client.calls[0]["user"]
    assert "Arabic" in prompt and "Gulf" in prompt
    assert "ارتقِ" in prompt                                 # Arabic banned phrase list


def test_result_language_and_direction_come_from_the_product():
    result = _generate(OUD, FakeClient(result=_draft()))
    assert isinstance(result.copy, AdCopy)
    assert result.copy.language is Language.AR and result.copy.direction == "rtl"
    assert result.prompt_version == COPY_PROMPT and result.fallback is None


def test_variation_number_reaches_the_prompt():
    client = FakeClient(result=_draft())
    _generate(HEADPHONES, client, variation=2)
    assert "variation 2" in client.calls[0]["user"].lower()


def test_llm_failure_gives_template_copy_in_the_product_language():
    result = _generate(OUD, FakeClient(error=LLMError("quota")))
    assert result.usage is None and "quota" in result.fallback
    copy = result.copy
    assert copy.language is Language.AR and copy.direction == "rtl"
    assert copy.variants[0].cta == "تسوّق الآن"
    assert copy.variants[0].bullets == OUD.features[:3]
    assert 3 <= len(copy.carousel) <= 5


@pytest.mark.parametrize("product", [HEADPHONES, OUD, CATALOG.by_id(5), CATALOG.by_id(11),
                                     CATALOG.by_id(4)])
def test_template_copy_is_valid_for_every_language(product):
    copy = template_copy(product, resolve_brand(product))
    assert copy.language is product.language


def test_hashtags_are_trimmed_to_platform_limits():
    many = [f"#tag{i}" for i in range(30)]
    draft = _draft(captions={p: {"text": "t", "hashtags": many}
                             for p in ("instagram", "tiktok", "facebook", "linkedin")})
    result = _generate(HEADPHONES, FakeClient(result=draft))
    for platform, limit in HASHTAG_LIMITS.items():
        assert len(result.copy.captions[platform].hashtags) == limit


def test_cliches_and_long_hooks_are_flagged_not_rejected():
    long_hook = "x" * (HOOK_MAX_CHARS + 10)
    draft = _draft(variants=[_variant("pain_point", "Elevate your listening experience"),
                             _variant("emotional", long_hook)])
    result = _generate(HEADPHONES, FakeClient(result=draft))
    joined = " ".join(result.warnings)
    assert "elevate your" in joined.lower()
    assert "hook" in joined.lower() and str(HOOK_MAX_CHARS) in joined


def test_find_cliches_is_case_insensitive_and_language_specific():
    assert find_cliches("Unleash the GAME-CHANGER", Language.EN) == ["unleash", "game-changer"]
    assert find_cliches("ارتقِ بتجربتك", Language.AR) == ["ارتقِ"]
    assert find_cliches("A calm, 60-hour battery", Language.EN) == []


def test_works_without_a_vision_analysis():
    client = FakeClient(result=_draft())
    result = _generate(HEADPHONES, client, vision=None)
    assert result.copy is not None


# --- Found in the first live run (2026-11-05) ------------------------------ #

def test_hashtags_repeated_inside_the_caption_text_are_removed():
    # Live: the LLM put the hashtags at the end of the text AND in the list → shown twice.
    captions = {p: {"text": "Line one.\n\nLine two 🎧\n\n#ANC #Audio", "hashtags": ["#ANC", "#Audio"]}
                for p in ("instagram", "tiktok", "facebook", "linkedin")}
    result = _generate(HEADPHONES, FakeClient(result=_draft(captions=captions)))
    assert result.copy.captions["instagram"].text == "Line one.\n\nLine two 🎧"


def test_prices_not_in_the_catalog_are_flagged():
    # Live: the LLM wrote "$149" although the catalog says "$129 – $189".
    assert HEADPHONES.price_range == "$129 – $189"
    draft = _draft(variants=[_variant("pain_point") | {"headline": "-42dB ANC for $149"},
                             _variant("emotional") | {"headline": "From $129"}])
    result = _generate(HEADPHONES, FakeClient(result=draft))
    flagged = [w for w in result.warnings if "price" in w]
    assert len(flagged) == 1 and "$149" in flagged[0] and "$129 – $189" in flagged[0]


def test_copy_prompt_v2_rules_from_the_live_review():
    client = FakeClient(result=_draft())
    _generate(HEADPHONES, client)
    system = client.calls[0]["system"]
    assert COPY_PROMPT == "copy_v2"
    assert "LAST carousel card" in system
    assert "Do not put hashtags inside" in system
    assert "price" in system.lower() and HEADPHONES.price_range in client.calls[0]["user"]


@pytest.mark.parametrize("text, flagged", [
    ("dès 149 $", True),          # French/Spanish/German order: number then symbol
    ("ab 149$", True),
    ("بسعر 149 دولار", True),      # Arabic
    ("for 149 USD", True),
    ("only $149", True),
    ("dès 129 $", False),         # in the catalog range → fine
    ("149 مل", False),            # a number that is not a price
])
def test_price_check_understands_european_and_arabic_formats(text, flagged):
    # Live (product #5, French): "dès 190 $" was not even looked at by the checker.
    draft = _draft(variants=[_variant("pain_point") | {"headline": text}, _variant("emotional")])
    result = _generate(HEADPHONES, FakeClient(result=draft))     # catalog: $129 – $189
    assert any("price" in w for w in result.warnings) is flagged


# --- Consistency review (C2): the last-resort fallback must never fail ----- #

def test_c2_template_copy_survives_empty_catalog_text():
    product = OUD.model_copy(update={"usp": "", "description": ""})
    copy = template_copy(product, resolve_brand(product))
    for variant in copy.variants:
        assert variant.headline and variant.hook and variant.primary_text
