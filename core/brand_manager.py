"""[2] Brand manager: build the BrandProfile for a product (FR-4, spec §4.2).

Resolution order (later overrides earlier; blanks never erase):
    1. catalog row          → brand_from_product()
    2. config file          → load_brand_config()   (config/brands/*.json|yaml)
    3. brand-guidelines PDF → patch_from_pdf()
    4. manual edits         → a BrandPatch from the dashboard
Then, if no colours were provided: dominant colours of the product image,
else DEFAULT_PALETTE. Generation never fails because a brand field is missing.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from config.settings import PROJECT_ROOT
from core.schemas import BrandPatch, BrandProfile, Product

logger = logging.getLogger(__name__)

DEFAULT_BRANDS_DIR = PROJECT_ROOT / "config" / "brands"

# Neutral fallback: dark primary, warm accent, white text.
DEFAULT_PALETTE: list[str] = ["#111827", "#F59E0B", "#FFFFFF"]

# The LLM sees at most this much PDF text (keeps the request small and cheap).
MAX_PDF_CHARS_FOR_LLM = 12_000

# A callable that turns PDF text into a dict of BrandPatch fields (the LLM).
# Injected, so this module has no dependency on the LLM client (Phase 3).
PdfExtractor = Callable[[str], dict]


class BrandConfigError(Exception):
    """A brand source (config file or PDF) exists but cannot be used."""


# --------------------------------------------------------------------------- #
# 1. Catalog
# --------------------------------------------------------------------------- #


def brand_from_product(product: Product) -> BrandProfile:
    """Default brand profile derived from the catalog row."""
    return BrandProfile(
        brand_name=product.brand_name,
        brand_voice=product.tone,
        target_market=product.target_audience,
        language=product.language,
        sources=["catalog"],
    )


# --------------------------------------------------------------------------- #
# Merge rule
# --------------------------------------------------------------------------- #


def apply_patch(base: BrandProfile, patch: BrandPatch, source: str) -> BrandProfile:
    """Return `base` with every non-blank field of `patch` applied on top."""
    data = base.model_dump()
    applied = False
    for name, value in patch.model_dump(exclude_none=True).items():
        if value in ("", []):
            continue  # blank = "not provided", never an erase
        data[name] = value
        applied = True
    if applied:
        data["sources"] = [*base.sources, source]
    return BrandProfile.model_validate(data)


# --------------------------------------------------------------------------- #
# 2. Structured config (JSON / YAML)
# --------------------------------------------------------------------------- #


def load_brand_config(brands_dir: Path, brand_name: str) -> BrandPatch | None:
    """Find the config file whose `brand_name` matches (case-insensitive).

    The file name doesn't matter; the `brand_name` inside the file does.
    Returns None when the folder or a matching file doesn't exist.

    Raises:
        BrandConfigError: the matching file has invalid fields or values.
    """
    if not brands_dir.is_dir():
        return None
    wanted = _normalise_name(brand_name)
    for path in sorted(brands_dir.iterdir()):
        if path.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        data = _read_config_file(path)
        if data is None or _normalise_name(str(data.get("brand_name", ""))) != wanted:
            continue
        try:
            return BrandPatch.model_validate(data)
        except ValidationError as exc:
            raise BrandConfigError(f"invalid brand config {path.name}: {exc}") from exc
    return None


def _read_config_file(path: Path) -> dict | None:
    """Parse one config file; an unreadable file is skipped with a warning."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Skipping unreadable brand config %s: %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Skipping brand config %s: top level must be an object", path.name)
        return None
    return data


def _normalise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


# --------------------------------------------------------------------------- #
# 3. Brand-guidelines PDF
# --------------------------------------------------------------------------- #


@dataclass
class PdfExtraction:
    patch: BrandPatch
    llm_used: bool
    text: str = ""
    warnings: list[str] = field(default_factory=list)


_HEX_RE = re.compile(r"(?<![0-9A-Za-z])#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9A-Za-z])")


