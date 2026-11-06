"""Tests for core.output_writer (FR-11, spec §5, ADR-004)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from config.settings import PROJECT_ROOT
from core.brand_manager import resolve_brand
from core.copy_engine import template_copy
from core.excel_loader import load_catalog
from core.output_writer import campaign_zip, slugify, usage_totals, write_campaign
from core.scene_generator import SceneGenerator
from core.schemas import AdCopy, CallUsage, CampaignResult
from core.visual_engine import FEED_SIZE, STORY_SIZE, build_creatives
from tests.test_visual_engine import fake_remover

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)   # English
BACKPACK = CATALOG.by_id(23)     # Arabic


def _campaign(product, tmp_path, cards: int = 3, **result_fields):
    brand = resolve_brand(product)
    copy = template_copy(product, brand)
    while len(copy.carousel) < cards:
        copy.carousel.append(copy.carousel[-1])
    copy = copy.model_copy(update={"carousel": copy.carousel[:cards]})
    photo = Image.open(product.image_path).convert("RGB")
    creatives = build_creatives(product, brand, copy, photo,
                                SceneGenerator("none", cache_dir=tmp_path),
                                scene_prompt="studio", remover=fake_remover)
    fields = dict(
        product=product, brand=brand, copy=copy,
        usage=[CallUsage(step="vision", provider="gemini", model="v", prompt_tokens=1000,
                         completion_tokens=200, estimated_cost_usd=0.0012),
               CallUsage(step="copy", provider="openrouter.ai", model="c", prompt_tokens=2000,
                         completion_tokens=800, estimated_cost_usd=0.0028, cached=True)],
        fallbacks=["scene: gradient"], prompt_versions={"vision": "vision_v3", "copy": "copy_v2"},
        image_source="local", warnings=["تحذير: the photo shows another brand"],
        timings_s={"vision": 1.5, "copy": 2.5}, product_mode=creatives.product_mode,
        background_sources=creatives.background_sources,
    )
    fields.update(result_fields)
    return CampaignResult(**fields), creatives


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()}


# --------------------------------------------------------------------------- #
# Folder names
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("Al-Andalus Parfums", "Al-Andalus_Parfums"),
    ("AeroSonic Pro ANC Wireless Headphones", "AeroSonic_Pro_ANC_Wireless_Headphones"),
    ("PranaEarth Organic Cork & Natural Tree Rubber Yoga Mat",
     "PranaEarth_Organic_Cork_Natural_Tree_Rubber_Yoga_Mat"),
    ("Café Crème", "Cafe_Creme"),                 # accents → plain ASCII
    ('A/B: "Test"? <x>|*', "A_B_Test_x"),          # characters Windows forbids
    ("ends with dots...", "ends_with_dots"),       # Windows drops trailing dots
    ("CON", "CON_"), ("nul", "nul_"), ("COM1", "COM1_"),  # reserved device names
])
def test_w1_folder_names_are_safe_on_windows_and_readable(text, expected):
    assert slugify(text, fallback="x") == expected


@pytest.mark.parametrize("text", ["عطر العود الملكي", "   ", "???", ""])
def test_w1_a_name_with_no_latin_letters_uses_the_fallback(text):
    assert slugify(text, fallback="product_10") == "product_10"


def test_w1_long_names_are_cut_without_a_dangling_separator():
    slug = slugify("Word " * 40, fallback="x")
    assert len(slug) <= 60 and not slug.endswith("_")


# --------------------------------------------------------------------------- #
# What gets written
# --------------------------------------------------------------------------- #

def test_w2_writes_exactly_the_structure_the_brief_asks_for(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path, cards=4)
    folder = write_campaign(result, creatives, tmp_path / "output")
    assert folder == tmp_path / "output" / slugify(HEADPHONES.brand_name, "x") / \
        "AeroSonic_Pro_ANC_Wireless_Headphones"
    assert _names(folder) == {
        "copy.json", "metadata.json", "creative_feed.png", "creative_feed_b.png",
        "creative_story.png", "carousel_slide_1.png", "carousel_slide_2.png",
        "carousel_slide_3.png", "carousel_slide_4.png"}


def test_w3_images_are_png_at_the_exact_sizes(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path)
    folder = write_campaign(result, creatives, tmp_path)
    for name, size in [("creative_feed.png", FEED_SIZE), ("creative_feed_b.png", FEED_SIZE),
                       ("creative_story.png", STORY_SIZE), ("carousel_slide_1.png", FEED_SIZE)]:
        with Image.open(folder / name) as image:
            assert (image.format, image.size) == ("PNG", size), name


def test_w4_copy_json_keeps_arabic_readable_and_loads_back(tmp_path):
    result, creatives = _campaign(BACKPACK, tmp_path)
    folder = write_campaign(result, creatives, tmp_path)
    raw = (folder / "copy.json").read_text(encoding="utf-8")
    assert "\\u06" not in raw                       # not escaped as \u06xx
    copy = AdCopy.model_validate_json(raw)
    assert copy == result.copy_ and copy.direction == "rtl"
    assert copy.variants[0].headline in raw          # the real Arabic text is in the file


def test_w5_metadata_records_how_the_ad_was_made(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path)
    folder = write_campaign(result, creatives, tmp_path)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["product"]["id"] == 13
    assert meta["image_source"] == "local"
    assert meta["prompt_versions"] == {"vision": "vision_v3", "copy": "copy_v2"}
    assert [u["step"] for u in meta["usage"]] == ["vision", "copy"]
    assert meta["fallbacks"] == ["scene: gradient"]
    assert meta["warnings"] == ["تحذير: the photo shows another brand"]
    assert meta["created_at"]
    assert "copy.json" in meta["files"] and "metadata.json" not in meta["files"]
    assert meta["totals"]["llm_calls"] == 2
    assert "copy" not in meta                       # the copy lives in copy.json only


def test_w5_totals_split_live_and_cached_calls():
    usage = [CallUsage(step="vision", provider="p", model="m", prompt_tokens=1000,
                       completion_tokens=200, estimated_cost_usd=0.0012),
             CallUsage(step="copy", provider="p", model="m", prompt_tokens=2000,
                       completion_tokens=800, estimated_cost_usd=0.0028, cached=True)]
    totals = usage_totals(usage)
    assert totals == {
        "llm_calls": 2, "cached_calls": 1, "prompt_tokens": 3000, "completion_tokens": 1000,
        "estimated_cost_usd": 0.004, "actual_cost_usd": 0.0,
        "spent_this_run_usd": 0.0012,          # a replayed answer costs nothing now
    }


def test_w5_totals_of_nothing_are_zero():
    assert usage_totals([])["llm_calls"] == 0


# --------------------------------------------------------------------------- #
# Reruns (ADR-004: the CLI overwrites cleanly)
# --------------------------------------------------------------------------- #

def test_w6_a_rerun_with_fewer_slides_removes_the_stale_ones(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path, cards=5)
    folder = write_campaign(result, creatives, tmp_path)
    (folder / "my_notes.txt").write_text("keep me", encoding="utf-8")
    result, creatives = _campaign(HEADPHONES, tmp_path, cards=3)
    assert write_campaign(result, creatives, tmp_path) == folder
    names = _names(folder)
    assert "carousel_slide_3.png" in names
    assert "carousel_slide_4.png" not in names and "carousel_slide_5.png" not in names
    assert "my_notes.txt" in names                    # never deletes what it didn't write


def test_w7_two_products_with_the_same_name_get_separate_folders(tmp_path):
    twin = HEADPHONES.model_copy(update={"id": 99})
    first = write_campaign(*_campaign(HEADPHONES, tmp_path), tmp_path)
    second = write_campaign(*_campaign(twin, tmp_path), tmp_path)
    assert first != second and second.name.endswith("_99")
    assert json.loads((first / "metadata.json").read_text("utf-8"))["product"]["id"] == 13
    assert write_campaign(*_campaign(HEADPHONES, tmp_path), tmp_path) == first


def test_w8_the_output_folder_is_created_when_missing(tmp_path):
    folder = write_campaign(*_campaign(HEADPHONES, tmp_path), tmp_path / "a" / "b")
    assert (folder / "metadata.json").exists()


# --------------------------------------------------------------------------- #
# The dashboard's .zip export (FR-13)
# --------------------------------------------------------------------------- #

def test_w9_the_zip_holds_exactly_the_files_of_the_output_folder(tmp_path):
    result, creatives = _campaign(BACKPACK, tmp_path)
    folder = write_campaign(result, creatives, tmp_path / "output")
    with zipfile.ZipFile(io.BytesIO(campaign_zip(result, creatives))) as archive:
        names = archive.namelist()
        prefix = f"{folder.parent.name}/{folder.name}/"     # same Brand/Product layout
        assert all(name.startswith(prefix) for name in names)
        assert {name[len(prefix):] for name in names} == _names(folder)
        copy = AdCopy.model_validate_json(archive.read(prefix + "copy.json").decode("utf-8"))
        assert copy.direction == "rtl"
        with Image.open(io.BytesIO(archive.read(prefix + "creative_story.png"))) as story:
            assert story.size == STORY_SIZE


def test_w9_the_zip_metadata_lists_the_files_like_the_folder_does(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path)
    folder = write_campaign(result, creatives, tmp_path / "output")
    on_disk = json.loads((folder / "metadata.json").read_text("utf-8"))
    with zipfile.ZipFile(io.BytesIO(campaign_zip(result, creatives))) as archive:
        [meta_name] = [n for n in archive.namelist() if n.endswith("metadata.json")]
        in_zip = json.loads(archive.read(meta_name).decode("utf-8"))
    assert in_zip["files"] == on_disk["files"]
    assert in_zip["totals"] == on_disk["totals"]


def test_w9_making_a_zip_does_not_need_or_touch_the_output_folder(tmp_path):
    result, creatives = _campaign(HEADPHONES, tmp_path)
    data = campaign_zip(result, creatives)
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None   # a valid archive
    assert not (tmp_path / "output").exists()
