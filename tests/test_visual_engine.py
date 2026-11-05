"""Tests for core.visual_engine (FR-8/9/10). No network, no real background removal."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from config.settings import PROJECT_ROOT
from core.brand_manager import resolve_brand
from core.copy_engine import template_copy
from core.excel_loader import load_catalog
from core.scene_generator import SceneGenerator
from core.visual_engine import FEED_SIZE, MARGIN, STORY_SIZE, build_creatives

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES, OUD, JACKET = CATALOG.by_id(13), CATALOG.by_id(10), CATALOG.by_id(4)  # EN, AR, DE
RED = (220, 20, 20)


def fake_remover(image: Image.Image) -> Image.Image:
    """A 'cut-out': a red square in the middle of a transparent canvas."""
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    w, h = image.size
    out.paste(Image.new("RGBA", (w // 2, h // 2), (*RED, 255)), (w // 4, h // 4))
    return out


def broken_remover(image: Image.Image) -> Image.Image:
    raise RuntimeError("model not installed")


def empty_remover(image: Image.Image) -> Image.Image:
    return Image.new("RGBA", image.size, (0, 0, 0, 0))


def _build(product, tmp_path: Path, remover=fake_remover):
    brand = resolve_brand(product)
    copy = template_copy(product, brand)
    photo = Image.open(product.image_path).convert("RGB")
    scenes = SceneGenerator(provider="none", cache_dir=tmp_path)
    return build_creatives(product, brand, copy, photo, scenes, scene_prompt="studio",
                           remover=remover), brand, copy


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _close(pixel, color, tol=40) -> bool:
    return all(abs(p - c) <= tol for p, c in zip(pixel, color))


def _hex(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


# --------------------------------------------------------------------------- #

def test_all_formats_are_produced_at_exact_sizes(tmp_path):
    result, _, copy = _build(HEADPHONES, tmp_path)
    assert len(result.feed_variants) == 2                       # A/B
    assert all(img.size == FEED_SIZE for img in result.feed_variants)
    assert result.story.size == STORY_SIZE
    assert len(result.carousel) == len(copy.carousel)
    assert all(img.size == FEED_SIZE and img.mode == "RGB" for img in result.carousel)


def test_the_real_product_is_visible_in_every_creative(tmp_path):
    result, _, _ = _build(HEADPHONES, tmp_path)
    for name, image in result.all_images():
        box = result.layouts[name].product_box
        center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        assert _close(image.getpixel(center), RED), name


@pytest.mark.parametrize("product", [HEADPHONES, OUD, JACKET])
def test_text_never_overlaps_the_product_and_stays_on_canvas(tmp_path, product):
    result, _, _ = _build(product, tmp_path)
    for name, image in result.all_images():
        layout = result.layouts[name]
        w, h = image.size
        for box in layout.text_boxes:
            assert not _overlaps(box, layout.product_box), name
            assert box[0] >= 0 and box[1] >= 0 and box[2] <= w and box[3] <= h, name


def test_english_text_is_left_aligned_and_arabic_right_aligned(tmp_path):
    en, _, _ = _build(HEADPHONES, tmp_path)
    ar, _, _ = _build(OUD, tmp_path)
    en_box = en.layouts["feed_a"].headline_box
    ar_box = ar.layouts["feed_a"].headline_box
    assert abs(en_box[0] - MARGIN) <= 2
    assert abs(ar_box[2] - (FEED_SIZE[0] - MARGIN)) <= 2


def test_the_cta_button_uses_the_brand_accent_colour(tmp_path):
    result, brand, _ = _build(OUD, tmp_path)          # config colours: #1A1A1A, #D4AF37
    box = result.layouts["feed_a"].cta_box
    pixel = result.feed_variants[0].getpixel((box[0] + 6, (box[1] + box[3]) // 2))
    assert _close(pixel, _hex(brand.brand_colors[1]))


@pytest.mark.parametrize("remover", [broken_remover, empty_remover])
def test_a_failed_cutout_falls_back_to_a_framed_photo(tmp_path, remover):
    result, _, _ = _build(HEADPHONES, tmp_path, remover=remover)
    assert result.product_mode == "framed"
    assert any("cut-out" in note for note in result.notes)
    assert result.story.size == STORY_SIZE


def test_background_source_is_reported(tmp_path):
    result, _, _ = _build(HEADPHONES, tmp_path)
    assert result.background_sources == {"square": "gradient", "story": "gradient"}


# --- Found in the first live render: yellow halo around the headphones ----- #

def fringe_remover(image: Image.Image) -> Image.Image:
    """Opaque red product with a YELLOW fringe (background spill), like the live render:
    an outer faint ring (alpha 110) and an inner semi-opaque ring (alpha 200)."""
    out = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    out.paste(Image.new("RGBA", (108, 108), (255, 220, 0, 110)), (46, 46))
    out.paste(Image.new("RGBA", (104, 104), (255, 220, 0, 200)), (48, 48))
    out.paste(Image.new("RGBA", (100, 100), (*RED, 255)), (50, 50))
    return out


def test_semi_transparent_background_fringe_is_removed():
    from core.visual_engine import prepare_product

    cut, mode, _ = prepare_product(Image.new("RGB", (200, 200)), fringe_remover)
    assert mode == "cutout"
    raw = cut.tobytes()
    pixels = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]
    yellow = [p for p in pixels if p[3] > 40 and p[0] > 200 and p[1] > 180 and p[2] < 90]
    assert yellow == []
