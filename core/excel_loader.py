"""[1] Catalog loader: products_master.xlsx (or .csv) → list[Product]  (FR-1).

Behaviour:
- Reads the .xlsx first; falls back to the .csv with the same stem if the
  .xlsx is missing or unreadable.
- Validates every row into a `Product`. A bad row is reported in
  `LoadResult.errors` and skipped; it never stops the rest of the batch.
- Resolves `Local Image Path` against the project root.
"""

from __future__ import annotations

import logging
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import ValidationError

from config.settings import PROJECT_ROOT
from core.schemas import Language, Product

logger = logging.getLogger(__name__)

# Catalog column → Product field. Keeping the mapping in one place means a
# renamed column is a one-line fix.
COLUMN_MAP: dict[str, str] = {
    "#": "id",
    "Product Name": "name",
    "Brand Name": "brand_name",
    "Category / Niche": "category",
    "Short Description": "description",
    "Target Audience": "target_audience",
    "Price Range (USD)": "price_range",
    "USP": "usp",
    "Tone / Style": "tone",
    "Suggested Language": "language",
    "Image URL": "image_url",
    "Local Image Path": "image_path",
}
FEATURE_COLUMNS: tuple[str, ...] = tuple(f"Feature {i}" for i in range(1, 6))
URL_COLUMN = "Image URL"
REQUIRED_COLUMNS: frozenset[str] = frozenset(COLUMN_MAP) | frozenset(FEATURE_COLUMNS)

# Errors that mean "this file can't be parsed": only these trigger the CSV
# fallback. Anything else is a bug in our code and must surface, not be hidden.
READ_ERRORS: tuple[type[Exception], ...] = (
    OSError,                 # file locked / permission denied
    ValueError,              # includes pandas ParserError, EmptyDataError, UnicodeDecodeError
    zipfile.BadZipFile,      # corrupted .xlsx (an .xlsx is a zip archive)
    InvalidFileException,    # openpyxl: not a valid workbook
)


class CatalogError(Exception):
    """The catalog as a whole cannot be read (as opposed to one bad row)."""


@dataclass
class RowError:
    row_number: int  # 1-based, as a user sees it in Excel (header = row 1)
    message: str


@dataclass
class LoadResult:
    products: list[Product] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    source: Path | None = None

    def by_id(self, product_id: int) -> Product:
        for p in self.products:
            if p.id == product_id:
                return p
        raise KeyError(f"no product with id {product_id}")

    def select(self, ids: Iterable[int]) -> list[Product]:
        """Return the products with the given ids, in the given order."""
        return [self.by_id(i) for i in ids]


def load_catalog(path: Path, project_root: Path | None = None) -> LoadResult:
    """Load and validate the product catalog.

    Args:
        path: the .xlsx (preferred) or .csv file.
        project_root: base folder that `Local Image Path` values are relative
            to. Defaults to the project root from `config.settings`, so the
            catalog file can live anywhere.

    Raises:
        CatalogError: when neither the file nor its fallback can be read, or
            required columns are missing.
    """
    root = project_root or PROJECT_ROOT
    frame, source = _read_with_fallback(path)

    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise CatalogError(f"{source.name} is missing columns: {sorted(missing)}")

    result = LoadResult(source=source)
    seen_ids: dict[int, int] = {}  # product id → row number where first seen
    # The frame index is the original 0-based data row, even after blank rows
    # were dropped, so +2 gives the row number the user sees in Excel.
    for index, raw in zip(frame.index, frame.to_dict(orient="records")):
        row_number = int(index) + 2  # header is row 1
        try:
            product = _row_to_product(raw, root)
        except (ValidationError, ValueError) as exc:
            _skip(result, row_number, _short_error(exc))
            continue
        if product.id in seen_ids:
            _skip(result, row_number,
                  f"duplicate product id {product.id} (first seen in row {seen_ids[product.id]})")
            continue
        seen_ids[product.id] = row_number
        result.products.append(product)

    logger.info(
        "Loaded %d products from %s (%d rows skipped)",
        len(result.products), source.name, len(result.errors),
    )
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _read_with_fallback(path: Path) -> tuple[pd.DataFrame, Path]:
    candidates = [path]
    if path.suffix.lower() in (".xlsx", ".xls"):
        candidates.append(path.with_suffix(".csv"))

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            frame = _read_table(candidate)
        except READ_ERRORS as exc:  # unreadable file: try the next candidate
            last_error = exc
            logger.warning("Could not read %s: %s", candidate.name, exc)
            continue
        if candidate != path:
            logger.warning("Using fallback catalog %s", candidate.name)
        return frame, candidate

    if last_error:
        raise CatalogError(f"could not read catalog {path}: {last_error}") from last_error
    raise CatalogError(f"catalog not found: {path} (or a .csv next to it)")


