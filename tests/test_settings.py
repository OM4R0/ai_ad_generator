"""Tests for config.settings (NFR-3)."""

from __future__ import annotations

import pytest

from config.settings import Settings

ENV_VARS = (
    "LLM_BASE_URL", "LLM_API_KEY", "VISION_MODEL", "COPY_MODEL", "IMAGE_PROVIDER",
    "LLM_TIMEOUT_S", "FALLBACK_MODELS", "CATALOG_PATH", "LLM_FALLBACK_MODELS",
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
