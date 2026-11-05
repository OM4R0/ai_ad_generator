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


# =========================================================================== #
# Edge cases — review 2 (written before the fixes; see docs/edge-cases.md)
# =========================================================================== #

import io as _io
import logging as _logging

import core.brand_manager as bm


def _encrypted_pdf(user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(_io.BytesIO(build_pdf(SAMPLE_LINES))))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")
    buffer = _io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- E1: catalog row with an empty Tone ----------------------------------- #

def test_e1_empty_catalog_tone_gets_a_default_voice():
    brand = brand_from_product(OUD.model_copy(update={"tone": ""}))
    assert brand.brand_voice == bm.DEFAULT_VOICE


# --- E2/E3: LLM returns partly invalid data -------------------------------- #

def test_e2_llm_partial_garbage_keeps_the_valid_fields():
    def llm(_text: str) -> dict:
        return {"brand_voice": "Calm", "brand_colors": ["gold", "#123456"],
                "notes": "extra key", "language": "Klingon"}

    result = patch_from_pdf(build_pdf(["No colour codes in this PDF"]), extractor=llm)
    assert result.patch.brand_voice == "Calm"
    assert result.patch.brand_colors == ["#123456"]   # "gold" dropped
    assert result.patch.language is None              # "Klingon" dropped
    assert result.llm_used is True
    joined = " ".join(result.warnings)
    assert "gold" in joined and "notes" in joined and "language" in joined


def test_e3_null_colours_from_llm_or_config_mean_empty():
    assert BrandPatch.model_validate({"brand_colors": None}).brand_colors == []


@pytest.mark.parametrize("output", ["just text", ["a", "list"], None, 42])
def test_e2_llm_non_dict_output_falls_back_to_text_only(output):
    result = patch_from_pdf(build_pdf(SAMPLE_LINES), extractor=lambda _: output)
    assert result.llm_used is False and result.warnings
    assert result.patch.brand_colors                    # still from the PDF text


# --- E4/E5/E6: config files ------------------------------------------------ #

def test_e4_json_saved_with_a_utf8_bom_still_loads(tmp_path: Path):
    # Windows Notepad saves "UTF-8" files with a BOM; json.loads rejects it.
    (tmp_path / "x.json").write_text(
        json.dumps({"brand_name": "X", "brand_voice": "v"}), encoding="utf-8-sig"
    )
    patch = load_brand_config(tmp_path, "X")
    assert patch is not None and patch.brand_voice == "v"


@pytest.mark.parametrize("yaml_text", [
    "brand_name: X\nbrand_colors:\n  - #1A1A1A\n  - #D4AF37\n",
    "brand_name: X\nbrand_colors: #1A1A1A\n",
])
def test_e5_unquoted_hex_in_yaml_gives_a_clear_hint(tmp_path: Path, yaml_text: str):
    # In YAML, '#' starts a comment, so unquoted colours silently become null.
    (tmp_path / "x.yaml").write_text(yaml_text, encoding="utf-8")
    with pytest.raises(BrandConfigError, match="quote"):
        load_brand_config(tmp_path, "X")


def test_e6_two_configs_for_the_same_brand_warn_and_use_the_first(tmp_path: Path, caplog):
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(
            json.dumps({"brand_name": "X", "brand_voice": f"from {name}"}), encoding="utf-8"
        )
    with caplog.at_level(_logging.WARNING):
        patch = load_brand_config(tmp_path, "X")
    assert patch.brand_voice == "from a.json"
    assert "b.json" in caplog.text


# --- E7/E8/E9: PDF input ---------------------------------------------------- #

def test_e7_pdf_with_bytes_before_the_header_is_read():
    data = b"\xef\xbb\xbfsome junk\n" + build_pdf(SAMPLE_LINES)
    assert "AeroSonic Audio" in extract_pdf_text(data)


def test_e8_password_protected_pdf_gives_a_clear_error():
    with pytest.raises(BrandConfigError, match="password"):
        extract_pdf_text(_encrypted_pdf("secret"))


def test_e8_pdf_encrypted_with_an_empty_password_is_read():
    assert "AeroSonic Audio" in extract_pdf_text(_encrypted_pdf(""))


def test_e9_missing_pdf_file_is_a_brand_error(tmp_path: Path):
    with pytest.raises(BrandConfigError, match="not found"):
        extract_pdf_text(tmp_path / "missing.pdf")


# --- E10: colour code formats ------------------------------------------------ #

