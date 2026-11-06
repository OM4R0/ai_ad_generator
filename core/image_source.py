"""Product image sourcing (FR-2): upload → custom URL → local file → catalog URL.

Covers the brief's options A (provided reference image) and D (user upload or
pasted link). A URL that fails, or returns something that isn't an image (e.g.
an HTML login page), is skipped and the next source is tried. An invalid
*upload* is reported immediately, because the user chose it explicitly.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from core.schemas import Product

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 30.0
LOGO_TIMEOUT_S = 10.0       # a logo is small: a slow server means use the wordmark


class ImageSourceError(Exception):
    """No usable product image could be obtained."""


def load_product_image(
    product: Product,
    *,
    upload: bytes | None = None,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    skipped: list[str] | None = None,
) -> tuple[Image.Image, str]:
    """Return (fully loaded RGB/RGBA image, source label).

    skipped: if given, the reason each source that didn't work was passed over is
    added to it (e.g. a custom link that returned a web page), so it can be shown.
    """
    if skipped is None:
        skipped = []
    if upload is not None:
        try:
            return _decode(upload), "upload"
        except ImageSourceError as exc:
            raise ImageSourceError(f"the upload is not a valid image: {exc}") from exc

    tried = skipped
    with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, transport=transport,
                      follow_redirects=True) as client:
        if url:
            image = _try_url(client, url, tried)
            if image is not None:
                return image, "url"
        if product.image_path is not None:
            if product.image_path.exists():
                try:
                    return _decode(product.image_path.read_bytes()), "local"
                except ImageSourceError as exc:
                    tried.append(f"local file: {exc}")
            else:
                tried.append(f"local file missing: {product.image_path.name}")
        if product.image_url:
            image = _try_url(client, product.image_url, tried)
            if image is not None:
                return image, "catalog_url"

    raise ImageSourceError(f"no usable image for {product.name!r}: " + "; ".join(tried))


def load_logo(ref: str, *, base_dir: Path,
              transport: httpx.BaseTransport | None = None) -> Image.Image:
    """The brand logo (`logo_url` in a brand config) from a link or a file path.

    A relative path is read from `base_dir` (the project root). Returned as RGBA,
    so a transparent logo stays transparent on the ad.

    Raises:
        ImageSourceError: the logo can't be downloaded, found, or read.
    """
    if ref.startswith(("http://", "https://")):
        tried: list[str] = []
        with httpx.Client(timeout=LOGO_TIMEOUT_S, transport=transport,
                          follow_redirects=True) as client:
            image = _try_url(client, ref, tried)
        if image is None:
            raise ImageSourceError(tried[0] if tried else ref)
        return image.convert("RGBA")
    path = Path(ref) if Path(ref).is_absolute() else base_dir / ref
    try:
        return _decode(path.read_bytes()).convert("RGBA")
    except OSError as exc:
        raise ImageSourceError(f"logo file not found: {path.name}") from exc


def _try_url(client: httpx.Client, url: str, tried: list[str]) -> Image.Image | None:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        tried.append(f"{url}: {exc!r}")
        return None
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200 or content_type.startswith("text/"):
        tried.append(f"{url}: HTTP {response.status_code} {content_type}")
        return None
    try:
        return _decode(response.content)
    except ImageSourceError as exc:
        tried.append(f"{url}: {exc}")
        return None


def _decode(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
            return img.convert("RGBA" if has_alpha else "RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageSourceError(f"not an image ({exc.__class__.__name__})") from exc
