"""[5] Visual engine: background + real product + copy → ad creatives (FR-8/9/10).

Hybrid approach (ADR-001): the product is the REAL photo, cut out of its
background; only the scene behind it is AI-generated (scene_generator).

Formats:
- feed 1080×1080, one per A/B variant (feed_a, feed_b)
- story 1080×1920 (keeps the bottom ~250 px free for the platform's UI)
- carousel: one 1080×1080 card per copy.carousel card (3–5): hook → features → CTA

Layout rules (checked by the tests):
- the product sits in its own zone; text boxes never overlap it
- text sits on a scrim in the brand's primary colour, so its colour can be
  chosen by WCAG contrast instead of guessed against a busy scene
- the CTA button uses the brand's accent colour
- Arabic: right-aligned, joined letters, RTL order (text_render)

If background removal fails or returns an unusable mask, the photo is shown
as a framed card instead (the product is still the real one).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterator

from PIL import Image, ImageDraw, ImageFilter

from core.brand_manager import contrast_ratio, text_color_for
from core.scene_generator import SceneGenerator, SceneResult, gradient_background
from core.schemas import AdCopy, BrandProfile, Language, Product
from core.text_render import TextBlock, draw_text_block, fit_text

logger = logging.getLogger(__name__)

FEED_SIZE = (1080, 1080)
STORY_SIZE = (1080, 1920)
MARGIN = 72
STORY_SAFE_BOTTOM = 250        # Instagram/TikTok UI covers the bottom of a story
CTA_HEIGHT = 78
REMBG_MODEL = "isnet-general-use"
MIN_COVERAGE, MAX_COVERAGE = 0.03, 0.97   # a cut-out outside this range is unusable
EDGE_ERODE_PX = 2
MIN_LARGEST_PIECE = 0.70  # below this the cut-out is "in pieces"
# Checked at 256 px with diagonal links: at 96 px the thin headband of the headphones
# broke apart and a good cut-out was rejected (measured on all 30 catalog photos).
PIECES_CHECK_SIDE = 256
_NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]

Remover = Callable[[Image.Image], Image.Image]
Box = tuple[int, int, int, int]


@dataclass
class Layout:
    product_box: Box
    headline_box: Box
    cta_box: Box | None = None
    text_boxes: list[Box] = field(default_factory=list)


@dataclass
class CreativeSet:
    feed_variants: list[Image.Image]
    story: Image.Image
    carousel: list[Image.Image]
    layouts: dict[str, Layout]
    product_mode: str                         # "cutout" | "framed"
    background_sources: dict[str, str]        # {"square": ..., "story": ...}
    notes: list[str] = field(default_factory=list)

    def all_images(self) -> Iterator[tuple[str, Image.Image]]:
        for name, image in zip(("feed_a", "feed_b"), self.feed_variants):
            yield name, image
        yield "story", self.story
        for i, image in enumerate(self.carousel, start=1):
            yield f"carousel_{i}", image


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def build_creatives(
    product: Product,
    brand: BrandProfile,
    copy: AdCopy,
    photo: Image.Image,
    scenes: SceneGenerator,
    *,
    scene_prompt: str,
    remover: Remover | None = None,
    seed: int = 0,
) -> CreativeSet:
    """Render every creative for one product."""
    notes: list[str] = []
    cutout, mode, note = prepare_product(photo, remover or rembg_remover)
    if note:
        notes.append(note)

    palette = _palette(brand)
    # ONE image request per product (the free tier allows ~1 request every few
    # seconds): the tall story scene is generated, and the square is cut from its
    # middle. A gradient is drawn locally at each size instead (free, better framing).
    story_bg = scenes.generate(scene_prompt, STORY_SIZE, brand.brand_colors, seed)
    if story_bg.source == "gradient":
        square_image = gradient_background(FEED_SIZE, brand.brand_colors)
    else:
        top = (STORY_SIZE[1] - FEED_SIZE[1]) // 2
        square_image = story_bg.image.crop((0, top, FEED_SIZE[0], top + FEED_SIZE[1]))
    square = SceneResult(square_image, story_bg.source)
    if story_bg.note:
        notes.append(story_bg.note)

    style = _Style(language=copy.language, palette=palette, brand_name=brand.brand_name)
    layouts: dict[str, Layout] = {}

    feeds = []
    for name, variant in zip(("feed_a", "feed_b"), copy.variants):
        image, layouts[name] = _square_card(square.image, cutout, style,
                                            title=variant.headline, body=None, cta=variant.cta)
        feeds.append(image)

    lead = copy.variants[0]
    story, layouts["story"] = _story(story_bg.image, cutout, style,
                                     headline=lead.headline, hook=lead.hook, cta=lead.cta)

    cards = []
    total = len(copy.carousel)
    for i, card in enumerate(copy.carousel, start=1):
        cta = lead.cta if i == total else None
        image, layouts[f"carousel_{i}"] = _square_card(
            square.image, cutout, style, title=card.title, body=card.body or None,
            cta=cta, page=(i, total))
        cards.append(image)

    return CreativeSet(feeds, story, cards, layouts, mode,
                       {"square": square.source, "story": story_bg.source}, notes)


def prepare_product(photo: Image.Image, remover: Remover) -> tuple[Image.Image, str, str | None]:
    """Cut the product out of its photo; fall back to a framed photo if that fails."""
    try:
        cut = remover(photo).convert("RGBA")
    except Exception as exc:  # external model boundary: any failure → framed photo
        logger.warning("Background removal failed: %s", exc)
        return _framed(photo), "framed", f"cut-out failed ({exc}); used the framed photo"

    alpha = _clean_edge(cut.getchannel("A"))
    cut.putalpha(alpha)
    histogram = alpha.histogram()
    coverage = sum(histogram[129:]) / max(1, cut.width * cut.height)
    bbox = alpha.point(lambda a: 255 if a > 128 else 0).getbbox()
    if bbox is None or not MIN_COVERAGE <= coverage <= MAX_COVERAGE:
        return (_framed(photo), "framed",
                f"cut-out unusable (product covers {coverage:.0%} of the photo); "
                "used the framed photo")
    share = _largest_piece_share(alpha)
    if share < MIN_LARGEST_PIECE:
        # Live: thin sunglasses came out as two floating lenses (the frame was lost).
        return (_framed(photo), "framed",
                f"cut-out fell apart into separate pieces (largest piece {share:.0%}); "
                "used the framed photo")
    return cut.crop(bbox), "cutout", None


def _largest_piece_share(alpha: Image.Image) -> float:
    """Share of the opaque area that belongs to the largest connected piece.

    A real product is usually one object; a cut-out in several large pieces means
    the removal lost part of it. Computed on a small copy (fast, pure Python).
    """
    small = alpha.copy()
    small.thumbnail((PIECES_CHECK_SIDE, PIECES_CHECK_SIDE))
    w, h = small.size
    raw = small.tobytes()
    filled = [v > 128 for v in raw]
    total = sum(filled)
    if total == 0:
        return 0.0
    seen = [False] * (w * h)
    sizes = []
    for start in range(w * h):
        if not filled[start] or seen[start]:
            continue
        seen[start], stack, size = True, [start], 0
        while stack:
            i = stack.pop()
            size += 1
            x, y = i % w, i // w
            for dx, dy in _NEIGHBOURS:            # 8-connected: diagonals count
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    n = ny * w + nx
                    if filled[n] and not seen[n]:
                        seen[n] = True
                        stack.append(n)
        sizes.append(size)
    significant = [s for s in sizes if s >= total * 0.02]   # ignore specks of noise
    return max(sizes) / max(1, sum(significant))


def _clean_edge(alpha: Image.Image) -> Image.Image:
    """Remove the background 'halo' that background removal leaves around a product.

    Live: black headphones on a yellow photo kept a yellow fringe. Shrinking the
    mask by 2 px and dropping semi-transparent pixels removes that spill.
    """
    eroded = alpha.filter(ImageFilter.MinFilter(EDGE_ERODE_PX * 2 + 1))
    return eroded.point(lambda a: 0 if a <= 128 else a)


def rembg_remover(image: Image.Image) -> Image.Image:
    """Background removal with rembg (local model, downloaded once on first use)."""
    from rembg import remove  # imported lazily: heavy, and optional in tests

    return remove(image.convert("RGB"), session=_rembg_session())


@lru_cache(maxsize=1)
def _rembg_session():
    from rembg import new_session

    return new_session(REMBG_MODEL)


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Style:
    language: Language
    palette: dict[str, str]
    brand_name: str

    @property
    def rtl(self) -> bool:
        return self.language.direction == "rtl"

    @property
    def align(self) -> str:
        return "right" if self.rtl else "left"


def _square_card(background, product, style: _Style, *, title, body, cta, page=None):
    """1080×1080: brand mark on top, product in the middle, text + CTA at the bottom."""
    w, h = FEED_SIZE
    canvas = background.convert("RGBA")
    _scrim(canvas, start=560, solid=720, color=style.palette["primary"])
    text_boxes = [_wordmark(canvas, style, y=44)]
    product_box = _place_product(canvas, product, zone=(MARGIN, 118, w - MARGIN, 600))

    draw = ImageDraw.Draw(canvas)
    text_color = style.palette["on_primary"]
    column = w - 2 * MARGIN
    title_block = fit_text(title, style.language, "bold", max_width=column,
                           max_lines=2 if body else 3, max_size=64, min_size=34)
    y = 640
    headline_box = draw_text_block(draw, title_block, x=MARGIN, y=y, width=column,
                                   align=style.align, fill=text_color)
    text_boxes.append(headline_box)
    y = headline_box[3] + 10
    if body:
        body_block = fit_text(body, style.language, "medium", max_width=column,
                              max_lines=2, max_size=34, min_size=24)
        body_box = draw_text_block(draw, body_block, x=MARGIN, y=y, width=column,
                                   align=style.align, fill=text_color)
        text_boxes.append(body_box)
        y = body_box[3] + 10

    cta_box = None
    if cta:
        cta_y = min(y + 18, h - MARGIN - CTA_HEIGHT)
        cta_box = _cta_button(canvas, style, cta, y=cta_y)
        text_boxes.append(cta_box)
    if page:
        _page_dots(canvas, style, *page, y=h - 34)
    return canvas.convert("RGB"), Layout(product_box, headline_box, cta_box, text_boxes)


def _story(background, product, style: _Style, *, headline, hook, cta):
    """1080×1920: brand mark, big product, headline + hook, CTA above the UI safe zone."""
    w, h = STORY_SIZE
    canvas = background.convert("RGBA")
    _scrim(canvas, start=940, solid=1120, color=style.palette["primary"])
    text_boxes = [_wordmark(canvas, style, y=110)]
    product_box = _place_product(canvas, product, zone=(MARGIN, 200, w - MARGIN, 1000))

    draw = ImageDraw.Draw(canvas)
    text_color = style.palette["on_primary"]
    column = w - 2 * MARGIN
    head_block = fit_text(headline, style.language, "bold", max_width=column,
                          max_lines=3, max_size=84, min_size=44)
    headline_box = draw_text_block(draw, head_block, x=MARGIN, y=1060, width=column,
                                   align=style.align, fill=text_color)
    hook_block = fit_text(hook, style.language, "medium", max_width=column,
                          max_lines=3, max_size=44, min_size=28)
    hook_box = draw_text_block(draw, hook_block, x=MARGIN, y=headline_box[3] + 18,
                               width=column, align=style.align, fill=text_color)
    cta_y = min(hook_box[3] + 34, h - STORY_SAFE_BOTTOM - CTA_HEIGHT)
    cta_box = _cta_button(canvas, style, cta, y=cta_y)
    text_boxes += [headline_box, hook_box, cta_box]
    return canvas.convert("RGB"), Layout(product_box, headline_box, cta_box, text_boxes)


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


MIN_BUTTON_CONTRAST = 2.0   # the CTA pill must stand out from the scrim behind it


def _palette(brand: BrandProfile) -> dict[str, str]:
    colors = brand.brand_colors or ["#111827", "#F59E0B"]
    primary = colors[0]
    on_primary = text_color_for(primary)
    accent = colors[1] if len(colors) > 1 else colors[0]
    if contrast_ratio(accent, primary) < MIN_BUTTON_CONTRAST:
        # e.g. two near-identical dark blues from the vision step: the button would
        # vanish into the scrim, so it takes the text colour instead.
        accent = on_primary
    return {"primary": primary, "accent": accent,
            "on_primary": on_primary, "on_accent": text_color_for(accent)}


def _scrim(canvas: Image.Image, *, start: int, solid: int, color: str) -> None:
    """Fade the brand's primary colour in from `start` to fully (≈92%) at `solid`."""
    w, h = canvas.size
    alpha = Image.new("L", (1, h), 0)
    for y in range(start, h):
        t = 1.0 if y >= solid else (y - start) / max(1, solid - start)
        alpha.putpixel((0, y), int(235 * t))
    overlay = Image.new("RGBA", (w, h), color)
    overlay.putalpha(alpha.resize((w, h)))
    canvas.alpha_composite(overlay)