def _read_table(path: Path) -> pd.DataFrame:
    # dtype=str keeps values exactly as written (e.g. "$180 – $220"); we convert
    # only the fields that need it, in one place.
    is_csv = path.suffix.lower() == ".csv"
    if is_csv:
        frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    else:
        frame = pd.read_excel(path, dtype=str, keep_default_na=False)
    frame.columns = [str(c).strip() for c in frame.columns]
    if not is_csv:
        _restore_hyperlinks(path, frame, URL_COLUMN)
    # With keep_default_na=False empty cells are "" (not NaN), so dropna() would
    # never remove anything. Drop rows where every cell is blank instead.
    blank = frame.apply(lambda col: col.astype(str).str.strip() == "").all(axis=1)
    return frame[~blank]


def _restore_hyperlinks(path: Path, frame: pd.DataFrame, column: str) -> None:
    """Replace a column's display text with the cells' hyperlink targets.

    In the provided .xlsx the `Image URL` cells display "View Photo 🔗" and
    the real URL is stored as the cell's hyperlink, which pandas does not read.
    """
    if column not in frame.columns:
        return
    from openpyxl import load_workbook  # local import: only needed for .xlsx

    workbook = load_workbook(path, read_only=False, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        header = [str(c.value).strip() if c.value is not None else "" for c in sheet[1]]
        col_idx = header.index(column) + 1
        targets = [
            sheet.cell(row=r, column=col_idx).hyperlink
            for r in range(2, len(frame) + 2)
        ]
    finally:
        workbook.close()
    for i, link in enumerate(targets):
        if link is not None and link.target:
            frame.iat[i, frame.columns.get_loc(column)] = link.target


def _row_to_product(raw: dict[str, Any], root: Path) -> Product:
    def cell(column: str) -> str:
        value = raw.get(column)
        return "" if value is None else str(value).strip()

    data: dict[str, Any] = {field_: cell(col) for col, field_ in COLUMN_MAP.items()}
    data["id"] = _parse_id(data["id"])
    data["language"] = Language.parse(data["language"])
    data["features"] = [cell(col) for col in FEATURE_COLUMNS]
    data["image_url"] = _clean_url(data["image_url"], row_name=data["name"])
    data["image_path"] = (root / data["image_path"]) if data["image_path"] else None
    return Product.model_validate(data)


def _clean_url(value: str, row_name: str) -> str | None:
    """Keep only real http(s) URLs; anything else becomes None (local image still works)."""
    if value.startswith(("http://", "https://")):
        return value
    if value:
        logger.warning("Ignoring non-URL image link for %r: %r", row_name, value)
    return None


def _parse_id(value: str) -> int:
    """Parse '1' or Excel's '1.0'; reject '1.5', 'abc', 'inf', 'nan'."""
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid product id {value!r}") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"invalid product id {value!r}")
    return int(number)


def _skip(result: LoadResult, row_number: int, message: str) -> None:
    result.errors.append(RowError(row_number, message))
    logger.warning("Skipping catalog row %d: %s", row_number, message)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'row'}: {err['msg']}"
            for err in exc.errors()
        )
    return str(exc)