def extract_pdf_text(pdf: bytes | Path) -> str:
    """Extract the text of every page of a PDF.

    Raises:
        BrandConfigError: the input is not a readable PDF.
    """
    data = pdf.read_bytes() if isinstance(pdf, Path) else pdf
    if not data.lstrip()[:5] == b"%PDF-":
        raise BrandConfigError("the uploaded file is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except (PyPdfError, ValueError, KeyError) as exc:
        raise BrandConfigError(f"could not read the PDF: {exc}") from exc


def colors_from_text(text: str) -> list[str]:
    """Hex colour codes written in the text, in order of appearance, deduplicated."""
    found: list[str] = []
    for match in _HEX_RE.finditer(text):
        code = match.group(1).upper()
        if len(code) == 3:
            code = "".join(ch * 2 for ch in code)
        code = f"#{code}"
        if code not in found:
            found.append(code)
    return found


def patch_from_pdf(pdf: bytes | Path, extractor: PdfExtractor | None = None) -> PdfExtraction:
    """Turn a brand-guidelines PDF into a BrandPatch.

    - Hex codes written in the PDF are extracted deterministically and **win**
      over the LLM's colours: an explicit "#0B1F3A" is more reliable than a guess.
    - If `extractor` (the LLM) is given, it reads the text and fills voice,
      market, language, etc. If it fails or returns invalid data, we keep the
      text-only result and record a warning instead of failing.
    """
    text = extract_pdf_text(pdf)
    result = PdfExtraction(patch=BrandPatch(), llm_used=False, text=text)
    if not text:
        result.warnings.append("PDF has no extractable text (scanned image?)")
        return result

    if extractor is not None:
        try:
            result.patch = BrandPatch.model_validate(extractor(text[:MAX_PDF_CHARS_FOR_LLM]))
            result.llm_used = True
        except ValidationError as exc:
            result.warnings.append(f"LLM returned invalid brand data: {exc.error_count()} errors")
        except Exception as exc:  # external call boundary: any failure → text-only result
            result.warnings.append(f"LLM brand extraction failed: {exc}")
        for warning in result.warnings:
            logger.warning(warning)

    hex_colors = colors_from_text(text)
    if hex_colors:
        result.patch = result.patch.model_copy(update={"brand_colors": hex_colors[:6]})
    return result


# --------------------------------------------------------------------------- #
# Colours from the product image
# --------------------------------------------------------------------------- #


def dominant_colors(image: Image.Image, count: int = 3) -> list[str]:
    """Most frequent distinct colours of a product photo, as hex codes.

    Near-white pixels are ignored because catalog photos usually sit on a
    white studio background. Similar shades are merged so the palette has
    genuinely different colours.
    """
    small = image.convert("RGB")
    small.thumbnail((128, 128))
    quantized = small.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)  # [(count, index)]

    picked: list[tuple[int, int, int]] = []
    for _, index in counts:
        rgb = tuple(palette[index * 3: index * 3 + 3])
        if len(rgb) < 3 or min(rgb) > 235:  # near-white background
            continue
        if any(_distance(rgb, other) < 60 for other in picked):
            continue
        picked.append(rgb)  # type: ignore[arg-type]
        if len(picked) == count:
            break
    return ["#{:02X}{:02X}{:02X}".format(*rgb) for rgb in picked]


def _distance(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _text_color_for(hex_color: str) -> str:
    """White text on dark colours, near-black text on light ones."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#FFFFFF" if luminance < 150 else "#111111"


# --------------------------------------------------------------------------- #
# Full resolution
# --------------------------------------------------------------------------- #


def resolve_brand(
    product: Product,
    *,
    brands_dir: Path | None = None,
    pdf_patch: BrandPatch | None = None,
    manual_patch: BrandPatch | None = None,
    image: Image.Image | None = None,
) -> BrandProfile:
    """Build the final BrandProfile for a product (spec §4.2 resolution order)."""
    brand = brand_from_product(product)

    config_patch = load_brand_config(brands_dir or DEFAULT_BRANDS_DIR, product.brand_name)
    for patch, source in ((config_patch, "config"), (pdf_patch, "pdf"), (manual_patch, "manual")):
        if patch is not None:
            brand = apply_patch(brand, patch, source)

    if not brand.brand_colors:
        brand = _fill_colors(brand, image)
    return brand


def _fill_colors(brand: BrandProfile, image: Image.Image | None) -> BrandProfile:
    colors = dominant_colors(image, count=2) if image is not None else []
    if colors:
        palette = [*colors, _text_color_for(colors[0])]
        return apply_patch(brand, BrandPatch(brand_colors=palette), source="image")
    return apply_patch(brand, BrandPatch(brand_colors=DEFAULT_PALETTE), source="default")
