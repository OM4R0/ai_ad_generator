"""CLI: generate ad campaigns for catalog products in one command (FR-12).

    python main.py                     # the 10 demo products (ADR-005)
    python main.py --ids 13 23         # chosen products, in this order
    python main.py --all               # all 30 products
    python main.py --offline           # no network: cached answers + template copy + gradients
    python main.py --refresh           # ignore cached AI answers (spends requests!)

Output: output/{Brand}/{Product}/ (spec §5). Exit code: 0 = every product OK,
1 = at least one product failed, 2 = bad input (unknown product ID, unreadable catalog).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from config.settings import Settings
from core.excel_loader import READ_ERRORS, LoadResult, load_catalog
from core.output_writer import usage_totals
from core.pipeline import ProductRun, open_services, run_batch
from core.schemas import Product
from core.visual_engine import Remover

# ADR-005: products whose reference photo shows the right kind of item, no other brand.
DEMO_PRODUCT_IDS = (2, 13, 19, 22, 3, 20, 23, 30, 21, 24)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="AI ad generator: catalog -> copy + creatives per product.")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--ids", nargs="+", type=int, metavar="ID",
                       help="product IDs from the catalog (default: the 10 demo products)")
    which.add_argument("--all", action="store_true", help="every product in the catalog")
    parser.add_argument("--offline", action="store_true",
                        help="no network: reuse cached answers, otherwise template copy + gradients")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached AI answers and ask again (spends requests)")
    parser.add_argument("--output", type=Path, help="output folder (default: output/)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every retry and fallback")
    return parser


def select_products(catalog: LoadResult, args: argparse.Namespace) -> list[Product]:
    """The products to run, in order. Raises KeyError naming any unknown ID."""
    if args.all:
        return list(catalog.products)
    ids = list(dict.fromkeys(args.ids or DEMO_PRODUCT_IDS))  # keep order, drop repeats
    known = {p.id for p in catalog.products}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise KeyError(f"unknown product ID(s): {', '.join(map(str, unknown))}; "
                       f"the catalog has IDs {min(known)}-{max(known)}")
    return [catalog.by_id(i) for i in ids]


def main(argv: list[str] | None = None, *, settings: Settings | None = None,
         remover: Remover | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Arabic names and errors on a Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="  log: %(message)s")

    settings = settings or Settings.from_env()
    if args.output:
        settings = replace(settings, output_dir=args.output)

    try:
        catalog = load_catalog(settings.catalog_path)
        products = select_products(catalog, args)
    except (KeyError, *READ_ERRORS) as exc:
        print(f"error: {exc.args[0] if isinstance(exc, KeyError) else exc}", file=sys.stderr)
        return 2

    print(_header(settings, len(products), offline=args.offline))
    for warning in settings.config_warnings():
        print(f"  ! setup: {warning}")

    with open_services(settings, offline=args.offline, refresh=args.refresh,
                       remover=remover) as services:
        runs = run_batch(products, services,
                         on_progress=lambda i, n, run: print(format_run(i, n, run), flush=True))

    print(format_summary(runs, settings.output_dir, offline=args.offline))
    return exit_code(runs)


# --------------------------------------------------------------------------- #
# Printing
# --------------------------------------------------------------------------- #


def _header(settings: Settings, count: int, *, offline: bool) -> str:
    if offline:
        llm = "offline (no AI calls; cached answers reused)"
    else:
        parts = []
        if settings.llm_api_key:
            parts.append(f"vision {settings.vision_model}, copy {settings.copy_model}")
        if settings.backup_configured:
            parts.append(f"backup: {len(settings.fallback_models)} OpenRouter model(s)")
        llm = " | ".join(parts) or "not configured"
    scenes = "gradient" if offline else settings.image_provider
    return f"AI Ad Generator: {count} product(s)\n  LLM: {llm}\n  scenes: {scenes}"


def format_run(index: int, total: int, run: ProductRun) -> str:
    """One product's line, plus its fallbacks and the warnings a human should check."""
    p = run.product
    head = f"[{index:>2}/{total}] #{p.id} {p.name} ({p.language.display_name})"
    if not run.ok:
        return f"{head}  FAIL\n         {run.error}"
    result = run.result
    seconds = result.timings_s.get("total")
    lines = [f"{head}  OK" + (f" {seconds:.0f}s" if seconds is not None else "")]
    if result.usage:
        lines.append("         " + " | ".join(
            f"{u.step}: {u.served_model or u.model}{' (cache)' if u.cached else ''}"
            for u in result.usage))
    lines += [f"         ! fallback: {f}" for f in result.fallbacks]
    lines += [f"         ! check: {w}" for w in result.warnings]
    return "\n".join(lines)


def format_summary(runs: list[ProductRun], output_dir: Path, *, offline: bool = False) -> str:
    ok = [r for r in runs if r.ok]
    usage = [u for r in ok for u in r.result.usage]
    totals = usage_totals(usage)
    fallbacks = sum(len(r.result.fallbacks) for r in ok)
    warnings = sum(len(r.result.warnings) for r in ok)
    live = totals["llm_calls"] - totals["cached_calls"]
    tokens = totals["prompt_tokens"] + totals["completion_tokens"]
    lines = [
        "-" * 60,
        f"Done: {len(ok)}/{len(runs)} products OK, {len(runs) - len(ok)} failed"
        + (" (offline)" if offline else ""),
        f"{totals['llm_calls']} LLM calls ({live} live, {totals['cached_calls']} from cache), "
        f"{tokens:,} tokens",
        f"Cost: ${totals['actual_cost_usd']:.4f} actual (free tiers); "
        f"${totals['spent_this_run_usd']:.4f} this run at the reference paid price",
        f"{fallbacks} fallback(s), {warnings} warning(s) to check",
        f"Output: {output_dir}",
    ]
    return "\n".join(lines)


def exit_code(runs: list[ProductRun]) -> int:
    return 0 if runs and all(r.ok for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
