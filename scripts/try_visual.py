"""Try the visual part on one product: real cut-out + background + layout. No Gemini requests.

Usage (from the project folder):
    .venv\\Scripts\\python -m scripts.try_visual 13             # headphones, AI background
    .venv\\Scripts\\python -m scripts.try_visual 23 --offline   # brand gradient, no network at all

Copy: uses the copy saved by `scripts.try_copy` (.cache/try/copy_<id>.json) if it
exists, otherwise plain template copy from the catalog. Output: .cache/try/visual_<id>/
A helper for studying Phase 5. The full pipeline (all steps + output folder) is main.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from config.settings import Settings
from core.brand_manager import resolve_brand
from core.copy_engine import template_copy
from core.excel_loader import load_catalog
from core.image_source import load_product_image
from core.scene_generator import SceneGenerator
from core.schemas import AdCopy
from core.visual_engine import build_creatives


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("product_id", type=int, help="product number from the catalog (1-30)")
    parser.add_argument("--offline", action="store_true", help="brand gradient instead of an AI scene")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="! %(message)s")

    settings = Settings.from_env()
    catalog = load_catalog(settings.catalog_path)
    try:
        product = catalog.by_id(args.product_id)
    except KeyError:
        sys.exit(f"no product with id {args.product_id}; valid ids: 1-{len(catalog.products)}")

    photo, photo_source = load_product_image(product)
    brand = resolve_brand(product, image=photo)
    saved = Path(settings.cache_dir) / "try" / f"copy_{product.id}.json"
    if saved.exists():
        copy, copy_source = AdCopy.model_validate_json(saved.read_text(encoding="utf-8")), "saved AI copy"
    else:
        copy, copy_source = template_copy(product, brand), "template copy (run scripts.try_copy first for AI copy)"

    scenes = SceneGenerator("none" if args.offline else settings.image_provider,
                            cache_dir=settings.cache_dir)
    print(f"\n=== {product.name} | {brand.language.display_name} ===")
    print(f"photo: {photo_source} | copy: {copy_source} | colours: {brand.brand_colors}")
    print("Rendering (the first run downloads the background-removal model) ...")
    started = time.time()
    result = build_creatives(product, brand, copy, photo, scenes,
                             scene_prompt=f"premium studio backdrop for {product.category.lower()}")
    out = Path(settings.cache_dir) / "try" / f"visual_{product.id}"
    out.mkdir(parents=True, exist_ok=True)
    for name, image in result.all_images():
        image.save(out / f"{name}.jpg", quality=90)
    print(f"done in {time.time() - started:.0f}s | product: {result.product_mode} "
          f"| background: {result.background_sources['story']}")
    for note in result.notes:
        print(f"  note: {note}")
    print(f"saved {len(list(out.glob('*.jpg')))} images to: {out}")


if __name__ == "__main__":
    main()
