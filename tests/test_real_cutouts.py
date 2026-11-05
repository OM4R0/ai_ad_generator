"""Regression tests on REAL catalog photos with the REAL background-removal model.

Why: the "cut-out in pieces" rule first ran at 96 px with 4-way links and wrongly
rejected the headphones (two cups joined by a thin band: 3 pieces, largest 34%).
A synthetic shape could not reproduce that, so these tests use the real photos.

Skipped automatically when the rembg model has not been downloaded yet
(it is downloaded on the first real run, ~170 MB).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import PROJECT_ROOT
from core.excel_loader import load_catalog
from core.image_source import load_product_image
from core.visual_engine import REMBG_MODEL, prepare_product, rembg_remover

MODEL_FILE = Path.home() / ".rembg" / "models" / REMBG_MODEL / f"{REMBG_MODEL}.onnx"
pytestmark = pytest.mark.skipif(not MODEL_FILE.exists(),
                                reason="rembg model not downloaded yet")

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")


@pytest.mark.parametrize("product_id, expected", [
    (13, "cutout"),   # headphones: one object with a thin band (was wrongly "pieces")
    (29, "cutout"),   # dumbbells joined by a resistance band
    (10, "cutout"),   # perfume bottle
    (22, "cutout"),   # wallet
    (20, "framed"),   # thin sunglasses: only the two lenses survive the removal
    (9, "framed"),    # flat lay of several skincare products
])
def test_real_photo_cutout_decision(product_id, expected):
    photo, _ = load_product_image(CATALOG.by_id(product_id))
    _, mode, note = prepare_product(photo, rembg_remover)
    assert mode == expected, note
