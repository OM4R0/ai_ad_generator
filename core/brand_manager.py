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
from pypdf.errors import DependencyError, PyPdfError

from config.settings import PROJECT_ROOT
from core.schemas import BrandPatch, BrandProfile, Product

logger = logging.getLogger(__name__)

DEFAULT_BRANDS_DIR = PROJECT_ROOT / "config" / "brands"

# Neutral fallback: dark primary, warm accent, white text.
DEFAULT_PALETTE: list[str] = ["#111827", "#F59E0B", "#FFFFFF"]

# Used when the catalog's Tone / Style cell is empty.
DEFAULT_VOICE = "Clear, confident, benefit-led"

# Text colours offered by text_color_for().
LIGHT_TEXT = "#FFFFFF"
DARK_TEXT = "#111111"

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
        brand_voice=product.tone or DEFAULT_VOICE,
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
    Returns None when the folder or a matching file doesn't exist. If several
    files match, the first in alphabetical order wins and a warning is logged.

    Raises:
        BrandConfigError: the matching file has invalid fields or values.
    """
    if not brands_dir.is_dir():
        return None
    wanted = _normalise_name(brand_name)
    matches: list[tuple[Path, dict, str]] = []
    for path in sorted(brands_dir.iterdir()):
        if path.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        parsed = _read_config_file(path)
        if parsed and _normalise_name(str(parsed[0].get("brand_name", ""))) == wanted:
            matches.append((path, *parsed))
    if not matches:
        return None

    path, data, text = matches[0]
    if len(matches) > 1:
        others = ", ".join(p.name for p, _, _ in matches[1:])
        logger.warning("Several configs for brand %r; using %s, ignoring %s",
                       brand_name, path.name, others)
    if path.suffix.lower() != ".json" and _UNQUOTED_YAML_HEX.search(text):
        raise BrandConfigError(
            f"{path.name}: quote hex colours in YAML, e.g. - \"#1A1A1A\" "
            "('#' starts a comment, so unquoted colours are lost)"
        )
    try:
        return BrandPatch.model_validate(data)
    except ValidationError as exc:
        raise BrandConfigError(f"invalid brand config {path.name}: {exc}") from exc


# A list item or key whose value starts with an unquoted '#hex' (a YAML comment).
_UNQUOTED_YAML_HEX = re.compile(r"^[ \t]*(?:-|[A-Za-z_]+:)[ \t]*#[0-9a-fA-F]{3,8}\b", re.M)


def _read_config_file(path: Path) -> tuple[dict, str] | None:
    """Parse one config file → (data, raw text); unreadable files are skipped with a warning."""
    try:
        # utf-8-sig also accepts files saved with a BOM (Windows Notepad does this).
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Skipping unreadable brand config %s: %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Skipping brand config %s: top level must be an object", path.name)
        return None
    return data, text


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


# #RGB, #RRGGBB, or #RRGGBBAA (alpha is dropped). Longest alternative first.
_HEX_RE = re.compile(
    r"(?<![0-9A-Za-z])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9A-Za-z])"
)

# The PDF spec allows junk bytes before the header, within the first 1 KB.
_PDF_HEADER_WINDOW = 1024


def extract_pdf_text(pdf: bytes | Path) -> str:
    """Extract the text of every page of a PDF.

    Raises:
        BrandConfigError: the file is missing, is not a PDF, is password-protected,
            or cannot be parsed.
    """
    if isinstance(pdf, Path):
        try:
            data = pdf.read_bytes()
        except FileNotFoundError as exc:
            raise BrandConfigError(f"PDF not found: {pdf}") from exc
        except OSError as exc:
            raise BrandConfigError(f"could not open the PDF: {exc}") from exc
    else:
        data = pdf

    start = data.find(b"%PDF-", 0, _PDF_HEADER_WINDOW)
    if start < 0:
        raise BrandConfigError("the uploaded file is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data[start:]))
        if reader.is_encrypted and not reader.decrypt(""):  # many PDFs use an empty password
            raise BrandConfigError(
                "the PDF is password-protected; upload an unlocked copy"
            )
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except DependencyError as exc:  # e.g. AES encryption without the crypto library
        raise BrandConfigError(f"the PDF is encrypted and can't be read: {exc}") from exc
    except (PyPdfError, ValueError, KeyError) as exc:
        raise BrandConfigError(f"could not read the PDF: {exc}") from exc


def colors_from_text(text: str) -> list[str]:
    """Hex colour codes written in the text, in order of appearance, deduplicated."""
    found: list[str] = []
    for match in _HEX_RE.finditer(text):
        code = match.group(1).upper()
        if len(code) == 8:
            code = code[:6]  # drop the alpha channel
        elif len(code) == 3:
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
      market, language, etc. Its output is checked **field by field**: valid
      fields are kept, invalid ones are dropped with a warning. If it fails
      completely, we keep the text-only result. It never raises.
    """
    text = extract_pdf_text(pdf)
    result = PdfExtraction(patch=BrandPatch(), llm_used=False, text=text)
    if not text:
        result.warnings.append("PDF has no extractable text (scanned image?)")
        return result

    if extractor is not None:
        try:
            raw = extractor(text[:MAX_PDF_CHARS_FOR_LLM])
        except Exception as exc:  # external call boundary: any failure → text-only result
            result.warnings.append(f"LLM brand extraction failed: {exc}")
        else:
            patch, notes = _lenient_patch(raw)
            result.warnings.extend(notes)
            if patch is not None:
                result.patch, result.llm_used = patch, True
        for warning in result.warnings:
            logger.warning(warning)

    hex_colors = colors_from_text(text)
    if hex_colors:
        result.patch = result.patch.model_copy(update={"brand_colors": hex_colors[:6]})
    return result