def _place_product(canvas: Image.Image, product: Image.Image, zone: Box) -> Box:
    """Scale the product into the zone (keeping its shape), add a soft shadow, paste it."""
    zx0, zy0, zx1, zy1 = zone
    zone_w, zone_h = zx1 - zx0, zy1 - zy0 - 30          # leave room for the shadow
    scale = min(zone_w / product.width, zone_h / product.height)
    size = (max(1, int(product.width * scale)), max(1, int(product.height * scale)))
    item = product.resize(size, Image.Resampling.LANCZOS)
    x = zx0 + (zone_w - size[0]) // 2
    y = zy0 + (zone_h - size[1]) // 2

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sw, sh = int(size[0] * 0.8), max(16, size[1] // 14)
    sx, sy = x + (size[0] - sw) // 2, y + size[1] - sh // 2
    ImageDraw.Draw(shadow).ellipse((sx, sy, sx + sw, sy + sh), fill=(0, 0, 0, 120))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
    canvas.alpha_composite(item, (x, y))
    return (x, y, x + size[0], y + size[1])


def _wordmark(canvas: Image.Image, style: _Style, *, y: int) -> Box:
    """The brand name as a text logo, coloured for contrast with what's behind it."""
    w = canvas.width
    block = fit_text(style.brand_name.upper(), Language.EN, "medium",
                     max_width=w - 2 * MARGIN, max_lines=1, max_size=32, min_size=22)
    region = canvas.crop((MARGIN, y, w - MARGIN, y + block.height)).convert("RGB")
    background = "#{:02X}{:02X}{:02X}".format(*region.resize((1, 1)).getpixel((0, 0)))
    return draw_text_block(ImageDraw.Draw(canvas), block, x=MARGIN, y=y, width=w - 2 * MARGIN,
                           align=style.align, fill=text_color_for(background))


def _cta_button(canvas: Image.Image, style: _Style, text: str, *, y: int) -> Box:
    """A pill-shaped button in the brand's accent colour."""
    w = canvas.width
    block: TextBlock = fit_text(text, style.language, "bold", max_width=w - 2 * MARGIN - 96,
                                max_lines=1, max_size=34, min_size=24)
    pad_x = 48
    width = int(block.width) + 2 * pad_x
    x0 = w - MARGIN - width if style.rtl else MARGIN
    box = (x0, y, x0 + width, y + CTA_HEIGHT)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, radius=CTA_HEIGHT // 2, fill=style.palette["accent"])
    text_y = y + (CTA_HEIGHT - block.size) // 2 - block.size // 6
    draw_text_block(draw, block, x=x0 + pad_x, y=text_y, width=width - 2 * pad_x,
                    align="center", fill=style.palette["on_accent"])
    return box


def _page_dots(canvas: Image.Image, style: _Style, current: int, total: int, *, y: int) -> None:
    draw = ImageDraw.Draw(canvas)
    gap, r = 26, 6
    start = (canvas.width - (total - 1) * gap) // 2
    order = range(total, 0, -1) if style.rtl else range(1, total + 1)  # RTL reads right→left
    for slot, index in enumerate(order):
        cx = start + slot * gap
        fill = style.palette["accent"] if index == current else style.palette["on_primary"]
        draw.ellipse((cx - r, y - r, cx + r, y + r), fill=fill)


def _framed(photo: Image.Image) -> Image.Image:
    """The original photo as a rounded card with a white border (cut-out fallback)."""
    img = photo.convert("RGB")
    img.thumbnail((900, 900))
    border, radius = 14, 36
    card = Image.new("RGBA", (img.width + 2 * border, img.height + 2 * border), (0, 0, 0, 0))
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *card.size), radius=radius, fill=255)
    white = Image.new("RGBA", card.size, (255, 255, 255, 255))
    card.paste(white, (0, 0), mask)
    inner = Image.new("L", img.size, 0)
    ImageDraw.Draw(inner).rounded_rectangle((0, 0, *img.size), radius=radius - border, fill=255)
    card.paste(img, (border, border), inner)
    return card
