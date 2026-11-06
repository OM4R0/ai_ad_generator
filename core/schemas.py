"""Data contracts shared by every pipeline stage (design.md §4).

Modules talk to each other only through these models. Pydantic validates
every hand-off, including the JSON that comes back from the LLM (NFR-4).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Primitive types
# --------------------------------------------------------------------------- #


def _normalise_hex(value: str) -> str:
    """Accept '#abc', 'abc', '#AABBCC' and return '#AABBCC'."""
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in v):
        raise ValueError(f"not a hex colour: {value!r}")
    return f"#{v.upper()}"


HexColor = Annotated[str, AfterValidator(_normalise_hex)]


class Language(str, Enum):
    """Output languages supported by the pipeline (ADR-005)."""

    EN = "en"
    AR = "ar"
    FR = "fr"
    ES = "es"
    DE = "de"

    @property
    def direction(self) -> Literal["ltr", "rtl"]:
        return "rtl" if self is Language.AR else "ltr"

    @property
    def display_name(self) -> str:
        return _LANGUAGE_NAMES[self]

    @classmethod
    def parse(cls, value: str) -> "Language":
        """Parse a catalog value such as 'Arabic' or a code such as 'ar'."""
        key = value.strip().lower()
        for lang, name in _LANGUAGE_NAMES.items():
            if key in (lang.value, name.lower()):
                return lang
        raise ValueError(f"unsupported language: {value!r}")


_LANGUAGE_NAMES: dict[Language, str] = {
    Language.EN: "English",
    Language.AR: "Arabic",
    Language.FR: "French",
    Language.ES: "Spanish",
    Language.DE: "German",
}

Platform = Literal["instagram", "tiktok", "facebook", "linkedin"]
PLATFORMS: tuple[Platform, ...] = ("instagram", "tiktok", "facebook", "linkedin")


class _Model(BaseModel):
    """Base config: strip whitespace and reject unknown fields."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


# --------------------------------------------------------------------------- #
# [1] Catalog
# --------------------------------------------------------------------------- #


class Product(_Model):
    """One row of the product catalog (spec §4.1)."""

    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    brand_name: str = Field(min_length=1)
    category: str
    description: str
    features: list[str] = Field(min_length=1, max_length=5)
    target_audience: str
    price_range: str
    usp: str
    tone: str
    language: Language
    image_url: str | None = None
    image_path: Path | None = None  # resolved against the project root

    @field_validator("features")
    @classmethod
    def _drop_blank_features(cls, v: list[str]) -> list[str]:
        cleaned = [f.strip() for f in v if f and f.strip()]
        if not cleaned:
            raise ValueError("product needs at least one feature")
        return cleaned


# --------------------------------------------------------------------------- #
# [2] Brand
# --------------------------------------------------------------------------- #


class BrandProfile(_Model):
    """Brand identity used for copy and visuals (spec §4.2)."""

    brand_name: str = Field(min_length=1)
    brand_voice: str
    brand_colors: list[HexColor] = Field(default_factory=list, max_length=6)
    target_market: str = ""
    language: Language
    arabic_register: str = "msa_gulf"  # ADR-006
    logo_url: str | None = None
    sources: list[str] = Field(default_factory=list)  # audit: which sources filled it
    warnings: list[str] = Field(default_factory=list)  # sources skipped or adjusted, and why


class BrandPatch(_Model):
    """A partial brand profile from one source (config file, PDF, manual edit).

    Every field is optional; blank values mean "not provided" and never erase
    what an earlier source set (see `brand_manager.apply_patch`).
    """

    brand_name: str | None = None
    brand_voice: str | None = None
    brand_colors: list[HexColor] = Field(default_factory=list, max_length=6)
    target_market: str | None = None
    language: Language | None = None
    arabic_register: str | None = None
    logo_url: str | None = None

    @field_validator("brand_colors", mode="before")
    @classmethod
    def _null_colours_mean_none(cls, v: object) -> object:
        # LLMs and hand-written configs often send `"brand_colors": null`.
        return [] if v is None else v

    @field_validator("language", mode="before")
    @classmethod
    def _parse_language(cls, v: object) -> object:
        # Config files and LLMs write "Arabic" as often as "ar".
        if isinstance(v, str):
            return Language.parse(v) if v.strip() else None
        return v


