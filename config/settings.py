"""Runtime settings read from the environment / `.env` (design.md §10, NFR-3).

Nothing provider-specific is hardcoded in the pipeline: model IDs, endpoints,
keys and limits all come from here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    """Read an env var; an empty value (e.g. `LLM_BASE_URL=` in .env) means 'use the default'."""
    return os.getenv(name, "").strip() or default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in _env(name).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the configuration."""

    # Paths
    project_root: Path = PROJECT_ROOT
    catalog_path: Path = PROJECT_ROOT / "data" / "products_master.xlsx"
    output_dir: Path = PROJECT_ROOT / "output"
    cache_dir: Path = PROJECT_ROOT / ".cache"
    prompts_dir: Path = PROJECT_ROOT / "config" / "prompt_templates"
    brands_dir: Path = PROJECT_ROOT / "config" / "brands"
    fonts_dir: Path = PROJECT_ROOT / "assets" / "fonts"

    # Primary LLM (ADR-003): any OpenAI-compatible endpoint
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    vision_model: str = ""
    copy_model: str = ""
    # Other models on the SAME provider/key, tried in order if the main one fails
    llm_fallback_models: list[str] = field(default_factory=list)

    # Fallback LLM provider (optional)
    fallback_base_url: str = "https://openrouter.ai/api/v1"
    fallback_api_key: str = ""
    fallback_models: list[str] = field(default_factory=list)

    # Images (ADR-007)
    image_provider: str = "pollinations"

    # Limits
    llm_timeout_s: float = 60.0
    max_requests_per_min: float = 10.0
    # FR-14, the "what this would cost on a paid plan" estimate, in USD per 1M tokens.
    # Reference: Gemini 3.5 Flash standard paid rates (public price lists, 2026-11;
    # check ai.google.dev/gemini-api/docs/pricing). Flash-Lite is cheaper, so for the
    # vision step this is an upper bound. Free tiers really cost $0 ("actual" cost).
    reference_input_price_per_mtok: float = 1.50
    reference_output_price_per_mtok: float = 9.00

    @property
    def llm_configured(self) -> bool:
        """True if at least one LLM provider can be called (Gemini, or the backup alone)."""
        primary = bool(self.llm_api_key and self.vision_model and self.copy_model)
        return primary or self.backup_configured

    @property
    def backup_configured(self) -> bool:
        return bool(self.fallback_api_key and self.fallback_models)

    def config_warnings(self) -> list[str]:
        """Setup mistakes that would silently switch something off. Never includes a key."""
        warnings: list[str] = []
        if not self.llm_configured:
            warnings.append("no LLM configured: every product will use template copy "
                            "(set LLM_API_KEY and/or FALLBACK_API_KEY in .env)")
        if self.fallback_api_key and not self.fallback_models:
            warnings.append("FALLBACK_API_KEY is set but FALLBACK_MODELS is empty: "
                            "the backup provider will never be used")
        if self.fallback_models and not self.fallback_api_key:
            warnings.append("FALLBACK_MODELS is set but FALLBACK_API_KEY is empty: "
                            "the backup provider will never be used")
        if self.llm_api_key.startswith("sk-or-") and "openrouter" not in self.llm_base_url:
            warnings.append("LLM_API_KEY looks like an OpenRouter key (sk-or-...) but "
                            "LLM_BASE_URL is not OpenRouter: put it in FALLBACK_API_KEY")
        return warnings

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """Load `.env` (if present) and build settings; env vars win over the file."""
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        vision_model = _env("VISION_MODEL")
        defaults = cls()
        return cls(
            catalog_path=Path(_env("CATALOG_PATH") or defaults.catalog_path),
            llm_base_url=_env("LLM_BASE_URL", defaults.llm_base_url),
            llm_api_key=_env("LLM_API_KEY"),
            vision_model=vision_model,
            copy_model=_env("COPY_MODEL") or vision_model,  # one model by default (DD-2)
            llm_fallback_models=_env_list("LLM_FALLBACK_MODELS"),
            fallback_base_url=_env("FALLBACK_BASE_URL", defaults.fallback_base_url),
            fallback_api_key=_env("FALLBACK_API_KEY"),
            fallback_models=_env_list("FALLBACK_MODELS"),
            image_provider=_env("IMAGE_PROVIDER", defaults.image_provider),
            llm_timeout_s=_env_float("LLM_TIMEOUT_S", defaults.llm_timeout_s),
            max_requests_per_min=_env_float(
                "MAX_REQUESTS_PER_MIN", defaults.max_requests_per_min
            ),
            reference_input_price_per_mtok=_env_float(
                "REFERENCE_INPUT_PRICE_PER_MTOK", defaults.reference_input_price_per_mtok
            ),
            reference_output_price_per_mtok=_env_float(
                "REFERENCE_OUTPUT_PRICE_PER_MTOK", defaults.reference_output_price_per_mtok
            ),
        )
