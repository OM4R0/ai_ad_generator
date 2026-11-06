"""Text on images: bundled fonts, Arabic shaping + RTL, and fitting text into a box (FR-10).

Why Arabic needs special handling: Pillow draws characters one by one, left to
right. Arabic letters must (1) change shape to join their neighbours and
(2) be ordered right to left. Pillow can do this only with the optional
`raqm` library, which is usually missing on Windows. So we do it ourselves:
- `arabic_reshaper` turns letters into their joined forms,
- `python-bidi` reorders the text for display (RTL, with embedded Latin/numbers),
and we always use Pillow's BASIC layout, so the result is identical on every machine.

Wrapping is done on the *logical* text (the order a person types it); each
line is converted to display form only when it's measured or drawn.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import arabic_reshaper
from PIL import ImageDraw, ImageFont

try:  # python-bidi >= 0.5 exposes get_display at the top level
    from bidi import get_display
except ImportError:  # pragma: no cover - older python-bidi
    from bidi.algorithm import get_display

from config.settings import PROJECT_ROOT
from core.schemas import Language

FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
Weight = Literal["bold", "medium"]
Align = Literal["left", "right", "center"]

_FONT_FILES: dict[tuple[str, str], str] = {
    ("latin", "bold"): "Poppins-Bold.ttf",
    ("latin", "medium"): "Poppins-Medium.ttf",
    ("arabic", "bold"): "IBMPlexSansArabic-Bold.ttf",
    ("arabic", "medium"): "IBMPlexSansArabic-Medium.ttf",
}
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
# Emoji and pictographs: the bundled fonts have no glyphs for them (they would render
# as empty boxes). Captions keep their emoji; only text drawn on images drops them.
# Symbols like ° © ® ™ → – are kept on purpose.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0E\U0000FE0F"
    "\U0000200D\U000E0020-\U000E007F\U00002B50\U00002B55]"
)
ELLIPSIS = "…"


def strip_emoji(text: str) -> str:
    return " ".join(_EMOJI_RE.sub(" ", text).split())
LINE_SPACING = 1.28


def contains_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text))


def to_visual(text: str, rtl: bool | None = None) -> str:
    """Display form of a line: Arabic is shaped (joined) and reordered; other text unchanged.

    rtl: the direction of the ad (True for Arabic ads). Without it the bidi library
    guesses from the line's first letter, and an Arabic line that starts with a Latin
    word ("1680D المقاوم…", live run #23) came out left-to-right, words out of order.
    """
    if not contains_arabic(text):
        return text
    base_dir = None if rtl is None else ("R" if rtl else "L")
    return get_display(arabic_reshaper.reshape(text), base_dir=base_dir)


def font_for(language: Language, weight: Weight, size: int, text: str = "") -> ImageFont.FreeTypeFont:
    """The bundled font for this language/text. Arabic text always gets the Arabic font."""
    script = "arabic" if contains_arabic(text) or (language is Language.AR and not text) else "latin"
    return _load_font(script, weight, size)


@lru_cache(maxsize=256)
def _load_font(script: str, weight: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS_DIR / _FONT_FILES[(script, weight)]
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #


@dataclass
class TextBlock:
    lines: list[str]                       # logical order (as typed)
    size: int
    font: ImageFont.FreeTypeFont
    line_widths: list[float] = field(default_factory=list)
    rtl: bool = False                      # the ad's direction: how each line is laid out

    @property
    def line_height(self) -> int:
        return math.ceil(self.size * LINE_SPACING)

    @property
    def height(self) -> int:
        return self.line_height * len(self.lines)

    @property
    def width(self) -> float:
        return max(self.line_widths, default=0)


def fit_text(
    text: str,
    language: Language,
    weight: Weight,
    *,
    max_width: int,
    max_lines: int,
    max_size: int,
    min_size: int,
    step: int = 2,
) -> TextBlock:
    """Largest font size (max → min) at which `text` wraps into ≤ max_lines lines.

    If it doesn't fit even at min_size, the text is cut after max_lines lines
    and ends with an ellipsis. Words longer than a line are broken by characters.
    """
    text = strip_emoji(text)  # one paragraph, no emoji, collapsed whitespace
    rtl = language.direction == "rtl"
    for size in range(max_size, min_size - 1, -step):
        font = font_for(language, weight, size, text)
        lines = _wrap(text, font, max_width)
        if len(lines) <= max_lines:
            return _block(lines, size, font, rtl)
    font = font_for(language, weight, min_size, text)
    lines = _wrap(text, font, max_width)
    return _block(_truncate(lines, max_lines, font, max_width), min_size, font, rtl)


def _block(lines: list[str], size: int, font: ImageFont.FreeTypeFont, rtl: bool) -> TextBlock:
    return TextBlock(lines, size, font, [_width(line, font) for line in lines], rtl)


def _width(line: str, font: ImageFont.FreeTypeFont) -> float:
    return font.getlength(to_visual(line))


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if _width(candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if _width(word, font) > max_width:  # a single word wider than the line
            pieces = _break_word(word, font, max_width)
            lines.extend(pieces[:-1])
            current = pieces[-1]
    if current:
        lines.append(current)
    return lines


def _break_word(word: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    pieces, current = [], ""
    for ch in word:
        if current and _width(current + ch, font) > max_width:
            pieces.append(current)
            current = ch
        else:
            current += ch
    return pieces + [current]


def _truncate(lines: list[str], max_lines: int, font, max_width: int) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and _width(last + ELLIPSIS, font) > max_width:
        last = last[:-1]
    kept[-1] = last.rstrip() + ELLIPSIS
    return kept


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


def draw_text_block(
    draw: ImageDraw.ImageDraw,
    block: TextBlock,
    *,
    x: int,
    y: int,
    width: int,
    align: Align,
    fill: str | tuple,
) -> tuple[int, int, int, int]:
    """Draw the block inside the column [x, x+width]. Returns the box it occupies."""
    left, right = float("inf"), float("-inf")
    for i, line in enumerate(block.lines):
        visual = to_visual(line, rtl=block.rtl)
        line_width = block.font.getlength(visual)
        if align == "right":
            lx = x + width - line_width
        elif align == "center":
            lx = x + (width - line_width) / 2
        else:
            lx = x
        draw.text((lx, y + i * block.line_height), visual, font=block.font, fill=fill)
        left, right = min(left, lx), max(right, lx + line_width)
    if not block.lines:
        return (x, y, x, y)
    return (math.floor(left), y, math.ceil(right), y + block.height)
