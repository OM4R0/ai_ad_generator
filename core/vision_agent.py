"""[3] Vision agent: product photo + catalog facts → VisionAnalysis (FR-3).

One LLM call per product (the multimodal model looks at the image). If the
call fails, a local analysis is built from the pixels and the catalog so the
pipeline continues (NFR-2); the reason is returned in `fallback`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from core.brand_manager import dominant_colors
from core.llm_client import LLMError
from core.prompts import load_prompt
from core.schemas import CallUsage, Product, VisionAnalysis

logger = logging.getLogger(__name__)

VISION_PROMPT = "vision_v1"
VISION_TEMPERATURE = 0.2  # describing a photo: we want consistency, not creativity


class JsonLLM(Protocol):
    """What this module needs from the LLM client (lets tests pass a fake)."""

    def complete_json(self, **kwargs) -> tuple[VisionAnalysis, CallUsage]: ...


@dataclass
class VisionResult:
    analysis: VisionAnalysis
    usage: CallUsage | None
    prompt_version: str
    fallback: str | None = None  # why the local analysis was used, if it was


def analyze_product(product: Product, image: Image.Image, client: JsonLLM, model: str) -> VisionResult:
    """Ask the vision model about the photo; fall back to a local analysis on failure."""
    system, user = load_prompt(
        VISION_PROMPT,
        product_name=product.name,
        brand_name=product.brand_name,
        category=product.category,
        description=product.description,
        features="\n".join(f"- {f}" for f in product.features),
        tone=product.tone,
    )
    try:
        analysis, usage = client.complete_json(
            step="vision", model=model, system=system, user=user,
            schema=VisionAnalysis, image=image, temperature=VISION_TEMPERATURE,
        )
    except LLMError as exc:
        logger.warning("Vision fallback for %s: %s", product.name, exc)
        return VisionResult(local_analysis(product, image), None, VISION_PROMPT,
                            fallback=f"vision: local analysis ({exc})")
    return VisionResult(analysis, usage, VISION_PROMPT)


def local_analysis(product: Product, image: Image.Image) -> VisionAnalysis:
    """No-LLM analysis: catalog text + pixel colours. Less precise, never fails."""
    return VisionAnalysis(
        product_type=product.category,
        dominant_colors=dominant_colors(image, count=3),
        materials_finish=[],
        mood=product.tone,
        visual_selling_points=product.features[:3],
        scene_prompt=(
            f"Minimal premium studio backdrop for a {product.category.lower()} ad, "
            f"soft directional light, subtle shadows, mood: {product.tone.lower()}, "
            "clean empty space in the centre, no text"
        ),
        cutout_quality_hint="unknown",
    )
