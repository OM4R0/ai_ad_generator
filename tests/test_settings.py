"""Tests for config.settings (NFR-3)."""

from __future__ import annotations

import pytest

from config.settings import Settings

ENV_VARS = (
    "LLM_BASE_URL", "LLM_API_KEY", "VISION_MODEL", "COPY_MODEL", "IMAGE_PROVIDER",
    "LLM_TIMEOUT_S", "FALLBACK_MODELS", "CATALOG_PATH", "LLM_FALLBACK_MODELS",
    "FALLBACK_API_KEY", "FALLBACK_BASE_URL",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "missing.env"  # an env file that doesn't exist


def test_empty_values_fall_back_to_defaults(monkeypatch, clean_env):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("IMAGE_PROVIDER", "  ")
    s = Settings.from_env(clean_env)
    assert s.llm_base_url.startswith("https://generativelanguage.googleapis.com")
    assert s.image_provider == "pollinations"


def test_copy_model_defaults_to_vision_model(monkeypatch, clean_env):
    monkeypatch.setenv("VISION_MODEL", "gemini-x")
    assert Settings.from_env(clean_env).copy_model == "gemini-x"
    monkeypatch.setenv("COPY_MODEL", "other-model")
    assert Settings.from_env(clean_env).copy_model == "other-model"


def test_llm_configured_needs_key_and_model(monkeypatch, clean_env):
    assert not Settings.from_env(clean_env).llm_configured
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("VISION_MODEL", "m")
    assert Settings.from_env(clean_env).llm_configured


def test_bad_number_gives_a_clear_error(monkeypatch, clean_env):
    monkeypatch.setenv("LLM_TIMEOUT_S", "sixty")
    with pytest.raises(ValueError, match="LLM_TIMEOUT_S must be a number"):
        Settings.from_env(clean_env)


def test_fallback_models_list(monkeypatch, clean_env):
    monkeypatch.setenv("FALLBACK_MODELS", "a:free, b:free ,,")
    assert Settings.from_env(clean_env).fallback_models == ["a:free", "b:free"]


def test_same_provider_fallback_models_list(monkeypatch, clean_env):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "gemini-a, gemini-b")
    assert Settings.from_env(clean_env).llm_fallback_models == ["gemini-a", "gemini-b"]


# --------------------------------------------------------------------------- #
# config_warnings(): mistakes that would silently switch a feature off
# --------------------------------------------------------------------------- #

def _full(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "AIza-test")
    monkeypatch.setenv("VISION_MODEL", "gemini-a")
    monkeypatch.setenv("FALLBACK_API_KEY", "sk-or-test")
    monkeypatch.setenv("FALLBACK_MODELS", "a:free")


def test_a_full_setup_has_no_warnings(monkeypatch, clean_env):
    _full(monkeypatch)
    assert Settings.from_env(clean_env).config_warnings() == []


def test_backup_key_without_models_is_reported(monkeypatch, clean_env):
    # Happened for real on 2026-11-05: the key was added, FALLBACK_MODELS was empty,
    # so the backup would never have been used.
    _full(monkeypatch)
    monkeypatch.setenv("FALLBACK_MODELS", "")
    warnings = Settings.from_env(clean_env).config_warnings()
    assert any("FALLBACK_MODELS" in w for w in warnings)


def test_backup_models_without_key_is_reported(monkeypatch, clean_env):
    _full(monkeypatch)
    monkeypatch.setenv("FALLBACK_API_KEY", "")
    assert any("FALLBACK_API_KEY" in w for w in Settings.from_env(clean_env).config_warnings())


def test_no_llm_at_all_is_reported(monkeypatch, clean_env):
    warnings = Settings.from_env(clean_env).config_warnings()
    assert any("template" in w for w in warnings)


def test_an_openrouter_key_in_the_gemini_slot_is_reported(monkeypatch, clean_env):
    _full(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-or-oops")
    assert any("LLM_API_KEY" in w and "OpenRouter" in w
               for w in Settings.from_env(clean_env).config_warnings())


def test_warnings_never_contain_the_keys(monkeypatch, clean_env):
    _full(monkeypatch)
    monkeypatch.setenv("FALLBACK_MODELS", "")
    monkeypatch.setenv("LLM_API_KEY", "sk-or-oops")
    text = " ".join(Settings.from_env(clean_env).config_warnings())
    assert "sk-or-oops" not in text and "sk-or-test" not in text


def test_openrouter_alone_counts_as_configured(monkeypatch, clean_env):
    monkeypatch.setenv("FALLBACK_API_KEY", "sk-or-test")
    monkeypatch.setenv("FALLBACK_MODELS", "a:free")
    assert Settings.from_env(clean_env).llm_configured
