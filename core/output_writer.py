"""Output folder writer (FR-11, spec §5, ADR-004).

    output/{Brand}/{Product}/
    ├── copy.json                 the ad copy (brief's shape, spec §5.1)
    ├── creative_feed.png         1080×1080, variant A (pain point)
    ├── creative_feed_b.png       1080×1080, variant B (emotional): the A/B bonus
    ├── creative_story.png        1080×1920
    ├── carousel_slide_1..N.png   1080×1080, N = 3-5
    └── metadata.json             how the ad was made (spec §5.2); written LAST

Reruns overwrite cleanly (ADR-004): the writer first deletes the files it
would itself create (so 5 old slides don't survive next to 3 new ones), and
never touches anything else in the folder.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

from PIL import Image

from core.schemas import CallUsage, CampaignResult
from core.visual_engine import CreativeSet

logger = logging.getLogger(__name__)

MAX_SLUG_LEN = 60   # keeps full paths well under Windows' 260-character limit
# Names Windows reserves for devices: a folder called "CON" can't be created.
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Only files matching these are ever deleted on a rerun.
OWNED_PATTERNS = ("copy.json", "metadata.json", "creative_feed*.png", "creative_story.png",
                  "carousel_slide_*.png", "mockup_instagram.png")


def slugify(text: str, fallback: str) -> str:
    """A folder name that is safe on Windows/macOS/Linux and still readable.

    "Café Crème" → "Cafe_Creme". A name with no Latin letters or digits (e.g. an
    Arabic-only name) becomes `fallback`; the original name is kept in metadata.json.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9-]+", "_", ascii_text).strip("_-")
    slug = slug[:MAX_SLUG_LEN].rstrip("_-")
    if not slug:
        return fallback
    if slug.upper() in WINDOWS_RESERVED:
        slug += "_"
    return slug


def write_campaign(result: CampaignResult, creatives: CreativeSet, output_dir: Path) -> Path:
    """Write one product's campaign and return its folder."""
    folder = _product_folder(Path(output_dir), result)
    folder.mkdir(parents=True, exist_ok=True)
    _remove_previous_files(folder)

    images = {"creative_feed.png": creatives.feed_variants[0],
              "creative_feed_b.png": creatives.feed_variants[1],
              "creative_story.png": creatives.story}
    for i, slide in enumerate(creatives.carousel, start=1):
        images[f"carousel_slide_{i}.png"] = slide
    for name, image in images.items():
        image.convert("RGB").save(folder / name, format="PNG")

    copy_ = result.copy_
    if copy_ is not None:
        _write_json(folder / "copy.json", copy_.model_dump(mode="json"))

    files = sorted([*images, *(["copy.json"] if copy_ is not None else [])])
    result.files = [Path(name) for name in files]
    metadata = result.model_dump(mode="json", by_alias=True, exclude={"copy_"})
    metadata["totals"] = usage_totals(result.usage)
    _write_json(folder / "metadata.json", metadata)  # last: its presence = a complete folder
    return folder


def usage_totals(usage: list[CallUsage]) -> dict:
    """Token and cost totals for the cost tracker (FR-14).

    `estimated_cost_usd` is what all the answers would cost at the reference paid
    price; `spent_this_run_usd` leaves out answers replayed from the cache.
    """
    return {
        "llm_calls": len(usage),
        "cached_calls": sum(u.cached for u in usage),
        "prompt_tokens": sum(u.prompt_tokens for u in usage),
        "completion_tokens": sum(u.completion_tokens for u in usage),
        "estimated_cost_usd": round(sum(u.estimated_cost_usd for u in usage), 6),
        "actual_cost_usd": round(sum(u.actual_cost_usd for u in usage), 6),
        "spent_this_run_usd": round(sum(u.estimated_cost_usd for u in usage if not u.cached), 6),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _product_folder(output_dir: Path, result: CampaignResult) -> Path:
    product = result.product
    brand_dir = output_dir / slugify(product.brand_name, fallback="brand")
    folder = brand_dir / slugify(product.name, fallback=f"product_{product.id}")
    owner = _folder_owner(folder)
    if owner is not None and owner != product.id:
        # Two products share a name: the second gets its ID appended.
        folder = folder.with_name(f"{folder.name}_{product.id}")
    return folder


def _folder_owner(folder: Path) -> int | None:
    """The product ID recorded in an existing folder's metadata.json, if any."""
    try:
        return int(json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
                   ["product"]["id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _remove_previous_files(folder: Path) -> None:
    for pattern in OWNED_PATTERNS:
        for path in folder.glob(pattern):
            path.unlink()


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
