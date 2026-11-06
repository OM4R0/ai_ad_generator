"""Tests for core.mockup (FR-15, the brief's bonus "Social Mockup Preview")."""

from __future__ import annotations

import pytest
from PIL import Image

from core.brand_manager import resolve_brand
from core.copy_engine import template_copy
from core.mockup import MOCKUP_WIDTH, instagram_handle, instagram_mockup
from core.schemas import Caption
from core.visual_engine import FEED_SIZE
from tests.test_visual_engine import HEADPHONES, OUD, _close, _hex   # EN, AR (config colours)


def _feed(color=(10, 200, 30)) -> Image.Image:
    return Image.new("RGB", FEED_SIZE, color)


def _ad(product, caption: str | None = None, hashtags: list[str] | None = None):
    """(brand, copy) for a product, with the Instagram caption replaced if asked.
    The Caption is built (not model_copy'd) so it is validated like a real one."""
    brand = resolve_brand(product)
    copy = template_copy(product, brand)
    old = copy.captions["instagram"]
    instagram = Caption(text=old.text if caption is None else caption,
                        hashtags=old.hashtags if hashtags is None else hashtags)
    return brand, copy.model_copy(update={"captions": {**copy.captions, "instagram": instagram}})


# --------------------------------------------------------------------------- #
# The post
# --------------------------------------------------------------------------- #

def test_mk1_the_feed_ad_sits_inside_the_post_unchanged():
    feed = _feed()
    image, layout = instagram_mockup(feed, *_ad(HEADPHONES))
    assert image.mode == "RGB" and image.width == MOCKUP_WIDTH
    x0, y0, x1, y1 = layout.media_box
    assert (x1 - x0, y1 - y0) == FEED_SIZE
    assert image.crop(layout.media_box).tobytes() == feed.tobytes()   # 1:1, not resized
    assert 0 < y0 and y1 < image.height           # the header above, the caption below


def test_mk2_the_avatar_and_the_shop_bar_use_the_brand_colour():
    brand, copy = _ad(OUD)                             # config colours: #1A1A1A, #D4AF37
    image, layout = instagram_mockup(_feed(), brand, copy)
    primary = _hex(brand.brand_colors[0])
    ax0, ay0, ax1, ay1 = layout.avatar_box
    inside_above_the_initials = ((ax0 + ax1) // 2, ay0 + (ay1 - ay0) // 4)
    assert _close(image.getpixel(inside_above_the_initials), primary)
    bx0, by0, bx1, _ = layout.cta_bar_box
    assert by0 == layout.media_box[3]                  # right under the ad, like Instagram
    assert _close(image.getpixel(((bx0 + bx1) // 2, by0 + 4)), primary)


def test_mk2_with_a_logo_the_avatar_shows_the_logo():
    logo = Image.new("RGBA", (300, 300), (20, 60, 230, 255))
    image, layout = instagram_mockup(_feed(), *_ad(HEADPHONES), logo=logo)
    ax0, ay0, ax1, ay1 = layout.avatar_box
    assert _close(image.getpixel(((ax0 + ax1) // 2, (ay0 + ay1) // 2)), (20, 60, 230))


def test_mk3_english_reads_left_to_right_and_arabic_is_mirrored():
    _, en = instagram_mockup(_feed(), *_ad(HEADPHONES))
    _, ar = instagram_mockup(_feed(), *_ad(OUD))
    assert en.avatar_box[2] < MOCKUP_WIDTH / 2 < ar.avatar_box[0]
    assert en.caption_box[0] <= 40                       # starts at the left edge
    assert ar.caption_box[2] >= MOCKUP_WIDTH - 40        # starts at the right edge


def test_mk4_the_interface_words_follow_the_ad_language():
    _, en = instagram_mockup(_feed(), *_ad(HEADPHONES))
    _, ar = instagram_mockup(_feed(), *_ad(OUD))
    assert (en.words["sponsored"], en.words["cta"], en.words["more"]) == ("Sponsored", "Shop now", "more")
    assert ar.words["more"] == "المزيد" and ar.words["cta"] == "تسوق الآن"


# --------------------------------------------------------------------------- #
# The caption: cut where Instagram cuts it (spec §8: the hook must come first)
# --------------------------------------------------------------------------- #

def test_mk5_a_long_caption_is_cut_after_two_lines_with_more():
    long = "Forty hours of quiet on one charge, with a fold-flat hinge. " * 6
    _, layout = instagram_mockup(_feed(), *_ad(HEADPHONES, caption=long))
    assert len(layout.caption_lines) == 2 and layout.truncated
    assert layout.caption_lines[-1].endswith("…")


def test_mk5_a_short_caption_is_shown_whole():
    _, layout = instagram_mockup(_feed(), *_ad(HEADPHONES, caption="Quiet, finally.", hashtags=[]))
    assert layout.caption_lines == ["Quiet, finally."] and not layout.truncated


def test_mk5_the_hashtags_come_after_the_caption_text():
    _, layout = instagram_mockup(_feed(), *_ad(HEADPHONES, caption="Quiet, finally.",
                                               hashtags=["#ANC", "headphones"]))
    assert layout.caption_lines == ["Quiet, finally.", "#ANC #headphones"]


def test_mk6_emoji_are_left_out_of_the_drawn_caption():
    # The bundled fonts have no emoji: they would be drawn as empty boxes.
    _, layout = instagram_mockup(_feed(), *_ad(HEADPHONES, caption="Silence 🎧✨ on demand",
                                               hashtags=[]))
    assert layout.caption_lines == ["Silence on demand"]


def test_mk6_arabic_text_in_the_post_is_always_laid_out_right_to_left(monkeypatch):
    # Same trap as the creatives: a caption line starting with "NomadPro" would be
    # guessed left-to-right by the bidi library.
    import core.mockup as mockup
    calls = []
    real = mockup.to_visual
    monkeypatch.setattr(mockup, "to_visual",
                        lambda text, rtl=None: calls.append((text, rtl)) or real(text, rtl=rtl))
    instagram_mockup(_feed(), *_ad(OUD, caption="NomadPro حقيبة مقاومة للمطر", hashtags=[]))
    arabic = [rtl for text, rtl in calls if any("؀" <= ch <= "ۿ" for ch in text)]
    assert arabic and all(rtl is True for rtl in arabic)


def test_mk6_an_arabic_caption_stays_inside_the_post():
    long = "صوت نقي وعزل تام للضوضاء طوال اليوم مع بطارية تدوم أربعين ساعة. " * 5
    image, layout = instagram_mockup(_feed(), *_ad(OUD, caption=long))
    assert layout.truncated and len(layout.caption_lines) == 2
    x0, y0, x1, y1 = layout.caption_box
    assert 0 <= x0 and x1 <= image.width and y1 <= image.height


# --------------------------------------------------------------------------- #
# The handle
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, handle", [
    ("AeroSonic Audio", "aerosonic.audio"),
    ("Al-Andalus Parfums", "al.andalus.parfums"),
    ("Café Crème", "cafe.creme"),
    ("عطور الأندلس", "brand"),                     # no Latin letters: a neutral handle
])
def test_mk7_the_handle_is_a_valid_instagram_username(name, handle):
    assert instagram_handle(name) == handle


def test_mk7_a_handle_is_at_most_30_characters_and_never_ends_with_a_dot():
    handle = instagram_handle("Extraordinarily Long Brand Name Holdings International")
    assert len(handle) <= 30 and not handle.endswith(".")
