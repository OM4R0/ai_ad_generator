"""Tests for core.vision_agent (FR-3) and core.prompts. Uses a fake LLM client."""

from __future__ import annotations

import pytest
from PIL import Image

from config.settings import PROJECT_ROOT
from core.excel_loader import load_catalog
from core.llm_client import LLMError
from core.prompts import load_prompt
from core.schemas import CallUsage, VisionAnalysis
from core.vision_agent import VISION_PROMPT, analyze_product

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)

GOOD = VisionAnalysis(
    product_type="over-ear headphones", dominant_colors=["#111111"],
    materials_finish=["matte plastic", "leather cushions"], mood="sleek, premium",
    visual_selling_points=["cushioned ear cups"], scene_prompt="dark studio, soft rim light",
    cutout_quality_hint="busy background",
)


class FakeClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result, CallUsage(step=kwargs["step"], provider="fake", model=kwargs["model"])


def _image() -> Image.Image:
    return Image.open(HEADPHONES.image_path)


def test_prompt_template_renders_and_rejects_missing_values():
    system, user = load_prompt(VISION_PROMPT, product_name="X", brand_name="B", category="C",
                               description="D", features="- f", tone="T")
    assert "JSON" in system and "X" in user
    with pytest.raises(KeyError):
        load_prompt(VISION_PROMPT, product_name="X")


def test_analyze_sends_image_and_catalog_facts_and_returns_the_analysis():
    client = FakeClient(result=GOOD)
    result = analyze_product(HEADPHONES, _image(), client, model="vision-m")
    assert result.analysis == GOOD and result.fallback is None
    assert result.usage.model == "vision-m"
    call = client.calls[0]
    assert call["image"] is not None and call["schema"] is VisionAnalysis
    assert call["model"] == "vision-m" and call["temperature"] <= 0.3
    assert HEADPHONES.name in call["user"] and HEADPHONES.features[0] in call["user"]
    assert result.prompt_version == VISION_PROMPT


def test_llm_failure_falls_back_to_a_local_analysis():
    client = FakeClient(error=LLMError("quota exceeded"))
    result = analyze_product(HEADPHONES, _image(), client, model="vision-m")
    assert result.usage is None
    assert "quota exceeded" in result.fallback
    assert result.analysis.product_type == HEADPHONES.category
    assert result.analysis.dominant_colors          # from the pixels
    assert result.analysis.scene_prompt              # still usable by the scene generator


def test_vision_prompt_asks_for_product_colours_not_background():
    # Lesson from Phase 2: the photos have coloured backgrounds (yellow behind black headphones).
    system, _ = load_prompt(VISION_PROMPT, product_name="X", brand_name="B", category="C",
                            description="D", features="- f", tone="T")
    assert "background" in system.lower() and "product itself" in system.lower()
