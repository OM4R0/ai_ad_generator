"""Dashboard (FR-13). Run with:   streamlit run app.py

This file only draws the page. Every decision is in `core/dashboard.py`, and the
ads are made by `core/pipeline.py`, the same code the CLI (`main.py`) runs.

Streamlit reruns this whole script on every click; what must survive a click
lives in `st.session_state`:
- variants[product_id]    the generations of each product (ADR-004: kept in the session)
- manual[product_id]      the user's brand edits so far (merged, never lost)
- pdf_patches[brand_name] what the brand PDF said (per brand, not per product)
"""

from __future__ import annotations

import hashlib

import streamlit as st

from core import dashboard
from core.brand_manager import BrandConfigError, patch_from_pdf, resolve_brand
from core.excel_loader import load_catalog
from core.output_writer import campaign_zip, slugify, usage_totals, write_campaign
from core.pipeline import run_product
from core.schemas import BrandProfile, Language
from main import DEMO_PRODUCT_IDS

PHOTO_SOURCES = ("Catalog photo", "Upload a photo", "Image URL")
PLATFORMS = ("instagram", "tiktok", "facebook", "linkedin")
MAX_COLOUR_PICKERS = 4

st.set_page_config(page_title="AI Ad Generator", layout="wide")
state = st.session_state
for name in ("variants", "manual", "pdf_patches", "zips"):
    state.setdefault(name, {})
state.setdefault("flash", [])

settings = dashboard.load_settings()


@st.cache_resource(show_spinner=False)
def _catalog(path: str):
    return load_catalog(path)


def _form_key(product_id: int, brand: BrandProfile) -> str:
    """Version of the brand form: a new brand to show (e.g. after the vision step)
    means new widgets, so the form never sends stale values back."""
    text = f"{product_id}|{brand.brand_voice}|{brand.brand_colors}|{brand.target_market}|{brand.language}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _flash(kind: str, message: str) -> None:
    """A message that survives st.rerun() (shown at the top of the next run)."""
    state.flash.append((kind, message))


def _copy_block(variant, direction: str) -> None:
    bullets = "\n".join(f"• {b}" for b in variant.bullets)
    for label, text in (("Headline", variant.headline), ("Hook", variant.hook),
                        ("Primary text", variant.primary_text), ("Bullets", bullets),
                        ("CTA", variant.cta)):
        st.caption(label)
        st.markdown(dashboard.rtl_block(text, direction), unsafe_allow_html=True)


catalog = _catalog(str(settings.catalog_path))
products = {p.id: p for p in dashboard.product_choices(catalog.products, DEMO_PRODUCT_IDS)}

# --------------------------------------------------------------------------- #
# Sidebar: product, photo, brand, actions
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.title("AI Ad Generator")
    product_id = st.selectbox("Product", list(products), key="product",
                              format_func=lambda i: dashboard.product_label(products[i]))
    product = products[product_id]

    ai_ready = settings.llm_configured
    use_ai = st.toggle("Use AI", value=ai_ready, disabled=not ai_ready, key="use_ai",
                       help="Gemini, with OpenRouter free models as the backup")
    if not ai_ready:
        st.caption("No API key in .env: ads use template copy and brand-gradient backgrounds.")
    for warning in settings.config_warnings():
        st.caption(f"⚠ {warning}")
    services = dashboard.services_for(settings, ai=use_ai)

    st.subheader("Product photo")
    source = st.radio("Photo source", PHOTO_SOURCES, key="image_source",
                      label_visibility="collapsed")
    upload = image_url = None
    if source == PHOTO_SOURCES[1]:
        file = st.file_uploader("Photo", type=["png", "jpg", "jpeg", "webp"], key="upload")
        upload = file.getvalue() if file is not None else None
        if file is None:
            st.caption("No file yet: the catalog photo is used.")
    elif source == PHOTO_SOURCES[2]:
        image_url = st.text_input("Image URL", key="image_url").strip() or None

    history = state.variants.get(product_id, [])
    pdf_patch = state.pdf_patches.get(product.brand_name)
    manual = state.manual.get(product_id)
    base = (history[-1].run.result.brand if history else
            resolve_brand(product, brands_dir=settings.brands_dir, pdf_patch=pdf_patch,
                          manual_patch=manual))
    form_key = state["brand_form_key"] = _form_key(product_id, base)

    with st.expander(f"Brand: {product.brand_name}"):
        pdf = st.file_uploader("Brand guidelines (PDF)", type=["pdf"], key="brand_pdf")
        if st.button("Read the PDF", key="read_pdf", disabled=pdf is None):
            extractor = (dashboard.make_pdf_extractor(services.llm, settings.copy_model)
                         if use_ai else None)
            try:
                extraction = patch_from_pdf(pdf.getvalue(), extractor=extractor)
            except BrandConfigError as exc:
                st.error(f"Could not read the PDF: {exc}")
            else:
                state.pdf_patches[product.brand_name] = extraction.patch
                for warning in extraction.warnings:
                    _flash("warning", f"PDF: {warning}")
                how = "with the AI" if extraction.llm_used else "colours only (no AI)"
                _flash("success", f"Read the brand PDF {how}.")
                st.rerun()

        voice = st.text_area("Voice", base.brand_voice, key=f"voice_{form_key}")
        shown = base.brand_colors[:MAX_COLOUR_PICKERS]
        picked = [column.color_picker(f"Colour {i + 1}", colour, key=f"color_{i}_{form_key}")
                  for i, (column, colour) in enumerate(zip(st.columns(max(1, len(shown))), shown))]
        colours = picked + list(base.brand_colors[MAX_COLOUR_PICKERS:])
        market = st.text_input("Target market", base.target_market, key=f"market_{form_key}")
        languages = list(Language)
        language = st.selectbox("Language", languages, index=languages.index(base.language),
                                format_func=lambda lang: lang.display_name,
                                key=f"language_{form_key}")

    left, right = st.columns(2)
    generate = left.button("Generate", type="primary", key="generate")
    regenerate = right.button("New angle", key="regenerate", disabled=not history,
                              help="Ask for a different hook and headline (Regenerate)")