def _lenient_patch(raw: object) -> tuple[BrandPatch | None, list[str]]:
    """Validate LLM output one field at a time, so one bad value doesn't discard the rest."""
    if not isinstance(raw, dict):
        return None, [f"LLM returned {type(raw).__name__}, expected an object"]

    notes: list[str] = []
    clean: dict[str, object] = {}
    for key, value in raw.items():
        if key not in BrandPatch.model_fields:
            notes.append(f"ignored unknown field {key!r} from the LLM")
            continue
        if key == "brand_colors" and isinstance(value, list):
            value = _valid_colors(value, notes)[:6]
        try:
            BrandPatch.model_validate({key: value})
        except ValidationError:
            notes.append(f"dropped invalid {key} from the LLM: {value!r}")
            continue
        clean[key] = value

    if not clean:
        return None, notes or ["LLM returned no usable brand fields"]
    return BrandPatch.model_validate(clean), notes


def _valid_colors(values: list[object], notes: list[str]) -> list[str]:
    valid: list[str] = []
    for value in values:
        try:
            valid.extend(BrandPatch(brand_colors=[str(value)]).brand_colors)
        except ValidationError:
            notes.append(f"dropped invalid colour {value!r} from the LLM")
    return valid


# --------------------------------------------------------------------------- #
# Colours from the product image
# --------------------------------------------------------------------------- #


def dominant_colors(image: Image.Image, count: int = 3) -> list[str]:
    """Most frequent distinct colours of a product photo, as hex codes.

    - Transparent pixels are ignored (background-removed cut-outs, Phase 5).
    - Near-white pixels are ignored (white studio backgrounds).
    - Similar shades are merged so the palette has genuinely different colours.
    """
    if count < 1:
        return []
    rgba = image.convert("RGBA")
    rgba.thumbnail((128, 128))
    raw = rgba.tobytes()
    opaque = b"".join(raw[i:i + 3] for i in range(0, len(raw), 4) if raw[i + 3] >= 128)
    if not opaque:
        return []
    # MEDIANCUT can't quantize RGBA, so pack the opaque pixels into a 1-row RGB image.
    pixels = Image.frombytes("RGB", (len(opaque) // 3, 1), opaque)
    quantized = pixels.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
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


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.x relative luminance of a '#RRGGBB' colour (0 = black, 1 = white)."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1 to 21; ≥ 4.5 is readable body text)."""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def text_color_for(background: str) -> str:
    """Whichever of light/dark text has the higher contrast on `background`."""
    if contrast_ratio(background, LIGHT_TEXT) >= contrast_ratio(background, DARK_TEXT):
        return LIGHT_TEXT
    return DARK_TEXT


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
    vision_colors: list[str] | None = None,
) -> BrandProfile:
    """Build the final BrandProfile for a product (spec §4.2 resolution order).

    If no source provided colours, they are filled from (design.md §5):
    the vision model's product colours → the photo's pixels → DEFAULT_PALETTE.

    Never raises because of a bad brand source: an invalid config file is
    skipped, and anything skipped or adjusted is recorded in `warnings`.
    """
    warnings: list[str] = []
    brand = brand_from_product(product)

    try:
        config_patch = load_brand_config(brands_dir or DEFAULT_BRANDS_DIR, product.brand_name)
    except BrandConfigError as exc:
        warnings.append(f"config skipped: {exc}")
        config_patch = None

    if pdf_patch is not None:
        pdf_patch = _keep_product_brand_name(pdf_patch, product, warnings)

    for patch, source in ((config_patch, "config"), (pdf_patch, "pdf"), (manual_patch, "manual")):
        if patch is not None:
            brand = apply_patch(brand, patch, source)

    if not brand.brand_colors:
        brand = _fill_colors(brand, image, vision_colors or [])
    for warning in warnings:
        logger.warning("%s: %s", product.brand_name, warning)
    return brand.model_copy(update={"warnings": [*brand.warnings, *warnings]})


def _keep_product_brand_name(
    patch: BrandPatch, product: Product, warnings: list[str]
) -> BrandPatch:
    """A PDF may not rename the brand: a mismatch usually means the wrong PDF.

    Only an explicit manual edit can rename a brand.
    """
    if patch.brand_name and _normalise_name(patch.brand_name) != _normalise_name(product.brand_name):
        warnings.append(
            f"the PDF is for {patch.brand_name!r}, but the product's brand is "
            f"{product.brand_name!r}; kept the product's brand name"
        )
        return patch.model_copy(update={"brand_name": None})
    return patch


def _fill_colors(
    brand: BrandProfile, image: Image.Image | None, vision_colors: list[str]
) -> BrandProfile:
    # The vision model sees the product itself; raw pixels may describe the background.
    for source, colors in (
        ("vision", vision_colors[:2]),
        ("image", dominant_colors(image, count=2) if image is not None else []),
    ):
        if colors:
            palette = [*colors, text_color_for(colors[0])]
            return apply_patch(brand, BrandPatch(brand_colors=palette), source=source)
    return apply_patch(brand, BrandPatch(brand_colors=DEFAULT_PALETTE), source="default")
