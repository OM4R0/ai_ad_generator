"""Social mockup (FR-15, the brief's bonus): the feed ad inside a realistic Instagram post.

What a marketer checks before publishing: the brand's avatar and handle,
"Sponsored", the 1080×1080 creative (pasted 1:1, so it stays sharp), the
"Shop now" bar under it, the action icons, the likes line, and the caption cut
where Instagram cuts it: after two lines, with "… more". That cut is why the
caption must open with the hook (spec §8).

Arabic ads are shown the way an Arabic phone shows them: the whole post is
mirrored (right to left) and the interface words are in Arabic. Every position
below is worked out left-to-right and then mirrored by one function, `_x`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from core.brand_manager import text_color_for
from core.schemas import AdCopy, BrandProfile, Language
from core.text_render import font_for, strip_emoji, to_visual

Box = tuple[int, int, int, int]

MOCKUP_WIDTH = 1080        # = the feed creative's width, so it is pasted 1:1
SIDE = 36                  # left/right padding of the post
HEADER_H = 150
AVATAR = 92                # the avatar including its story ring
CTA_BAR_H = 112
ACTIONS_H = 128
ICON = 64
LINE_H = 54                # likes and caption line height
CAPTION_LINES = 2          # Instagram shows two lines, then "… more"
BOTTOM = 40
SUPERSAMPLE = 4            # shapes are drawn 4× larger and scaled down: smooth edges

TEXT, MUTED, BORDER, WHITE = "#000000", "#737373", "#DBDBDB", "#FFFFFF"
RING = ("#FEDA75", "#FA7E1E", "#D62976", "#962FBF", "#4F5BD5")  # story ring, ↙ to ↗
DEFAULT_PRIMARY = "#111827"     # the visual engine's default too
MAX_HANDLE = 30                 # Instagram's username limit

# The words of Instagram's own interface, in the ad's language. The likes count is
# a plausible number for a preview, not a real metric.
WORDS: dict[Language, dict[str, str]] = {
    Language.EN: {"sponsored": "Sponsored", "cta": "Shop now", "likes": "2,481 likes",
                  "more": "more"},
    Language.AR: {"sponsored": "ممول", "cta": "تسوق الآن", "likes": "2,481 تسجيل إعجاب",
                  "more": "المزيد"},
    Language.FR: {"sponsored": "Sponsorisé", "cta": "Acheter", "likes": "2 481 J'aime",
                  "more": "plus"},
    Language.ES: {"sponsored": "Publicidad", "cta": "Comprar", "likes": "2.481 Me gusta",
                  "more": "más"},
    Language.DE: {"sponsored": "Gesponsert", "cta": "Jetzt einkaufen",
                  "likes": "Gefällt 2.481 Mal", "more": "mehr"},
}


@dataclass
class MockupLayout:
    """Where things were drawn (the tests check these, like visual_engine.Layout)."""

    media_box: Box
    avatar_box: Box
    cta_bar_box: Box
    caption_box: Box
    caption_lines: list[str]      # the caption text drawn after the handle (as typed)
    truncated: bool               # True: cut after two lines and "… more" drawn
    words: dict[str, str]
    handle: str


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def instagram_mockup(feed: Image.Image, brand: BrandProfile, copy: AdCopy, *,
                     logo: Image.Image | None = None) -> tuple[Image.Image, MockupLayout]:
    """The feed creative inside an Instagram feed post (light mode), with its layout.

    logo: shown in the avatar; without one the avatar shows the brand's initials.
    """
    language = copy.language
    rtl = language.direction == "rtl"
    words = WORDS[language]
    handle = instagram_handle(brand.brand_name)
    primary = brand.brand_colors[0] if brand.brand_colors else DEFAULT_PRIMARY
    on_primary = text_color_for(primary)
    width = MOCKUP_WIDTH

    def _x(x: float, w: float) -> int:
        """x of something `w` wide placed `x` from the reading start (mirrored for RTL)."""
        return round(width - x - w if rtl else x)

    # Every text is laid out in the ad's direction (see text_render.to_visual).
    def measure(text: str, font: ImageFont.FreeTypeFont) -> float:
        return font.getlength(to_visual(text, rtl=rtl))

    def write(text: str, font: ImageFont.FreeTypeFont, *, x: int, y: int, fill: str) -> Box:
        return _text(draw, text, font, x=x, y=y, fill=fill, rtl=rtl)

    media = feed.convert("RGB")
    if media.width != width:
        media = media.resize((width, round(media.height * width / media.width)),
                             Image.Resampling.LANCZOS)

    caption = copy.captions["instagram"]
    caption_font = font_for(language, "medium", 34, caption.text)
    handle_font = font_for(Language.EN, "bold", 34, handle)
    space = caption_font.getlength(" ")
    more_font = font_for(language, "medium", 34, words["more"])
    column = width - 2 * SIDE
    handle_w = handle_font.getlength(handle)
    paragraphs = [strip_emoji(p) for p in caption.text.split("\n")]
    if caption.hashtags:
        paragraphs.append(" ".join(caption.hashtags))
    lines, truncated = _caption_lines(
        [p for p in paragraphs if p], caption_font, first=column - handle_w - space,
        rest=column, more=space + measure(words["more"], more_font), measure=measure)

    media_top = HEADER_H
    bar_top = media_top + media.height
    likes_top = bar_top + CTA_BAR_H + ACTIONS_H
    caption_top = likes_top + LINE_H
    height = caption_top + LINE_H * max(1, len(lines)) + BOTTOM

    canvas = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width - 1, height - 1), outline=BORDER, width=2)  # under the media

    # Header: avatar with story ring, handle, "Sponsored", the ••• menu
    avatar_box = (_x(SIDE, AVATAR), (HEADER_H - AVATAR) // 2)
    avatar = _avatar(brand.brand_name, primary, on_primary, logo)
    canvas.paste(avatar, avatar_box, avatar)
    avatar_box = (*avatar_box, avatar_box[0] + AVATAR, avatar_box[1] + AVATAR)
    name_x = SIDE + AVATAR + 24
    write(handle, handle_font, x=_x(name_x, handle_w), y=54, fill=TEXT)
    small = font_for(language, "medium", 28, words["sponsored"])
    write(words["sponsored"], small,
          x=_x(name_x, measure(words["sponsored"], small)), y=98, fill=MUTED)
    for i in range(3):
        dot = _x(width - SIDE - 9 - i * 17, 9)
        draw.ellipse((dot, HEADER_H // 2 - 4, dot + 9, HEADER_H // 2 + 5), fill=TEXT)

    # The ad itself, then the "Shop now" bar in the brand colour
    canvas.paste(media, (0, media_top))
    media_box = (0, media_top, width, bar_top)
    cta_bar_box = (0, bar_top, width, bar_top + CTA_BAR_H)
    draw.rectangle(cta_bar_box, fill=primary)
    cta_font = font_for(language, "bold", 34, words["cta"])
    middle = bar_top + CTA_BAR_H // 2
    write(words["cta"], cta_font, x=_x(SIDE, measure(words["cta"], cta_font)),
          y=middle, fill=on_primary)
    tip = width - SIDE - 4
    chevron = [(tip - 15, middle - 15), (tip, middle), (tip - 15, middle + 15)]
    draw.line([(_x(px, 0), py) for px, py in chevron], fill=on_primary, width=5, joint="curve")

    # Like, comment, share … save
    icon_top = bar_top + CTA_BAR_H + (ACTIONS_H - ICON) // 2
    for i, kind in enumerate(("heart", "comment", "share")):
        icon = _icon(kind, mirrored=rtl)
        canvas.paste(icon, (_x(SIDE - 6 + i * (ICON + 30), ICON), icon_top), icon)
    save = _icon("bookmark", mirrored=rtl)
    canvas.paste(save, (_x(width - SIDE + 6 - ICON, ICON), icon_top), save)

    # Likes, then "handle caption…  more"
    likes_font = font_for(language, "bold", 32, words["likes"])
    write(words["likes"], likes_font,
          x=_x(SIDE, measure(words["likes"], likes_font)), y=likes_top + LINE_H // 2, fill=TEXT)
    boxes: list[Box] = []
    for i, line in enumerate(lines or [""]):
        y = caption_top + i * LINE_H
        start = SIDE
        if i == 0:
            boxes.append(write(handle, handle_font, x=_x(SIDE, handle_w),
                               y=y + LINE_H // 2, fill=TEXT))
            start = SIDE + handle_w + space
        line_w = measure(line, caption_font)
        if line:
            boxes.append(write(line, caption_font, x=_x(start, line_w),
                               y=y + LINE_H // 2, fill=TEXT))
        if truncated and i == len(lines) - 1:
            more_w = measure(words["more"], more_font)
            boxes.append(write(words["more"], more_font,
                               x=_x(start + line_w + space, more_w), y=y + LINE_H // 2,
                               fill=MUTED))
    caption_box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes))

    return canvas, MockupLayout(media_box, avatar_box, cta_bar_box, caption_box,
                                lines, truncated, dict(words), handle)


def instagram_handle(brand_name: str) -> str:
    """A plausible Instagram username: "AeroSonic Audio" → "aerosonic.audio".

    Usernames allow only Latin letters, digits, "." and "_" (≤ 30 characters), so
    accents are dropped and a name with no Latin letters becomes "brand".
    """
    words = re.findall(r"[a-z0-9]+", _ascii(brand_name).lower())
    return ".".join(words)[:MAX_HANDLE].strip(".") or "brand"


# --------------------------------------------------------------------------- #
# Caption
# --------------------------------------------------------------------------- #


def _caption_lines(paragraphs: list[str], font: ImageFont.FreeTypeFont, *, first: float,
                   rest: float, more: float,
                   measure: Callable[[str, ImageFont.FreeTypeFont], float]) -> tuple[list[str], bool]:
    """Wrap like Instagram: the first line is shorter (the handle comes first); after
    CAPTION_LINES lines the text is cut and the last line keeps room for "… more"."""
    lines: list[str] = []
    for paragraph in paragraphs:
        current = ""
        for word in paragraph.split(" "):
            while word:
                limit = first if not lines else rest
                candidate = f"{current} {word}".strip()
                if measure(candidate, font) <= limit:
                    current, word = candidate, ""
                elif current:                    # the word starts the next line
                    lines.append(current)
                    current = ""
                else:                            # one word wider than a whole line
                    cut = len(word)
                    while cut > 1 and measure(word[:cut], font) > limit:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
        if current:
            lines.append(current)
    if len(lines) <= CAPTION_LINES:
        return lines, False

    kept = lines[:CAPTION_LINES]
    limit = (first if CAPTION_LINES == 1 else rest) - more
    last = kept[-1]
    while " " in last and measure(last + "…", font) > limit:   # drop whole words first
        last = last.rsplit(" ", 1)[0]
    while len(last) > 1 and measure(last + "…", font) > limit:
        last = last[:-1]
    kept[-1] = last.rstrip(" ,.;:-") + "…"
    return kept, True


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #


def _text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, *,
          x: int, y: int, fill: str, rtl: bool) -> Box:
    """Draw one line with its left edge at x and its middle at y; returns its box."""
    visual = to_visual(text, rtl=rtl)
    draw.text((x, y), visual, font=font, fill=fill, anchor="lm")
    half = math.ceil(font.size * 0.7)
    return (x, y - half, math.ceil(x + font.getlength(visual)), y + half)


def _avatar(brand_name: str, primary: str, on_primary: str,
            logo: Image.Image | None = None) -> Image.Image:
    """The brand's logo on white (or its initials on its primary colour), inside
    Instagram's story ring."""
    big = AVATAR * SUPERSAMPLE
    ring, gap = 6 * SUPERSAMPLE, 4 * SUPERSAMPLE

    def disc(inset: int) -> Image.Image:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((inset, inset, big - 1 - inset, big - 1 - inset), fill=255)
        return mask.resize((AVATAR, AVATAR), Image.Resampling.LANCZOS)

    outer, inner_edge, face = disc(0), disc(ring), disc(ring + gap)
    ring_mask = Image.composite(Image.new("L", outer.size, 0), outer, inner_edge)  # outer − inner
    avatar = Image.new("RGBA", (AVATAR, AVATAR), (0, 0, 0, 0))
    avatar.paste(_ring_gradient(AVATAR), (0, 0), ring_mask)
    if logo is not None:
        avatar.paste(Image.new("RGB", (AVATAR, AVATAR), WHITE), (0, 0), face)
        side = round(AVATAR * 0.56)                       # fits inside the circle
        scale = min(side / logo.width, side / logo.height)
        small = logo.convert("RGBA").resize((max(1, round(logo.width * scale)),
                                             max(1, round(logo.height * scale))),
                                            Image.Resampling.LANCZOS)
        avatar.alpha_composite(small, ((AVATAR - small.width) // 2, (AVATAR - small.height) // 2))
        return avatar
    avatar.paste(Image.new("RGB", (AVATAR, AVATAR), primary), (0, 0), face)
    initials = _initials(brand_name)
    font = font_for(Language.EN, "bold", 28, initials)
    ImageDraw.Draw(avatar).text((AVATAR / 2, AVATAR / 2), to_visual(initials), font=font,
                                fill=on_primary, anchor="mm")
    return avatar


def _ring_gradient(size: int) -> Image.Image:
    stops = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in RING]
    pixels = []
    for y in range(size):
        for x in range(size):
            t = (x + (size - 1 - y)) / (2 * (size - 1)) * (len(stops) - 1)  # ↙ 0 … ↗ 1
            i = min(int(t), len(stops) - 2)
            f = t - i
            pixels.append(tuple(round(a + (b - a) * f) for a, b in zip(stops[i], stops[i + 1])))
    image = Image.new("RGB", (size, size))
    image.putdata(pixels)
    return image


def _initials(brand_name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", _ascii(brand_name))
    return "".join(w[0] for w in words[:2]).upper() if words else brand_name.strip()[:1] or "?"


def _icon(kind: str, *, mirrored: bool) -> Image.Image:
    """Instagram's outline icons, from their 24-unit drawings (stroke 2)."""
    big = ICON * SUPERSAMPLE
    k = big / 24
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    width = round(2 * k)

    def path(points, closed=False):
        scaled = [(x * k, y * k) for x, y in points]
        draw.line(scaled + (scaled[:1] if closed else []), fill=255, width=width, joint="curve")
        for x, y in (scaled[0], scaled[-1]):            # round line ends
            draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=255)

    if kind == "heart":
        s = 19 / 32
        path([(12 + 16 * math.sin(t) ** 3 * s,
               12.3 - (13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t)
                       - math.cos(4 * t) + 2.5) * s)
              for t in (i * 2 * math.pi / 120 for i in range(120))], closed=True)
    elif kind == "comment":
        r = 9.993
        draw.arc(((12 - r) * k, (12 - r) * k, (12 + r) * k, (12 + r) * k),
                 start=59.6, end=390.1, fill=255, width=width)
        path([(17.066, 20.623), (22, 22), (20.656, 17.008)])
    elif kind == "share":
        path([(11.698, 20.334), (22, 3.001), (2, 3.001), (9.218, 10.084)], closed=True)
        path([(22, 3.001), (9.218, 10.084)])
    elif kind == "bookmark":
        path([(20, 21), (12, 13.44), (4, 21), (4, 3), (20, 3)], closed=True)
    else:
        raise ValueError(f"unknown icon {kind!r}")

    mask = mask.resize((ICON, ICON), Image.Resampling.LANCZOS)
    if mirrored:
        mask = ImageOps.mirror(mask)
    icon = Image.new("RGBA", (ICON, ICON), (0, 0, 0, 0))
    icon.paste(Image.new("RGB", (ICON, ICON), TEXT), (0, 0), mask)
    return icon


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