def test_e10_eight_digit_hex_drops_the_alpha_channel():
    # Figma and CSS export #RRGGBBAA.
    assert colors_from_text("Primary #0B1F3AFF, accent #00C2D1") == ["#0B1F3A", "#00C2D1"]


# --- E11/E15: resolution safety --------------------------------------------- #

def test_e11_pdf_for_another_brand_cannot_rename_the_product(tmp_path: Path):
    brand = resolve_brand(OUD, brands_dir=tmp_path,
                          pdf_patch=BrandPatch(brand_name="Kingsman & Co.", brand_voice="v"))
    assert brand.brand_name == "Al-Andalus Parfums"
    assert brand.brand_voice == "v"                      # the rest of the PDF still applies
    assert any("Kingsman" in w for w in brand.warnings)


def test_e11_manual_edit_can_rename_the_brand(tmp_path: Path):
    brand = resolve_brand(OUD, brands_dir=tmp_path, manual_patch=BrandPatch(brand_name="New"))
    assert brand.brand_name == "New"


def test_e15_invalid_config_does_not_stop_generation(tmp_path: Path):
    (tmp_path / "bad.json").write_text(
        json.dumps({"brand_name": OUD.brand_name, "brand_colours": ["#000000"]}),
        encoding="utf-8",
    )
    brand = resolve_brand(OUD, brands_dir=tmp_path)
    assert brand.brand_voice == OUD.tone                 # fell back to the catalog
    assert any("bad.json" in w for w in brand.warnings)


# --- E12/E13/E14: image colours ---------------------------------------------- #

def test_e12_transparent_pixels_of_a_cutout_are_ignored():
    # Phase 5 passes background-removed PNGs; transparent pixels are (0,0,0,0).
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (50, 50), (178, 34, 34, 255)), (25, 25))
    colors = dominant_colors(img, count=2)
    assert len(colors) == 1 and _close(colors[0], "#B22222")


def test_e12_fully_transparent_image_gives_no_colours():
    assert dominant_colors(Image.new("RGBA", (10, 10), (0, 0, 0, 0))) == []


@pytest.mark.parametrize("count", [0, -1])
def test_e13_non_positive_count_returns_nothing(count):
    img = Image.new("RGB", (10, 10), "#B22222")
    assert dominant_colors(img, count=count) == []


def test_e13_tiny_and_all_white_images():
    assert len(dominant_colors(Image.new("RGB", (1, 1), "#B22222"))) == 1
    assert dominant_colors(Image.new("RGB", (50, 50), "#FFFFFF")) == []


def test_e13_all_white_image_falls_back_to_the_default_palette(tmp_path: Path):
    brand = resolve_brand(HEADPHONES, brands_dir=tmp_path,
                          image=Image.new("RGB", (50, 50), "#FFFFFF"))
    assert brand.brand_colors == DEFAULT_PALETTE and brand.sources[-1] == "default"


@pytest.mark.parametrize("background, expected_text", [
    ("#808080", "#111111"),   # mid grey: dark text has more contrast (4.8 vs 3.9)
    ("#FBC601", "#111111"),   # yellow
    ("#1A1A1A", "#FFFFFF"),   # near black
    ("#0B1F3A", "#FFFFFF"),   # navy
])
def test_e14_text_colour_is_chosen_by_wcag_contrast(tmp_path: Path, background, expected_text):
    brand = resolve_brand(HEADPHONES, brands_dir=tmp_path,
                          image=Image.new("RGB", (40, 40), background))
    assert brand.brand_colors[-1] == expected_text


# =========================================================================== #
# Consistency review (C1): design.md §5 says colours come from the vision
# analysis BEFORE the raw photo pixels, but resolve_brand had no way to use them.
# =========================================================================== #

def test_c1_vision_colours_come_before_raw_photo_pixels(tmp_path: Path):
    with Image.open(HEADPHONES.image_path) as photo:   # black headphones, yellow background
        brand = resolve_brand(HEADPHONES, brands_dir=tmp_path, image=photo,
                              vision_colors=["#1C1D21", "#C8C9CE"])
    assert brand.brand_colors == ["#1C1D21", "#C8C9CE", "#FFFFFF"]
    assert brand.sources[-1] == "vision"


def test_c1_config_colours_still_beat_vision_colours():
    brand = resolve_brand(OUD, vision_colors=["#123456"])   # real config/brands/ folder
    assert brand.brand_colors == ["#1A1A1A", "#D4AF37", "#FFFFFF"]
    assert "vision" not in brand.sources
