"""Tests for core.schemas: the contracts that validate LLM output (NFR-4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.schemas import AdCopy, BrandProfile, Caption, Language, VisionAnalysis


def _variant(angle: str) -> dict:
    return {
        "angle": angle, "headline": "198 g. Carbon plate. Faster.", "hook": "h",
        "primary_text": "p", "bullets": ["a", "b", "c"], "cta": "Shop now",
    }


def _copy(**overrides) -> dict:
    data = {
        "language": "en", "direction": "ltr",
        "variants": [_variant("pain_point"), _variant("emotional")],
        "carousel": [{"title": "1"}, {"title": "2"}, {"title": "3"}],
        "captions": {p: {"text": "t", "hashtags": ["run"]}
                     for p in ("instagram", "tiktok", "facebook", "linkedin")},
    }
    data.update(overrides)
    return data


def test_language_parse_and_direction():
    assert Language.parse("Arabic") is Language.AR
    assert Language.parse(" english ") is Language.EN
    assert Language.parse("de") is Language.DE
    assert Language.AR.direction == "rtl" and Language.FR.direction == "ltr"
    with pytest.raises(ValueError):
        Language.parse("Klingon")


def test_hex_colours_are_normalised():
    brand = BrandProfile(
        brand_name="X", brand_voice="v", language="ar",
        brand_colors=["#d4af37", "fff", "#1A1A1A"],
    )
    assert brand.brand_colors == ["#D4AF37", "#FFFFFF", "#1A1A1A"]
    with pytest.raises(ValidationError):
        VisionAnalysis(product_type="x", dominant_colors=["gold"])


def test_valid_ad_copy_parses():
    copy = AdCopy.model_validate(_copy())
    assert copy.captions["instagram"].hashtags == ["#run"]


@pytest.mark.parametrize("bad", [
    {"variants": [_variant("pain_point")]},                     # only one A/B variant
    {"carousel": [{"title": "1"}, {"title": "2"}]},             # carousel too short
    {"captions": {"instagram": {"text": "t"}}},                 # platforms missing
    {"unexpected": "field"},                                    # unknown field
])
def test_invalid_ad_copy_is_rejected(bad):
    with pytest.raises(ValidationError):
        AdCopy.model_validate(_copy(**bad))


def test_hashtags_are_cleaned():
    tags = Caption(text="t", hashtags=["oud", "#Luxury Scent", " "]).hashtags
    assert tags == ["#oud", "#LuxuryScent"]
