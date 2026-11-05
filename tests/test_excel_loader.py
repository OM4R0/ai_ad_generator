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


# --------------------------------------------------------------------------- #
# Regression tests for the review fixes
# --------------------------------------------------------------------------- #


def _write_csv(frame: pd.DataFrame, tmp_path: Path) -> Path:
    path = tmp_path / "catalog.csv"
    frame.to_csv(path, index=False)
    return path


def _catalog_frame() -> pd.DataFrame:
    return pd.read_csv(CATALOG_CSV, dtype=str, keep_default_na=False)


def test_duplicate_id_reports_the_real_row_number(tmp_path: Path):
    frame = _catalog_frame()
    frame.loc[4, "#"] = "1"  # row 6 in Excel duplicates product 1 (row 2)
    result = load_catalog(_write_csv(frame, tmp_path))
    assert len(result.products) == 29
    assert [(e.row_number, e.message) for e in result.errors] == [
        (6, "duplicate product id 1 (first seen in row 2)"),
    ]


@pytest.mark.parametrize("bad_id", ["inf", "nan", "1.5", "-3"])
def test_odd_ids_are_row_errors_not_crashes(tmp_path: Path, bad_id: str):
    frame = _catalog_frame()
    frame.loc[0, "#"] = bad_id
    result = load_catalog(_write_csv(frame, tmp_path))
    assert len(result.products) == 29
    assert result.errors[0].row_number == 2


def test_blank_rows_are_ignored_and_row_numbers_stay_correct(tmp_path: Path):
    frame = _catalog_frame()
    blank = pd.DataFrame([[""] * len(frame.columns)], columns=frame.columns)
    frame = pd.concat([frame.iloc[:3], blank, frame.iloc[3:], blank], ignore_index=True)
    frame.loc[5, "Suggested Language"] = "Klingon"  # Excel row 7
    result = load_catalog(_write_csv(frame, tmp_path))
    assert len(result.products) == 29          # blank rows are not counted as errors
    assert [e.row_number for e in result.errors] == [7]


def test_xlsx_hyperlinks_stay_aligned_after_a_blank_row(tmp_path: Path):
    from openpyxl import Workbook

    frame = _catalog_frame().iloc[:3]
    wb = Workbook()
    ws = wb.active
    ws.append(list(frame.columns))
    url_col = list(frame.columns).index("Image URL") + 1
    rows = [list(frame.iloc[0]), [None] * len(frame.columns), list(frame.iloc[1])]
    for values in rows:
        ws.append(values)
    for excel_row, product_row in ((2, 0), (4, 1)):
        cell = ws.cell(row=excel_row, column=url_col)
        cell.value = "View Photo"
        cell.hyperlink = frame.iloc[product_row]["Image URL"]
    path = tmp_path / "catalog.xlsx"
    wb.save(path)

    result = load_catalog(path)
    assert [p.id for p in result.products] == [1, 2]
    assert [p.image_url for p in result.products] == list(frame["Image URL"].iloc[:2])


def test_corrupted_xlsx_falls_back_to_csv(tmp_path: Path):
    (tmp_path / "catalog.xlsx").write_bytes(b"this is not a zip file")
    (tmp_path / "catalog.csv").write_bytes(CATALOG_CSV.read_bytes())
    result = load_catalog(tmp_path / "catalog.xlsx")
    assert result.source.suffix == ".csv"
    assert len(result.products) == 30


def test_image_paths_resolve_against_project_root_wherever_the_catalog_is(tmp_path: Path):
    path = tmp_path / "somewhere" / "deep" / "catalog.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(CATALOG_CSV.read_bytes())
    product = load_catalog(path).by_id(1)
    assert product.image_path == PROJECT_ROOT / "product_images" / "01_aeroglide_shoes.jpg"
    assert product.image_path.exists()
