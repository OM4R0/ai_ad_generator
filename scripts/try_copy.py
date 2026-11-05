"""Try the AI part on one product: photo analysis + ad copy, printed and saved.

Usage (from the project folder):
    .venv\\Scripts\\python -m scripts.try_copy 10              # product #10 (Arabic oud)
    .venv\\Scripts\\python -m scripts.try_copy 13 --variation 1  # a fresh variation

This is a temporary helper for experimenting; Phase 6 adds the real CLI (main.py).
Uses 2 LLM requests per run. The result is also saved to .cache/try/copy_<id>.json
(open that file in VS Code to read Arabic correctly).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PIL import Image

from config.settings import Settings
from core.brand_manager import resolve_brand
from core.copy_engine import generate_copy
from core.excel_loader import load_catalog
from core.llm_client import LLMClient
from core.vision_agent import analyze_product


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console can't print Arabic by default
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("product_id", type=int, help="product number from the catalog (1-30)")
    parser.add_argument("--variation", type=int, default=0, help="1, 2, ... for a fresh take")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="! %(message)s")

    settings = Settings.from_env()
    if not settings.llm_configured:
        sys.exit("LLM is not configured: put LLM_API_KEY and VISION_MODEL in .env")
    catalog = load_catalog(settings.catalog_path)
    try:
        product = catalog.by_id(args.product_id)
    except KeyError:
        sys.exit(f"no product with id {args.product_id}; valid ids: 1-{len(catalog.products)}")

    client = LLMClient(settings)
    with Image.open(product.image_path) as photo:
        photo.load()
    brand = resolve_brand(product, image=photo)

    print(f"\n=== {product.name} | {brand.brand_name} | {brand.language.display_name} ===")
    print("Analysing the photo ...")
    vision = analyze_product(product, photo, client, model=settings.vision_model)
    v = vision.analysis
    print(f"  type: {v.product_type} | colours: {v.dominant_colors} | mood: {v.mood}")
    print(f"  materials: {v.materials_finish}")
    print(f"  scene idea: {v.scene_prompt}")
    if vision.fallback:
        print(f"  (fallback used: {vision.fallback})")

    print("Writing the copy ...")
    result = generate_copy(product, brand, v, client, model=settings.copy_model,
                           variation=args.variation)
    copy = result.copy
    for variant in copy.variants:
        print(f"\n--- {variant.angle} ---")
        print(f"Headline: {variant.headline}\nHook:     {variant.hook}")
        print(f"Text:     {variant.primary_text}")
        print("Bullets:  " + " | ".join(variant.bullets))
        print(f"CTA:      {variant.cta}")
    print("\n--- carousel ---")
    for i, card in enumerate(copy.carousel, start=1):
        print(f"{i}. {card.title}: {card.body}")
    for platform, caption in copy.captions.items():
        print(f"\n--- {platform} ---\n{caption.text}\n{' '.join(caption.hashtags)}")

    print("\n=== summary ===")
    if result.usage:
        u = result.usage
        print(f"model: {u.model} | tokens: {u.prompt_tokens} in / {u.completion_tokens} out "
              f"| {u.latency_s}s")
    if result.fallback:
        print(f"FALLBACK USED (template copy): {result.fallback}")
    print("warnings: " + ("; ".join(result.warnings) if result.warnings else "none"))

    target = Path(settings.cache_dir) / "try" / f"copy_{product.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(copy.model_dump_json(indent=2), encoding="utf-8")
    print(f"saved: {target}")


if __name__ == "__main__":
    main()
