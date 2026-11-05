"""Tests for core.excel_loader (FR-1) against the real catalog + edge cases."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from config.settings import PROJECT_ROOT
from core.excel_loader import CatalogError, load_catalog
from core.schemas import Language

CATALOG_XLSX = PROJECT_ROOT / "data" / "products_master.xlsx"
CATALOG_CSV = PROJECT_ROOT / "data" / "products_master.csv"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(CATALOG_XLSX)


def test_loads_all_30_products_without_errors(catalog):
    assert len(catalog.products) == 30
    assert catalog.errors == []
    assert catalog.source == CATALOG_XLSX


def test_language_distribution_matches_spec(catalog):
    counts = Counter(p.language for p in catalog.products)
    assert counts == {
        Language.AR: 11, Language.EN: 10, Language.FR: 4, Language.ES: 3, Language.DE: 2,
    }


def test_every_product_has_five_features_and_an_existing_image(catalog):
    for p in catalog.products:
        assert len(p.features) == 5, p.name
        assert p.image_path is not None and p.image_path.exists(), p.name
        assert p.image_url and p.image_url.startswith("https://")


def test_known_product_fields(catalog):
    oud = catalog.by_id(10)
    assert oud.brand_name == "Al-Andalus Parfums"
    assert oud.language is Language.AR
    assert oud.language.direction == "rtl"
    assert oud.price_range.startswith("$")


def test_select_keeps_requested_order(catalog):
    assert [p.id for p in catalog.select([27, 1, 10])] == [27, 1, 10]
    with pytest.raises(KeyError):
        catalog.by_id(999)


def test_csv_and_xlsx_give_identical_products():
    xlsx = load_catalog(CATALOG_XLSX).products
    csv = load_catalog(CATALOG_CSV).products
    assert [p.model_dump() for p in xlsx] == [p.model_dump() for p in csv]


def test_falls_back_to_csv_when_xlsx_missing(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "products_master.csv").write_bytes(CATALOG_CSV.read_bytes())
    result = load_catalog(data_dir / "products_master.xlsx")
    assert result.source.suffix == ".csv"
    assert len(result.products) == 30


def test_bad_rows_are_skipped_not_fatal(tmp_path: Path):
    frame = pd.read_csv(CATALOG_CSV, dtype=str, keep_default_na=False)
    frame.loc[0, "Product Name"] = ""             # empty required field
    frame.loc[1, "Suggested Language"] = "Klingon"  # unsupported language
    frame.loc[2, "#"] = "abc"                      # invalid id
    path = tmp_path / "data" / "catalog.csv"
    path.parent.mkdir()
    frame.to_csv(path, index=False)

    result = load_catalog(path)
    assert len(result.products) == 27
    assert [e.row_number for e in result.errors] == [2, 3, 4]
    assert "Klingon" in result.errors[1].message


def test_missing_columns_is_a_catalog_error(tmp_path: Path):
    path = tmp_path / "catalog.csv"
    path.write_text("#,Product Name\n1,Thing\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="missing columns"):
        load_catalog(path)


def test_missing_file_is_a_catalog_error(tmp_path: Path):
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope.xlsx")
