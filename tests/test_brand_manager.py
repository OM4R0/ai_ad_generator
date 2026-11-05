"""Tests for core.brand_manager (FR-4, spec §4.2). Written before the implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from config.settings import PROJECT_ROOT
from core.brand_manager import (
    DEFAULT_PALETTE,
    BrandConfigError,
    BrandPatch,
    apply_patch,
    brand_from_product,
    colors_from_text,
    dominant_colors,
    extract_pdf_text,
    load_brand_config,
    patch_from_pdf,
    resolve_brand,
)
from core.excel_loader import load_catalog
from core.schemas import Language
from scripts.make_sample_brand_pdf import SAMPLE_LINES, build_pdf

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
OUD = CATALOG.by_id(10)        # Al-Andalus Parfums, Arabic
HEADPHONES = CATALOG.by_id(13)  # AeroSonic Audio, English


# --------------------------------------------------------------------------- #
# Source 1: catalog row
# --------------------------------------------------------------------------- #


def test_brand_from_product_uses_catalog_fields():
    brand = brand_from_product(OUD)
    assert brand.brand_name == "Al-Andalus Parfums"
    assert brand.brand_voice == OUD.tone
    assert brand.target_market == OUD.target_audience
    assert brand.language is Language.AR
    assert brand.brand_colors == []          # the catalog has no colours
    assert brand.sources == ["catalog"]


# --------------------------------------------------------------------------- #
# Merge rule: later sources override earlier ones; blanks never erase
# --------------------------------------------------------------------------- #


def test_patch_overrides_only_the_fields_it_sets():
    base = brand_from_product(OUD)
    merged = apply_patch(base, BrandPatch(brand_voice="Royal, poetic"), source="config")
    assert merged.brand_voice == "Royal, poetic"
    assert merged.target_market == base.target_market
    assert merged.sources == ["catalog", "config"]


def test_blank_values_in_a_patch_do_not_erase_existing_values():
    base = brand_from_product(OUD)
    merged = apply_patch(base, BrandPatch(brand_voice="  ", brand_colors=[]), source="manual")
    assert merged.brand_voice == base.brand_voice
    assert merged.sources == ["catalog"]      # nothing applied → source not recorded


def test_patch_rejects_unknown_fields_and_bad_colours():
    with pytest.raises(ValueError):
        BrandPatch.model_validate({"brand_voise": "typo"})
    with pytest.raises(ValueError):
        BrandPatch.model_validate({"brand_colors": ["gold"]})


# --------------------------------------------------------------------------- #
# Source 2: structured config file (JSON / YAML), matched by brand_name
# --------------------------------------------------------------------------- #


def test_load_json_config_matched_by_brand_name_case_insensitive(tmp_path: Path):
    (tmp_path / "whatever-filename.json").write_text(
        json.dumps({"brand_name": "al-andalus PARFUMS", "brand_colors": ["#1A1A1A", "#D4AF37"]}),
        encoding="utf-8",
    )
    patch = load_brand_config(tmp_path, "Al-Andalus Parfums")
    assert patch is not None and patch.brand_colors == ["#1A1A1A", "#D4AF37"]


def test_load_yaml_config(tmp_path: Path):
    (tmp_path / "aerosonic.yaml").write_text(
        "brand_name: AeroSonic Audio\nbrand_voice: Calm, technical\n", encoding="utf-8"
    )
    patch = load_brand_config(tmp_path, "AeroSonic Audio")
    assert patch is not None and patch.brand_voice == "Calm, technical"


def test_no_matching_config_returns_none(tmp_path: Path):
    assert load_brand_config(tmp_path, "Unknown Brand") is None
    assert load_brand_config(tmp_path / "missing-dir", "Unknown Brand") is None


def test_invalid_config_raises_a_clear_error(tmp_path: Path):
    (tmp_path / "bad.json").write_text(
        json.dumps({"brand_name": "X", "brand_colours": ["#000000"]}), encoding="utf-8"
    )
    with pytest.raises(BrandConfigError, match="bad.json"):
        load_brand_config(tmp_path, "X")


def test_repo_ships_a_config_for_the_brief_example_brand():
    patch = load_brand_config(PROJECT_ROOT / "config" / "brands", "Al-Andalus Parfums")
    assert patch is not None
    assert patch.brand_colors == ["#1A1A1A", "#D4AF37", "#FFFFFF"]


# --------------------------------------------------------------------------- #
# Source 3: brand-guidelines PDF
# --------------------------------------------------------------------------- #


def test_extract_pdf_text_reads_the_pages():
    text = extract_pdf_text(build_pdf(SAMPLE_LINES))
    assert "AeroSonic Audio" in text and "#00C2D1" in text


def test_extract_pdf_text_rejects_non_pdf_bytes():
    with pytest.raises(BrandConfigError, match="PDF"):
        extract_pdf_text(b"definitely not a pdf")


def test_colors_from_text_finds_hex_codes_in_order_without_duplicates():
    text = "Primary #0b1f3a, accent #00C2D1; again #0B1F3A and short #fff"
    assert colors_from_text(text) == ["#0B1F3A", "#00C2D1", "#FFFFFF"]


def test_pdf_without_llm_still_yields_the_colours():
    result = patch_from_pdf(build_pdf(SAMPLE_LINES))
    assert result.patch.brand_colors == ["#0B1F3A", "#00C2D1", "#F4F6F8"]
    assert result.llm_used is False


def test_pdf_with_llm_merges_its_fields_but_explicit_hex_codes_win():
    seen = {}

    def fake_llm(text: str) -> dict:
        seen["text"] = text
        return {"brand_voice": "Confident, precise", "brand_colors": ["#123456"], "language": "en"}

    result = patch_from_pdf(build_pdf(SAMPLE_LINES), extractor=fake_llm)
    assert "Midnight Navy" in seen["text"]                    # the LLM got the PDF text
    assert result.patch.brand_voice == "Confident, precise"
    assert result.patch.language is Language.EN
    assert result.patch.brand_colors == ["#0B1F3A", "#00C2D1", "#F4F6F8"]  # PDF codes win
    assert result.llm_used is True


def test_pdf_llm_failure_falls_back_to_text_only():
    def broken_llm(text: str) -> dict:
        raise RuntimeError("quota exceeded")

    result = patch_from_pdf(build_pdf(SAMPLE_LINES), extractor=broken_llm)
    assert result.patch.brand_colors                     # colours still extracted
    assert result.llm_used is False
    assert any("quota exceeded" in w for w in result.warnings)


def test_pdf_llm_returning_garbage_falls_back_to_text_only():
    result = patch_from_pdf(build_pdf(SAMPLE_LINES), extractor=lambda _: {"nonsense": 1})
    assert result.llm_used is False and result.warnings


# --------------------------------------------------------------------------- #
# Missing colours: derived from the product image, else a neutral default
# --------------------------------------------------------------------------- #


def test_dominant_colors_ignore_the_white_studio_background():
    img = Image.new("RGB", (100, 100), "#FFFFFF")
    img.paste(Image.new("RGB", (60, 60), "#B22222"), (20, 20))   # big red block
    img.paste(Image.new("RGB", (20, 20), "#1E3A8A"), (0, 0))     # small blue block
    colors = dominant_colors(img, count=2)
    assert len(colors) == 2
    assert _close(colors[0], "#B22222") and _close(colors[1], "#1E3A8A")


def test_dominant_colors_on_a_real_product_photo():
    colors = dominant_colors(Image.open(OUD.image_path), count=3)
    assert 1 <= len(colors) <= 3
    assert all(c.startswith("#") and len(c) == 7 for c in colors)


# --------------------------------------------------------------------------- #
# Full resolution order (spec §4.2): catalog → config → PDF → manual → colours
# --------------------------------------------------------------------------- #


def test_resolve_brand_applies_sources_in_order(tmp_path: Path):
    (tmp_path / "oud.json").write_text(
        json.dumps({"brand_name": "Al-Andalus Parfums", "brand_voice": "from config",
                    "brand_colors": ["#1A1A1A"]}),
        encoding="utf-8",
    )
    brand = resolve_brand(
        OUD,
        brands_dir=tmp_path,
        pdf_patch=BrandPatch(brand_voice="from pdf", target_market="GCC luxury"),
        manual_patch=BrandPatch(brand_voice="from manual edit"),
    )
    assert brand.brand_voice == "from manual edit"     # manual wins
    assert brand.target_market == "GCC luxury"         # pdf beats catalog
    assert brand.brand_colors == ["#1A1A1A"]           # config, untouched by later sources
    assert brand.sources == ["catalog", "config", "pdf", "manual"]


def test_resolve_brand_fills_missing_colours_from_the_image(tmp_path: Path):
    brand = resolve_brand(HEADPHONES, brands_dir=tmp_path, image=Image.open(HEADPHONES.image_path))
    assert len(brand.brand_colors) >= 2
    assert brand.sources[-1] == "image"


def test_resolve_brand_never_fails_without_colours_or_image(tmp_path: Path):
    brand = resolve_brand(HEADPHONES, brands_dir=tmp_path)
    assert brand.brand_colors == DEFAULT_PALETTE
    assert brand.sources[-1] == "default"


def _close(actual: str, expected: str, tolerance: int = 40) -> bool:
    a = [int(actual[i:i + 2], 16) for i in (1, 3, 5)]
    e = [int(expected[i:i + 2], 16) for i in (1, 3, 5)]
    return all(abs(x - y) <= tolerance for x, y in zip(a, e))
