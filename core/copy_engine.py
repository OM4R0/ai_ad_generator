"""[4] Copy engine: product + brand + vision → AdCopy (FR-5/6/7/9, spec §8).

One LLM call returns everything (2 A/B variants, carousel cards, 4 platform
captions) so the copy stays consistent across formats and each product costs
a single copy request (NFR-7).

Quality is enforced in two places:
- the prompt (`config/prompt_templates/copy_v1.txt` + a language guide with
  a banned-phrase list per language), and
- post-checks here: clichés and over-long hooks are flagged as warnings
  (not rejected), and hashtags are trimmed to platform limits.

If the LLM fails, `template_copy` builds plain but valid copy from the
catalog fields, in the right language, so the pipeline never stops.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterator, Protocol

from core.llm_client import LLMError
from core.prompts import PROMPTS_DIR, load_prompt, read_text_file
from core.schemas import (
    PLATFORMS,
    AdCopy,
    AdCopyDraft,
    BrandProfile,
    CallUsage,
    Caption,
    CarouselCard,
    CopyVariant,
    Language,
    Product,
    VisionAnalysis,
)

logger = logging.getLogger(__name__)

# v2 (2026-11-05, after the first live run): prices only from the catalog,
# last carousel card = CTA, no hashtags inside caption text, consistent audience tags.
COPY_PROMPT = "copy_v2"
COPY_TEMPERATURE = 0.8      # creative writing: more variety than the vision step
HOOK_MAX_CHARS = 125        # Instagram truncates the caption after ~125 characters
HASHTAG_LIMITS: dict[str, int] = {"instagram": 12, "tiktok": 5, "facebook": 3, "linkedin": 5}

CTA_BY_LANGUAGE: dict[Language, str] = {
    Language.EN: "Shop now",
    Language.AR: "تسوّق الآن",
    Language.FR: "Acheter maintenant",
    Language.ES: "Comprar ahora",
    Language.DE: "Jetzt kaufen",
}

ARABIC_REGISTERS: dict[str, str] = {
    "msa_gulf": "Modern Standard Arabic with a light Gulf (Khaleeji) flavour",
    "msa": "Modern Standard Arabic (formal)",
    "gulf": "Gulf (Khaleeji) dialect",
    "levantine": "Levantine dialect",
    "egyptian": "Egyptian dialect",
}


class JsonLLM(Protocol):
    def complete_json(self, **kwargs) -> tuple[AdCopyDraft, CallUsage]: ...


@dataclass
class CopyResult:
    copy: AdCopy
    usage: CallUsage | None
    prompt_version: str
    fallback: str | None = None                       # why template copy was used
    warnings: list[str] = field(default_factory=list)  # quality flags (clichés, long hooks)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_copy(
    product: Product,
    brand: BrandProfile,
    vision: VisionAnalysis | None,
    client: JsonLLM,
    model: str,
    variation: int = 0,
) -> CopyResult:
    """Write the ad copy with the LLM; fall back to template copy on failure.

    `variation` > 0 asks for a fresh angle (the dashboard's Regenerate button).
    """
    language = brand.language
    system, user = _build_prompt(product, brand, vision, variation)
    try:
        draft, usage = client.complete_json(
            step="copy", model=model, system=system, user=user,
            schema=AdCopyDraft, temperature=COPY_TEMPERATURE,
            image=None,  # the photo was already analysed in step 3: send its findings, not pixels

        )
    except LLMError as exc:
        logger.warning("Copy fallback for %s: %s", product.name, exc)
        return CopyResult(template_copy(product, brand), None, COPY_PROMPT,
                          fallback=f"copy: template ({exc})")

    copy = _finalise(draft, language)
    warnings = quality_warnings(copy) + price_warnings(copy, product)
    for warning in warnings:
        logger.info("Copy check for %s: %s", product.name, warning)
    return CopyResult(copy, usage, COPY_PROMPT, warnings=warnings)


def template_copy(product: Product, brand: BrandProfile) -> AdCopy:
    """Plain, valid copy built only from catalog fields (no LLM)."""
    language = brand.language
    cta = CTA_BY_LANGUAGE[language]
    # The last-resort fallback must never fail, even on a sparse catalog row.
    usp = product.usp or product.name
    description = product.description or usp
    bullets = (product.features + [usp] * 3)[:3]
    hashtags = [_hashtag(brand.brand_name), _hashtag(product.category)]

    variants = [
        CopyVariant(angle="pain_point", headline=usp, hook=usp[:HOOK_MAX_CHARS],
                    primary_text=description, bullets=bullets, cta=cta),
        CopyVariant(angle="emotional", headline=product.name,
                    hook=description[:HOOK_MAX_CHARS],
                    primary_text=f"{description} {usp}", bullets=bullets, cta=cta),
    ]
    carousel = (
        [CarouselCard(title=usp, body=product.name)]
        + [CarouselCard(title=f, body="") for f in product.features[:3]]
        + [CarouselCard(title=cta, body=brand.brand_name)]
    )
    caption = Caption(text=f"{usp}\n\n{description}", hashtags=hashtags)
    return AdCopy(language=language, direction=language.direction, variants=variants,
                  carousel=carousel, captions={p: caption for p in PLATFORMS})


def find_cliches(text: str, language: Language) -> list[str]:
    """Banned phrases (from the language guide) that appear in `text`."""
    haystack = _normalise(text)
    return [phrase.casefold() for phrase in banned_phrases(language)
            if _normalise(phrase) in haystack]


def quality_warnings(copy: AdCopy) -> list[str]:
    """Clichés and over-long hooks. Flags for a human, not reasons to reject."""
    warnings = []
    for where, text in _texts(copy):
        for phrase in find_cliches(text, copy.language):
            warnings.append(f"cliché {phrase!r} in {where}")
    for variant in copy.variants:
        if len(variant.hook) > HOOK_MAX_CHARS:
            warnings.append(f"{variant.angle} hook is {len(variant.hook)} chars "
                            f"(Instagram shows ~{HOOK_MAX_CHARS})")
    return warnings


# A dollar amount with the currency before ("$149", "USD 149") or after the number
# ("149 $" in French/Spanish/German, "149 USD", "149 دولار" in Arabic).
_PRICE_RE = re.compile(
    r"(?:\$|\bUSD\s?)\s?(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s?(?:\$|USD\b|dollars?\b|دولار)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def price_warnings(copy: AdCopy, product: Product) -> list[str]:
    """Fact check: dollar amounts in the copy that are not in the catalog price range.

    Found live: the model wrote "$149" for a product listed at "$129 – $189".
    (Covers $ / USD / دولار amounts; prices in words or other currencies are not checked.)
    """
    allowed = {_to_number(n) for n in _NUMBER_RE.findall(product.price_range)}
    warnings, seen = [], set()
    for where, text in _texts(copy):
        for before, after in _PRICE_RE.findall(text):
            amount = before or after
            value = _to_number(amount)
            if value not in allowed and value not in seen:
                seen.add(value)
                warnings.append(f"price ${amount} in {where} is not in the catalog range "
                                f"{product.price_range}")
    return warnings


def _to_number(text: str) -> float:
    return float(text.replace(",", ""))


@lru_cache(maxsize=None)
def banned_phrases(language: Language) -> tuple[str, ...]:
    """The '## Banned phrases' list of the language guide."""
    phrases, in_section = [], False
    for line in language_guide(language).splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower() == "## banned phrases"
        elif in_section and line.strip().startswith("- "):
            phrases.append(line.strip()[2:].strip())
    return tuple(phrases)


def language_guide(language: Language) -> str:
    return read_text_file(PROMPTS_DIR / "languages" / f"{language.value}.md")


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _build_prompt(product, brand, vision, variation) -> tuple[str, str]:
    language = brand.language
    register = ""
    if language is Language.AR:
        register = "Arabic register: " + ARABIC_REGISTERS.get(
            brand.arabic_register, ARABIC_REGISTERS["msa_gulf"]) + "."
    variation_note = (
        f"This is variation {variation}: use a clearly different hook, headline and "
        "opening line from the most obvious first idea."
        if variation > 0 else ""
    )
    return load_prompt(
        COPY_PROMPT,
        language_name=language.display_name,
        register_line=register,
        language_guide=language_guide(language),
        hook_max=str(HOOK_MAX_CHARS),
        ig_tags=str(HASHTAG_LIMITS["instagram"]), tt_tags=str(HASHTAG_LIMITS["tiktok"]),
        fb_tags=str(HASHTAG_LIMITS["facebook"]), li_tags=str(HASHTAG_LIMITS["linkedin"]),
        product_name=product.name,
        brand_name=brand.brand_name,
        category=product.category,
        description=product.description,
        features="\n".join(f"- {f}" for f in product.features),
        usp=product.usp,
        price_range=product.price_range,
        target_audience=product.target_audience,
        brand_voice=brand.brand_voice,
        target_market=brand.target_market or product.target_audience,
        vision_block=_vision_block(vision),
        variation_note=variation_note,
    )


def _vision_block(vision: VisionAnalysis | None) -> str:
    if vision is None:
        return "(no photo analysis available)"
    return "\n".join([
        f"- Product type: {vision.product_type}",
        f"- Materials / finish: {', '.join(vision.materials_finish) or 'n/a'}",
        f"- Mood: {vision.mood or 'n/a'}",
        f"- Visible selling points: {'; '.join(vision.visual_selling_points) or 'n/a'}",
    ])


def _finalise(draft: AdCopyDraft, language: Language) -> AdCopy:
    """Attach language/direction (ours, not the LLM's) and trim hashtags per platform."""
    captions = {
        platform: Caption(text=_strip_trailing_hashtags(caption.text),
                          hashtags=_dedupe(caption.hashtags)[:HASHTAG_LIMITS[platform]])
        for platform, caption in draft.captions.items()
    }
    return AdCopy(language=language, direction=language.direction,
                  variants=draft.variants, carousel=draft.carousel, captions=captions)


def _strip_trailing_hashtags(text: str) -> str:
    """Drop trailing lines made only of hashtags (they are rendered from the list)."""
    lines = text.rstrip().splitlines()
    while lines and (not lines[-1].strip()
                     or all(token.startswith("#") for token in lines[-1].split())):
        lines.pop()
    return "\n".join(lines).rstrip() or text.strip()


def _texts(copy: AdCopy) -> Iterator[tuple[str, str]]:
    for v in copy.variants:
        yield f"{v.angle} headline", v.headline
        yield f"{v.angle} hook", v.hook
        yield f"{v.angle} text", " ".join([v.primary_text, *v.bullets, v.cta])
    for i, card in enumerate(copy.carousel, start=1):
        yield f"carousel card {i}", f"{card.title} {card.body}"
    for platform, caption in copy.captions.items():
        yield f"{platform} caption", caption.text


def _normalise(text: str) -> str:
    """Casefold and strip Arabic diacritics, so 'ارتقِ' matches 'ارتق'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _dedupe(tags: list[str]) -> list[str]:
    seen, unique = set(), []
    for tag in tags:
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(tag)
    return unique


def _hashtag(text: str) -> str:
    return "#" + re.sub(r"[^\w]", "", text.replace(" ", ""))
