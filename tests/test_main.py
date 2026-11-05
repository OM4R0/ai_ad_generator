"""Tests for main.py, the CLI (FR-12): one command makes N products and prints a summary."""

from __future__ import annotations

from dataclasses import replace

import pytest

import main
from config.settings import PROJECT_ROOT, Settings
from core.excel_loader import load_catalog
from core.pipeline import ProductRun
from core.schemas import CallUsage, CampaignResult
from tests.test_visual_engine import fake_remover

CATALOG = load_catalog(PROJECT_ROOT / "data" / "products_master.xlsx")
HEADPHONES = CATALOG.by_id(13)


def _select(*argv):
    return [p.id for p in main.select_products(CATALOG, main.build_parser().parse_args(argv))]


def _offline_settings(tmp_path, **overrides) -> Settings:
    # No keys at all: proves --offline needs nothing from .env
    return replace(Settings(), output_dir=tmp_path / "output", cache_dir=tmp_path / "cache",
                   **overrides)


def _ok_run(product, usage=(), fallbacks=(), warnings=()) -> ProductRun:
    from core.brand_manager import resolve_brand
    result = CampaignResult(product=product, brand=resolve_brand(product), usage=list(usage),
                            fallbacks=list(fallbacks), warnings=list(warnings))
    return ProductRun(product, result=result)


# --------------------------------------------------------------------------- #
# Choosing products
# --------------------------------------------------------------------------- #

def test_m1_the_default_is_the_ten_demo_products_of_adr_005():
    assert _select() == [2, 13, 19, 22, 3, 20, 23, 30, 21, 24]


def test_m2_ids_are_taken_in_order_without_repeats():
    assert _select("--ids", "23", "13", "23") == [23, 13]


def test_m3_all_means_the_whole_catalog():
    assert len(_select("--all")) == 30


def test_m4_an_unknown_id_is_a_clear_error_and_nothing_runs(tmp_path, capsys):
    code = main.main(["--offline", "--ids", "13", "99"], settings=_offline_settings(tmp_path),
                     remover=fake_remover)
    assert code == 2
    assert "99" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_m5_ids_and_all_together_is_rejected():
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["--all", "--ids", "13"])


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def test_m6_offline_run_makes_every_folder_and_a_summary(tmp_path, capsys):
    code = main.main(["--offline", "--ids", "13", "23"], settings=_offline_settings(tmp_path),
                     remover=fake_remover)
    out = capsys.readouterr().out
    assert code == 0
    assert len(list((tmp_path / "output").glob("*/*/metadata.json"))) == 2
    assert "2/2" in out and "offline" in out.lower()
    assert "#13" in out and "#23" in out


def test_m7_the_output_option_wins_over_the_settings(tmp_path):
    target = tmp_path / "elsewhere"
    main.main(["--offline", "--ids", "13", "--output", str(target)],
              settings=_offline_settings(tmp_path), remover=fake_remover)
    assert len(list(target.glob("*/*/metadata.json"))) == 1


def test_m8_setup_mistakes_are_shown_before_the_run(tmp_path, capsys):
    settings = _offline_settings(tmp_path, fallback_api_key="sk-or-x", fallback_models=[])
    main.main(["--offline", "--ids", "13"], settings=settings, remover=fake_remover)
    out = capsys.readouterr().out
    assert "FALLBACK_MODELS" in out and "sk-or-x" not in out


# --------------------------------------------------------------------------- #
# What it prints, and the exit code
# --------------------------------------------------------------------------- #

LIVE = CallUsage(step="vision", provider="gemini", model="gemini-a", served_model="gemini-a",
                 prompt_tokens=1000, completion_tokens=500, estimated_cost_usd=0.0015)
REPLAY = CallUsage(step="copy", provider="openrouter.ai", model="openrouter/free",
                   served_model="vendor/x:free", prompt_tokens=2000, completion_tokens=1000,
                   estimated_cost_usd=0.003, cached=True)


def test_m9_a_product_line_names_the_models_that_really_answered():
    text = main.format_run(1, 10, _ok_run(HEADPHONES, usage=[LIVE, REPLAY]))
    assert "#13" in text and "OK" in text
    assert "gemini-a" in text and "vendor/x:free" in text and "cache" in text


def test_m9_fallbacks_and_warnings_are_shown_under_the_product():
    text = main.format_run(1, 1, _ok_run(HEADPHONES, fallbacks=["copy: template (quota)"],
                                         warnings=["the photo shows Chanel"]))
    assert "copy: template (quota)" in text and "the photo shows Chanel" in text


def test_m9_a_failed_product_shows_why():
    text = main.format_run(2, 10, ProductRun(HEADPHONES, error="ImageSourceError: no photo"))
    assert "FAIL" in text and "no photo" in text


def test_m10_the_summary_counts_successes_calls_tokens_and_cost(tmp_path):
    runs = [_ok_run(HEADPHONES, usage=[LIVE, REPLAY], fallbacks=["scene: gradient"]),
            ProductRun(HEADPHONES, error="boom")]
    text = main.format_summary(runs, tmp_path)
    assert "1/2" in text and "1 failed" in text
    assert "2 LLM calls" in text and "1 live" in text and "1 from cache" in text
    assert "4,500 tokens" in text
    assert "$0.0015" in text                     # only the live call is spent this run
    assert "1 fallback" in text
    assert str(tmp_path) in text


@pytest.mark.parametrize("oks, expected", [([True, True], 0), ([True, False], 1), ([], 1)])
def test_m11_exit_code_is_non_zero_if_any_product_failed(oks, expected):
    runs = [_ok_run(HEADPHONES) if ok else ProductRun(HEADPHONES, error="x") for ok in oks]
    assert main.exit_code(runs) == expected