class BrandPdfDraft(BaseModel):
    """What the LLM returns after reading a brand-guidelines PDF (prompt brand_pdf_v1).

    Deliberately loose (plain strings, unknown keys ignored): each field is then
    validated on its own by `brand_manager.patch_from_pdf`, so one bad value from
    the LLM doesn't throw away the good ones.
    """

    model_config = ConfigDict(extra="ignore")

    brand_name: str | None = None
    brand_voice: str | None = None
    brand_colors: list[str] = Field(default_factory=list)
    target_market: str | None = None
    language: str | None = None


# --------------------------------------------------------------------------- #
# [3] Vision
# --------------------------------------------------------------------------- #


class VisionAnalysis(_Model):
    """What the vision model saw in the product photo (FR-3)."""

    product_type: str
    dominant_colors: list[HexColor] = Field(default_factory=list, max_length=6)
    materials_finish: list[str] = Field(default_factory=list)
    mood: str = ""
    visual_selling_points: list[str] = Field(default_factory=list)
    scene_prompt: str = ""
    cutout_quality_hint: Literal["clean", "busy background", "unknown"] = "unknown"
    # Brand names/logos printed on the product in the photo (to catch photos of
    # another brand's product, e.g. the catalog's oud photo is a Chanel bottle).
    visible_brands: list[str] = Field(default_factory=list)
    # Does the photo show the catalog product at all? (~2/3 of the reference photos don't:
    # e.g. an iced coffee for a kettle, a blender for an air fryer.)
    photo_matches_product: bool = True
    photo_shows: str = ""


# --------------------------------------------------------------------------- #
# [4] Copy
# --------------------------------------------------------------------------- #


class CopyVariant(_Model):
    """One A/B angle of the ad copy (FR-5, FR-6)."""

    angle: Literal["pain_point", "emotional"]
    headline: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    primary_text: str = Field(min_length=1)
    bullets: list[Annotated[str, Field(min_length=1)]] = Field(min_length=3, max_length=3)
    cta: str = Field(min_length=1)


class CarouselCard(_Model):
    title: str = Field(min_length=1)
    body: str = ""


class Caption(_Model):
    text: str = Field(min_length=1)
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("hashtags")
    @classmethod
    def _normalise_hashtags(cls, v: list[str]) -> list[str]:
        tags = []
        for tag in v:
            t = tag.strip().replace(" ", "")
            if t:
                tags.append(t if t.startswith("#") else f"#{t}")
        return tags


class AdCopyDraft(_Model):
    """What the LLM writes. Language and direction are set by our code, not the model."""

    variants: list[CopyVariant] = Field(min_length=2, max_length=2)
    carousel: list[CarouselCard] = Field(min_length=3, max_length=5)
    captions: dict[Platform, Caption]

    @field_validator("captions")
    @classmethod
    def _all_platforms(cls, v: dict[str, Caption]) -> dict[str, Caption]:
        missing = set(PLATFORMS) - set(v)
        if missing:
            raise ValueError(f"missing captions for: {sorted(missing)}")
        return v

    @field_validator("variants")
    @classmethod
    def _distinct_angles(cls, v: list[CopyVariant]) -> list[CopyVariant]:
        if len({variant.angle for variant in v}) != len(v):
            raise ValueError("the two A/B variants must use different angles")
        return v


class AdCopy(AdCopyDraft):
    """Everything the copy engine produces for one product (→ copy.json)."""

    language: Language
    direction: Literal["ltr", "rtl"]

    @model_validator(mode="after")
    def _direction_follows_language(self) -> "AdCopy":
        # Direction is derived from the language, never trusted from the LLM:
        # an Arabic ad marked "ltr" would render backwards.
        self.direction = self.language.direction
        return self


# --------------------------------------------------------------------------- #
# [6] Result / metadata
# --------------------------------------------------------------------------- #


class CallUsage(_Model):
    """One external call, for the cost tracker (FR-14)."""

    step: str
    provider: str
    model: str                       # the model we asked for
    served_model: str = ""           # the model that really answered (a router may pick another)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    actual_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0  # at the reference paid price
    latency_s: float = 0.0
    cached: bool = False


class CampaignResult(_Model):
    """Full record of one generation run (→ metadata.json)."""

    product: Product
    brand: BrandProfile
    vision: VisionAnalysis | None = None
    copy_: AdCopy | None = Field(default=None, alias="copy")
    files: list[Path] = Field(default_factory=list)          # names inside the product folder
    usage: list[CallUsage] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)       # quality was downgraded, and why
    warnings: list[str] = Field(default_factory=list)        # things a human should check
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    image_source: str = ""                                   # upload | url | local | catalog_url
    product_mode: str = ""                                   # cutout | framed
    background_sources: dict[str, str] = Field(default_factory=dict)
    timings_s: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
