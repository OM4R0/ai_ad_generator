"""Pipeline: runs steps [1]→[6] for one product, and a batch helper (FR-12, design §1).

The CLI (`main.py`) and the dashboard (`app.py`) both call this module, so they
cannot behave differently.

    photo ─► [3] vision ─► [2] brand (vision colours) ─► [4] copy ─► [5] visuals ─► [6] write

Failure policy (NFR-2, design §8):
- an AI step that fails degrades to its fallback (local analysis, template copy,
  gradient, framed photo) and the reason goes to `fallbacks`: the ad is still made;
- a product that can't be made at all (no photo, an unexpected bug) is reported as
  failed and the batch moves on to the next product.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

from config.settings import Settings
from core.brand_manager import resolve_brand
from core.cache import CachedLLM
from core.copy_engine import generate_copy
from core.image_source import load_product_image
from core.llm_client import LLMClient, LLMError
from core.output_writer import write_campaign
from core.scene_generator import SceneGenerator
from core.schemas import BrandPatch, CampaignResult, Product
from core.vision_agent import analyze_product, photo_warnings
from core.visual_engine import CreativeSet, Remover, build_creatives

logger = logging.getLogger(__name__)


class JsonLLM(Protocol):
    def complete_json(self, **kwargs): ...


@dataclass
class Services:
    """Everything the pipeline talks to, built once per batch (tests pass fakes)."""

    settings: Settings
    llm: JsonLLM                     # normally CachedLLM(LLMClient(settings))
    scenes: SceneGenerator
    remover: Remover | None = None   # None → rembg (local model)


@dataclass
class ProductRun:
    """The outcome for one product: a result, or the reason it failed."""

    product: Product
    result: CampaignResult | None = None
    creatives: CreativeSet | None = None
    folder: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class OfflineLLM:
    """Stands in for the LLM in --offline mode: every call fails cleanly, so the
    steps use their fallbacks. Wrapped in the cache, saved answers are still used."""

    def complete_json(self, **kwargs):
        raise LLMError("offline mode, no AI calls")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_campaign(
    product: Product,
    services: Services,
    *,
    upload: bytes | None = None,
    image_url: str | None = None,
    pdf_patch: BrandPatch | None = None,
    manual_patch: BrandPatch | None = None,
    variation: int = 0,
) -> tuple[CampaignResult, CreativeSet]:
    """Make one product's campaign in memory (nothing is written).

    Raises:
        ImageSourceError: no usable photo (the only failure with no fallback).
    """
    settings = services.settings
    timings: dict[str, float] = {}
    started = clock = time.monotonic()

    def lap(step: str) -> None:
        nonlocal clock
        now = time.monotonic()
        timings[step] = round(now - clock, 2)
        clock = now

    photo, image_source = load_product_image(product, upload=upload, url=image_url)
    lap("image")

    vision = analyze_product(product, photo, services.llm, settings.vision_model)
    lap("vision")

    # Colours from a failed vision step would be the photo's backdrop, not the product.
    vision_colors = vision.analysis.dominant_colors if vision.fallback is None else None
    brand = resolve_brand(product, brands_dir=settings.brands_dir, pdf_patch=pdf_patch,
                          manual_patch=manual_patch, image=photo, vision_colors=vision_colors)

    copy = generate_copy(product, brand, vision.analysis, services.llm, settings.copy_model,
                         variation=variation)
    lap("copy")

    creatives = build_creatives(product, brand, copy.copy, photo, services.scenes,
                                scene_prompt=vision.analysis.scene_prompt,
                                remover=services.remover, seed=variation)
    lap("visuals")
    timings["total"] = round(time.monotonic() - started, 2)

    result = CampaignResult(
        product=product,
        brand=brand,
        vision=vision.analysis,
        copy=copy.copy,
        usage=[u for u in (vision.usage, copy.usage) if u is not None],
        fallbacks=[f for f in (vision.fallback, copy.fallback) if f] + creatives.notes,
        warnings=[*brand.warnings, *photo_warnings(vision.analysis, product), *copy.warnings],
        prompt_versions={"vision": vision.prompt_version, "copy": copy.prompt_version},
        image_source=image_source,
        product_mode=creatives.product_mode,
        background_sources=creatives.background_sources,
        timings_s=timings,
    )
    return result, creatives


def run_product(product: Product, services: Services, *, write: bool = True,
                **options) -> ProductRun:
    """Make one product's campaign and (by default) write its output folder.

    Never raises: a failure is returned as `ProductRun.error` so a batch can go on.
    `options` are passed to `generate_campaign` (upload, image_url, patches, variation).
    """
    try:
        result, creatives = generate_campaign(product, services, **options)
        folder = write_campaign(result, creatives, services.settings.output_dir) if write else None
    except Exception as exc:  # batch boundary: one product must not stop the others
        logger.exception("Product #%s (%s) failed", product.id, product.name)
        return ProductRun(product, error=f"{type(exc).__name__}: {exc}")
    return ProductRun(product, result, creatives, folder)


def run_batch(
    products: Iterable[Product],
    services: Services,
    *,
    on_progress: Callable[[int, int, ProductRun], None] | None = None,
) -> list[ProductRun]:
    """Run products one after another (free tiers allow ~10 requests per minute)."""
    products = list(products)
    runs: list[ProductRun] = []
    for i, product in enumerate(products, start=1):
        run = run_product(product, services)
        runs.append(run)
        if on_progress is not None:
            on_progress(i, len(products), run)
    return runs


@contextmanager
def open_services(
    settings: Settings,
    *,
    offline: bool = False,
    refresh: bool = False,
    remover: Remover | None = None,
) -> Iterator[Services]:
    """Build the real services and close the HTTP connections afterwards.

    offline: no network at all. Answers already in the cache are reused; everything
    else uses the fallbacks (template copy, gradient backgrounds).
    refresh: ignore cached LLM answers (new ones are still saved).
    """
    client: LLMClient | None = None
    if offline:
        llm = CachedLLM(OfflineLLM(), settings.cache_dir)
        scenes = SceneGenerator("none", cache_dir=settings.cache_dir)
    else:
        client = LLMClient(settings)
        llm = CachedLLM(client, settings.cache_dir, refresh=refresh)
        scenes = SceneGenerator(settings.image_provider, cache_dir=settings.cache_dir)
    try:
        yield Services(settings=settings, llm=llm, scenes=scenes, remover=remover)
    finally:
        if client is not None:
            client.close()
