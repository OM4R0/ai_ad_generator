"""Dashboard logic (FR-10, FR-13, FR-14, ADR-004).

Everything the dashboard decides lives here, as plain functions, so it can be
tested without a browser. `app.py` only draws widgets and calls these.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, fields
from typing import Iterable

from config.settings import Settings
from core.brand_manager import PdfExtractor
from core.pipeline import JsonLLM, ProductRun, Services, build_services
from core.prompts import load_prompt
from core.schemas import BrandPatch, BrandPdfDraft, BrandProfile, CampaignResult, Language, Product
from core.visual_engine import Remover

BRAND_PDF_PROMPT = "brand_pdf_v1"
BRAND_PDF_TEMPERATURE = 0.1   # extraction, not creativity
MAX_VARIANTS = 5              # how many Regenerate results stay in the session


@dataclass
class Variant:
    """One generation shown in the dashboard (ADR-004: kept in the session)."""

    run: ProductRun
    variation: int            # 0 = first generation; Regenerate asks for 1, 2, …


# --------------------------------------------------------------------------- #
# Text on the page
# --------------------------------------------------------------------------- #


def rtl_block(text: str, direction: str) -> str:
    """HTML for one piece of ad copy, right-to-left for Arabic (FR-10).

    The text comes from an LLM, so it is escaped: it is shown, never run as markup.
    """
    direction = "rtl" if direction == "rtl" else "ltr"
    align = "right" if direction == "rtl" else "left"
    body = html.escape(text).replace("\n", "<br>")
    return f'<div dir="{direction}" style="text-align:{align}">{body}</div>'


def product_label(product: Product) -> str:
    return f"#{product.id} · {product.name} · {product.language.display_name}"


def product_choices(products: Iterable[Product], demo_ids: Iterable[int]) -> list[Product]:
    """The demo products first (ADR-005 order), then every other product by ID."""
    by_id = {p.id: p for p in products}
    demo = [by_id[i] for i in dict.fromkeys(demo_ids) if i in by_id]
    rest = sorted((p for p in by_id.values() if p not in demo), key=lambda p: p.id)
    return demo + rest


# --------------------------------------------------------------------------- #
# Brand editor
# --------------------------------------------------------------------------- #


def brand_patch_from_form(
    base: BrandProfile,
    *,
    voice: str,
    colors: list[str],
    market: str | None,
    language: str | Language,
) -> BrandPatch | None:
    """A manual BrandPatch with ONLY the fields the user changed, or None.

    Blank fields are "not changed" (they never erase a value), and colour pickers
    return lowercase hex, which is not a change either.
    """
    changes: dict[str, object] = {}
    voice = (voice or "").strip()
    if voice and voice != base.brand_voice:
        changes["brand_voice"] = voice
    picked = [c.upper() for c in colors if c]
    if picked and picked != [c.upper() for c in base.brand_colors]:
        changes["brand_colors"] = picked
    market = (market or "").strip()
    if market and market != base.target_market:
        changes["target_market"] = market
    chosen = Language.parse(language) if isinstance(language, str) else language
    if chosen is not None and chosen != base.language:
        changes["language"] = chosen
    return BrandPatch(**changes) if changes else None


def merge_patches(earlier: BrandPatch | None, new: BrandPatch | None) -> BrandPatch | None:
    """The user's edits so far plus a new one: the new edit wins field by field,
    and fields it didn't touch keep their earlier edit (so edits are never lost)."""
    if earlier is None or new is None:
        return new if earlier is None else earlier
    return BrandPatch(**{**earlier.model_dump(exclude_defaults=True),
                         **new.model_dump(exclude_defaults=True)})


def make_pdf_extractor(llm: JsonLLM, model: str) -> PdfExtractor:
    """The LLM step for `brand_manager.patch_from_pdf` (FR-4): PDF text → raw fields.

    It may raise (LLMError): `patch_from_pdf` catches that and keeps the text-only
    result, so a failed call still gives the hex colours written in the PDF.
    """
    def extract(text: str) -> dict:
        system, user = load_prompt(BRAND_PDF_PROMPT, pdf_text=text)
        draft, _usage = llm.complete_json(
            step="brand_pdf", model=model, system=system, user=user,
            schema=BrandPdfDraft, temperature=BRAND_PDF_TEMPERATURE,
        )
        return draft.model_dump(exclude_none=True)

    return extract


# --------------------------------------------------------------------------- #
# Cost tracker (FR-14)
# --------------------------------------------------------------------------- #


def cost_rows(result: CampaignResult) -> list[dict]:
    """One row per AI call: which model was asked, which answered, tokens, cost."""
    return [
        {
            "step": u.step,
            "model": u.model,
            "answered by": u.served_model or u.model,
            "tokens": u.prompt_tokens + u.completion_tokens,
            "from cache": u.cached,
            "est. cost (USD)": u.estimated_cost_usd,
        }
        for u in result.usage
    ]


# --------------------------------------------------------------------------- #
# Regenerate history (ADR-004)
# --------------------------------------------------------------------------- #


def add_variant(history: list[Variant], run: ProductRun, variation: int,
                limit: int = MAX_VARIANTS) -> list[Variant]:
    """A new list with this run added; only the last `limit` are kept."""
    return [*history, Variant(run, variation)][-limit:]


def next_variation(history: list[Variant]) -> int:
    return max((v.variation for v in history), default=-1) + 1


# --------------------------------------------------------------------------- #
# Services that live as long as the app
# --------------------------------------------------------------------------- #

_SERVICES: dict[tuple, Services] = {}


def load_settings() -> Settings:
    """Settings from .env (a function, so the dashboard tests can swap it)."""
    return Settings.from_env()


def services_for(settings: Settings, *, ai: bool, remover: Remover | None = None) -> Services:
    """The same services for every click (one HTTP client, one cache), per mode.

    Streamlit reruns app.py on every click; building a new client each time
    would lose the session's knowledge of which models are out of quota.
    """
    key = (ai, _settings_key(settings))
    if key not in _SERVICES:
        _SERVICES[key], _client = build_services(settings, offline=not ai, remover=remover)
    return _SERVICES[key]


def _settings_key(settings: Settings) -> tuple:
    # Settings holds lists, so it isn't hashable as it is.
    return tuple((f.name, tuple(v) if isinstance(v := getattr(settings, f.name), list) else v)
                 for f in fields(settings))
