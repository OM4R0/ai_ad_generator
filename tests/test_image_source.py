"""Tests for core.image_source (FR-2): upload → custom URL → local file → catalog URL."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from config.settings import PROJECT_ROOT
from core.excel_loader import load_catalog
from core.image_source import ImageSourceError, load_product_image

PRODUCT = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx").by_id(13)


def _png(color="#FF0000", size=(20, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _transport(responses: list[httpx.Response], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return responses.pop(0)
    return httpx.MockTransport(handler)


def test_local_file_is_the_default():
    image, source = load_product_image(PRODUCT)
    assert source == "local" and image.size[0] > 100


def test_upload_wins_over_everything():
    image, source = load_product_image(PRODUCT, upload=_png(size=(7, 5)))
    assert source == "upload" and image.size == (7, 5)


def test_custom_url_wins_over_the_local_file():
    seen: list[str] = []
    transport = _transport([httpx.Response(200, content=_png(size=(9, 9)),
                                           headers={"content-type": "image/png"})], seen)
    image, source = load_product_image(PRODUCT, url="https://img.test/x.png", transport=transport)
    assert source == "url" and image.size == (9, 9) and seen == ["https://img.test/x.png"]


def test_missing_local_file_falls_back_to_the_catalog_url(tmp_path: Path):
    product = PRODUCT.model_copy(update={"image_path": tmp_path / "missing.jpg"})
    seen: list[str] = []
    transport = _transport([httpx.Response(200, content=_png(),
                                           headers={"content-type": "image/png"})], seen)
    _, source = load_product_image(product, transport=transport)
    assert source == "catalog_url" and seen == [PRODUCT.image_url]


def test_nothing_usable_raises_a_clear_error(tmp_path: Path):
    product = PRODUCT.model_copy(update={"image_path": tmp_path / "missing.jpg"})
    transport = _transport([httpx.Response(404)], [])
    with pytest.raises(ImageSourceError, match="no usable image"):
        load_product_image(product, transport=transport)


def test_invalid_upload_bytes_are_a_clear_error():
    with pytest.raises(ImageSourceError, match="upload"):
        load_product_image(PRODUCT, upload=b"not an image")


def test_url_returning_html_is_rejected_and_the_next_source_is_used():
    transport = _transport([httpx.Response(200, text="<html>login</html>",
                                           headers={"content-type": "text/html"})], [])
    _, source = load_product_image(PRODUCT, url="https://img.test/page", transport=transport)
    assert source == "local"


def test_returned_image_is_fully_loaded_and_independent_of_the_file():
    image, _ = load_product_image(PRODUCT)
    assert image.mode in ("RGB", "RGBA")
    image.load()  # would fail if the file handle had been closed under it
