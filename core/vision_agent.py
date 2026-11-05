"""[3] Vision agent: product photo + catalog facts → VisionAnalysis (FR-3).

One LLM call per product (the multimodal model looks at the image). If the
call fails, a local analysis is built from the pixels and the catalog so the
pipeline continues (NFR-2); the reason is returned in `fallback`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from core.brand_manager import dominant_colors
from core.llm_client import LLMError
from core.prompts import load_prompt
from core.schemas import CallUsage, Product, VisionAnalysis

logger = logging.getLogger(__name__)

# v2 (2026-11-05): also reports visible_brands (the oud photo turned out to be a Chanel bottle)
VISION_PROMPT = "vision_v2"
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


def foreign_brands(analysis: VisionAnalysis, brand_name: str, product_name: str = "") -> list[str]:
    """Brand text visible on the photo that is NOT the advertised brand or product.

    A non-empty result means the reference photo shows another company's product
    (e.g. "BLEU DE CHANEL" on the photo for Al-Andalus Parfums): fine for a demo,
    not fine to publish. The pipeline reports it as a warning.
    """
    own = [_squash(brand_name), _squash(product_name)]
    foreign = []
    for text in analysis.visible_brands:
        key = _squash(text)
        if key and not any(o and (key in o or o in key) for o in own):
            foreign.append(text)
    return foreign


def _squash(text: str) -> str:
    """'Al-Andalus Parfums' → 'alandalusparfums' (case, spaces and punctuation ignored)."""
    return re.sub(r"[\W_]+", "", text.casefold())


def photo_warnings(analysis: VisionAnalysis, product: Product) -> list[str]:
    """Human-readable warnings about the reference photo (shown in metadata + dashboard)."""
    warnings = []
    if not analysis.photo_matches_product:
        shows = analysis.photo_shows or "a different item"
        warnings.append(f"the photo shows {shows}, not the product ({product.name}); "
                        "upload a correct photo before publishing")
    others = foreign_brands(analysis, product.brand_name, product.name)
    if others:
        warnings.append(f"the photo shows another brand ({', '.join(others)}); "
                        f"not publishable as a {product.brand_name} ad")
    return warnings
