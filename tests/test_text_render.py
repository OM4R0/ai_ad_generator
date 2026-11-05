"""Tests for core.text_render: fonts, Arabic shaping/RTL, text fitting (FR-10)."""

from __future__ import annotations

from PIL import Image, ImageDraw

from core.schemas import Language
from core.text_render import (
    contains_arabic,
    draw_text_block,
    fit_text,
    font_for,
    to_visual,
)

ARABIC = "ثبات يتجاوز 16 ساعة بتركيز 30% من دهن العود الكمبودي المعتق"
GERMAN_WORD = "Rindfleischetikettierungsüberwachungsaufgabenübertragungsgesetz"


def _presentation_forms(text: str) -> int:
    """Count Arabic presentation-form glyphs (what joined, shaped letters look like)."""
    return sum(0xFB50 <= ord(c) <= 0xFDFF or 0xFE70 <= ord(c) <= 0xFEFF for c in text)


def test_contains_arabic():
    assert contains_arabic("عطر Sultan") and not contains_arabic("Sultan Oud 100ml")


def test_arabic_is_shaped_and_reordered_latin_is_untouched():
    visual = to_visual("مرحبا بك")
    assert _presentation_forms(visual) > 0          # letters are joined
    assert visual != "مرحبا بك"
    assert to_visual("Hello world") == "Hello world"


def test_fonts_exist_for_every_language():
    for language in Language:
        assert font_for(language, "bold", 40).getlength("Abc") > 0
    assert font_for(Language.AR, "bold", 40).getlength("عطر") > 0


def test_fit_text_keeps_every_line_inside_the_width():
    block = fit_text("Silence the open office with -42 dB hybrid noise cancellation",
                     Language.EN, "bold", max_width=500, max_lines=3, max_size=80, min_size=30)
    assert 1 < len(block.lines) <= 3
    assert all(w <= 500 for w in block.line_widths)


def test_fit_text_uses_the_largest_size_that_fits():
    short = fit_text("Hi", Language.EN, "bold", max_width=800, max_lines=2, max_size=90, min_size=30)
    assert short.size == 90


def test_a_very_long_german_word_is_broken_instead_of_overflowing():
    block = fit_text(GERMAN_WORD, Language.DE, "bold", max_width=400, max_lines=4,
                     max_size=60, min_size=28)
    assert all(w <= 400 for w in block.line_widths)


def test_text_that_cannot_fit_is_truncated_with_an_ellipsis():
    block = fit_text("word " * 200, Language.EN, "medium", max_width=400, max_lines=2,
                     max_size=40, min_size=30)
    assert len(block.lines) == 2 and block.lines[-1].endswith("…")


def test_arabic_wraps_on_words_in_reading_order():
    block = fit_text(ARABIC, Language.AR, "bold", max_width=420, max_lines=4,
                     max_size=60, min_size=28)
    assert len(block.lines) > 1
    assert " ".join(block.lines) == ARABIC                # logical order is preserved
    assert all(w <= 420 for w in block.line_widths)


def test_right_aligned_block_ends_at_the_right_edge():
    img = Image.new("RGB", (800, 400), "white")
    block = fit_text(ARABIC, Language.AR, "bold", max_width=600, max_lines=3,
                     max_size=50, min_size=28)
    box = draw_text_block(ImageDraw.Draw(img), block, x=100, y=50, width=600,
                          align="right", fill="#000000")
    assert abs(box[2] - 700) <= 2 and box[0] >= 100


def test_left_aligned_block_starts_at_the_left_edge():
    img = Image.new("RGB", (800, 400), "white")
    block = fit_text("Hello there", Language.EN, "bold", max_width=600, max_lines=2,
                     max_size=50, min_size=28)
    box = draw_text_block(ImageDraw.Draw(img), block, x=100, y=50, width=600,
                          align="left", fill="#000000")
    assert box[0] == 100 and box[2] <= 700
