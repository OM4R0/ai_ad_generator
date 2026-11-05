"""Stress tests: push every product, extreme text, odd images and odd brand colours
through the visual engine and check the layout rules still hold. No network.

Rules checked for every creative:
- exact size and RGB mode
- every text box stays inside the canvas
- no text box overlaps the product
- the story CTA stays above the platform UI safe zone
- only the last carousel card has a CTA
- Arabic text is right-aligned, other languages left-aligned
- the CTA button is clearly visible against the scrim behind it
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from config.settings import PROJECT_ROOT
from core.brand_manager import contrast_ratio, resolve_brand
from core.copy_engine import template_copy
from core.excel_loader import load_catalog
from core.scene_generator import SceneGenerator
from core.schemas import AdCopy, BrandPatch, CarouselCard, CopyVariant
from core.visual_engine import (
    FEED_SIZE,
    MARGIN,
    STORY_SAFE_BOTTOM,
    STORY_SIZE,
    build_creatives,
    prepare_product,
)

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")


def ellipse_remover(image: Image.Image) -> Image.Image:
    """A plausible cut-out: an opaque ellipse in the middle of the photo."""
    rgba = image.convert("RGBA")
    mask = Image.new("L", rgba.size, 0)
    w, h = rgba.size
    ImageDraw.Draw(mask).ellipse((w * 0.2, h * 0.2, w * 0.8, h * 0.8), fill=255)
    rgba.putalpha(mask)
    return rgba


class CountingScenes(SceneGenerator):
    def __init__(self):
        super().__init__("none")
        self.calls = 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        return super().generate(*args, **kwargs)


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _render(product, copy=None, brand=None, photo=None, scenes=None):
    brand = brand or resolve_brand(product)
    copy = copy or template_copy(product, brand)
    photo = photo if photo is not None else Image.open(product.image_path).convert("RGB")
    return build_creatives(product, brand, copy, photo, scenes or SceneGenerator("none"),
                           scene_prompt="studio", remover=ellipse_remover), copy


def _assert_layout_rules(result, copy) -> None:
    rtl = copy.direction == "rtl"
    total = len(result.carousel)
    for name, image in result.all_images():
        w, h = image.size
        assert image.mode == "RGB", name
        assert (w, h) == (STORY_SIZE if name == "story" else FEED_SIZE), name
        layout = result.layouts[name]
        for box in layout.text_boxes:
            assert 0 <= box[0] and 0 <= box[1] and box[2] <= w and box[3] <= h, (name, box)
            assert not _overlaps(box, layout.product_box), (name, box, layout.product_box)
        head = layout.headline_box
        if rtl:
            assert abs(head[2] - (w - MARGIN)) <= 2, name
        else:
            assert abs(head[0] - MARGIN) <= 2, name
        if name == "story":
            assert layout.cta_box[3] <= h - STORY_SAFE_BOTTOM, name
        if name.startswith("carousel_"):
            is_last = name == f"carousel_{total}"
            assert (layout.cta_box is not None) == is_last, name


# --------------------------------------------------------------------------- #
# Every product in the catalog (all 5 languages, every niche)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("product", CATALOG.products, ids=lambda p: f"{p.id}-{p.language.value}")
def test_every_catalog_product_renders_within_the_layout_rules(product):
    result, copy = _render(product)
    _assert_layout_rules(result, copy)


# --------------------------------------------------------------------------- #
# Extreme copy
# --------------------------------------------------------------------------- #

def _copy_with(text: str, body: str, product, cards: int = 5) -> AdCopy:
    variant = dict(headline=text, hook=text, primary_text=body, bullets=[body] * 3, cta=text[:40])
    return AdCopy(
        language=product.language, direction=product.language.direction,
        variants=[CopyVariant(angle="pain_point", **variant), CopyVariant(angle="emotional", **variant)],
        carousel=[CarouselCard(title=text, body=body) for _ in range(cards)],
        captions=template_copy(product, resolve_brand(product)).captions,
    )


EXTREMES = {
    "very long": ("Silence the open office " * 15, "Long body sentence. " * 30),
    "one huge word": ("Supercalifragilisticexpialidocious" * 4, "Pneumonoultramicroscopic" * 5),
    "emoji": ("🎧 Silence 🔥 the office ✨ 👍🏽", "Battery 🔋 60h ⚡"),
    "numbers": ("-42 dB · 60 h · 45 mm · $129 – $189", "1/2/3/4/5/6/7/8/9/10"),
    "one char": ("X", "Y"),
}


@pytest.mark.parametrize("case", EXTREMES, ids=list(EXTREMES))
@pytest.mark.parametrize("product_id", [13, 10, 4])   # EN, AR, DE
def test_extreme_copy_still_follows_the_layout_rules(case, product_id):
    product = CATALOG.by_id(product_id)
    text, body = EXTREMES[case]
    if product.language.value == "ar":
        text, body = "فخامة ملكية تدوم 16 ساعة " * 10 + text, "دهن عود كمبودي " * 20 + body
    copy = _copy_with(text, body, product)
    result, _ = _render(product, copy=copy)
    _assert_layout_rules(result, copy)


# --------------------------------------------------------------------------- #
# Odd product images (shape, size, colour mode)
# --------------------------------------------------------------------------- #

def _odd_images() -> dict[str, Image.Image]:
    rgb = Image.new("RGB", (600, 400), "#884422")
    palette = rgb.convert("P", palette=Image.Palette.ADAPTIVE)
    palette.info["transparency"] = 0
    return {
        "tiny 1x1": Image.new("RGB", (1, 1), "#FF0000"),
        "tiny 8x8": Image.new("RGB", (8, 8), "#FF0000"),
        "very wide": Image.new("RGB", (4000, 120), "#3366FF"),
        "very tall": Image.new("RGB", (120, 4000), "#3366FF"),
        "grayscale L": Image.new("L", (500, 500), 128),
        "CMYK": Image.new("CMYK", (500, 500), (0, 50, 100, 0)),
        "palette P + transparency": palette,
        "RGBA": Image.new("RGBA", (500, 500), (10, 200, 10, 128)),
        "LA": Image.new("LA", (500, 500), (100, 200)),
    }


@pytest.mark.parametrize("name", list(_odd_images()))
def test_odd_photos_never_break_rendering(name):
    photo = _odd_images()[name]
    product = CATALOG.by_id(13)
    result, copy = _render(product, photo=photo)
    _assert_layout_rules(result, copy)
    assert result.product_mode in ("cutout", "framed")


def test_prepare_product_handles_every_mode_without_crashing():
    for name, photo in _odd_images().items():
        cut, mode, _ = prepare_product(photo, ellipse_remover)
        assert cut.mode == "RGBA" and cut.width >= 1 and cut.height >= 1, name


# --------------------------------------------------------------------------- #
# Odd brand colours
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("colors", [
    ["#FFFFFF"],                       # white brand
    ["#000000"],                       # black brand
    ["#0E1626", "#05070A"],            # two near-identical darks (real: the oud's vision colours)
    ["#FBC601", "#FDD946"],            # two near-identical yellows
    ["#777777", "#787878"],            # mid grey: hardest case for text contrast
    ["#FF0000", "#FF0000"],            # identical primary and accent
])
def test_cta_button_stays_visible_on_any_brand_palette(colors):
    product = CATALOG.by_id(13)
    brand = resolve_brand(product).model_copy(update={"brand_colors": colors})
    result, copy = _render(product, brand=brand)
    for name in ("feed_a", "story"):
        image = result.feed_variants[0] if name == "feed_a" else result.story
        box = result.layouts[name].cta_box
        y = (box[1] + box[3]) // 2
        pill = image.getpixel((box[0] + 8, y))
        beside = image.getpixel((min(image.width - 1, box[2] + 20), y))
        hexes = ["#{:02X}{:02X}{:02X}".format(*px) for px in (pill, beside)]
        assert contrast_ratio(*hexes) >= 1.8, (colors, name, hexes)


# --------------------------------------------------------------------------- #
# Cost: one image-generation request per product
# --------------------------------------------------------------------------- #

def test_only_one_scene_request_per_product():
    # The free image tier allows about one request every few seconds: the square
    # background is cut from the story background instead of being a second request.
    scenes = CountingScenes()
    result, _ = _render(CATALOG.by_id(13), scenes=scenes)
    assert scenes.calls == 1
    assert set(result.background_sources) == {"square", "story"}