# --------------------------------------------------------------------------- #
# Generating
# --------------------------------------------------------------------------- #

if generate or regenerate:
    edit = dashboard.brand_patch_from_form(base, voice=voice, colors=colours,
                                           market=market, language=language)
    manual = state.manual[product_id] = dashboard.merge_patches(manual, edit)
    variation = dashboard.next_variation(history) if regenerate else 0
    with st.spinner("Making the ad… (a product's first run can take a minute)"):
        run = run_product(product, services, write=False, upload=upload, image_url=image_url,
                          pdf_patch=pdf_patch, manual_patch=manual, variation=variation)
    if run.ok:
        state.variants[product_id] = dashboard.add_variant(history, run, variation)
        st.rerun()  # redraw the brand form from the new result
    st.error(f"Could not make this ad: {run.error}")

# --------------------------------------------------------------------------- #
# Main area: the chosen variant
# --------------------------------------------------------------------------- #

st.header(product.name)
st.caption(f"{product.brand_name} · {product.category} · {product.language.display_name}")
while state.flash:
    kind, message = state.flash.pop(0)
    getattr(st, kind)(message)

history = state.variants.get(product_id, [])
if not history:
    st.info("Choose the photo and the brand on the left, then press **Generate**.")
    st.stop()

labels = [f"#{i + 1} " + ("(first)" if v.variation == 0 else f"(angle {v.variation})")
          for i, v in enumerate(history)]
chosen = st.radio("Variant", range(len(history)), index=len(history) - 1, horizontal=True,
                  format_func=lambda i: labels[i], key=f"variant_{product_id}_{len(history)}")
run = history[chosen].run
result, creatives, copy = run.result, run.creatives, run.result.copy_

st.caption(f"Photo: {result.image_source} · product: {result.product_mode} · "
           f"background: {result.background_sources.get('story', '?')} · "
           f"{result.timings_s.get('total', 0):.0f} s")
for warning in result.warnings:
    st.warning(f"Check: {warning}")
for fallback in result.fallbacks:
    st.info(f"Fallback: {fallback}")

feed_tab, story_tab, carousel_tab, captions_tab, cost_tab = st.tabs(
    ["Feed A/B", "Story", "Carousel", "Captions", "Cost & details"])

with feed_tab:
    for column, image, variant, name in zip(st.columns(2), creatives.feed_variants,
                                            copy.variants, ("A · pain point", "B · emotional")):
        with column:
            st.image(image, caption=name, width="stretch")
            _copy_block(variant, copy.direction)

with story_tab:
    left, right = st.columns([1, 2])
    left.image(creatives.story, width=360)
    with right:
        _copy_block(copy.variants[0], copy.direction)

with carousel_tab:
    total = len(creatives.carousel)
    for i, (column, image) in enumerate(zip(st.columns(total), creatives.carousel), start=1):
        column.image(image, caption=f"{i}/{total}", width="stretch")

with captions_tab:
    for platform in PLATFORMS:
        caption = copy.captions[platform]
        st.subheader(platform.title())
        text = caption.text + "\n\n" + " ".join(caption.hashtags)
        st.markdown(dashboard.rtl_block(text, copy.direction), unsafe_allow_html=True)

with cost_tab:
    totals = usage_totals(result.usage)
    tokens, actual, estimated = st.columns(3)
    tokens.metric("Tokens", f"{totals['prompt_tokens'] + totals['completion_tokens']:,}")
    actual.metric("Actual cost", f"${totals['actual_cost_usd']:.4f}", help="Free tiers")
    estimated.metric("At the reference paid price", f"${totals['estimated_cost_usd']:.4f}")
    rows = dashboard.cost_rows(result)
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.caption("No AI calls for this ad (offline, or the AI failed and templates were used).")
    st.caption(f"Prompt versions: {result.prompt_versions} · timings (s): {result.timings_s}")

# --------------------------------------------------------------------------- #
# Save / export (ADR-004: only the chosen variant is saved)
# --------------------------------------------------------------------------- #

st.divider()
save_column, zip_column = st.columns(2)
if save_column.button("Save to output/", key="save"):
    try:
        folder = write_campaign(result, creatives, settings.output_dir)
    except OSError as exc:
        st.error(f"Could not save: {exc}")
    else:
        st.success(f"Saved to {folder}")

live = {id(v.run) for versions in state.variants.values() for v in versions}
state.zips = {key: data for key, data in state.zips.items() if key in live}
if id(run) not in state.zips:
    state.zips[id(run)] = campaign_zip(result, creatives)
zip_column.download_button(
    "Download .zip", state.zips[id(run)], mime="application/zip", key="download",
    file_name=f"{slugify(product.name, fallback=f'product_{product.id}')}.zip")
