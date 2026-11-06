# Design — AI Ad Generation Workflow

> **Status:** v1.0 — accepted by owner 2026-11-05
> **Implements:** [spec.md](spec.md) v0.3
> The spec says **what** the system does; this document says **how**. Every component below traces back to a requirement (FR-x / NFR-x).

---

## 1. Pipeline at a glance

```
 data/products_master.xlsx ──► [1] excel_loader ──► Product
                                                       │
 config/brands/*.json ─┐                               ▼
 brand PDF upload ─────┼──► [2] brand_manager ──► BrandProfile
 dashboard edits ──────┘                               │
                                                       ▼
 product image ───────────► [3] vision_agent ──► VisionAnalysis       (LLM call #1)
                                                       │
                                                       ▼
                             [4] copy_engine ──► AdCopy               (LLM call #2)
                                                       │
                                                       ▼
                             [5] visual_engine
                                  ├─ remove background   (local model)
                                  ├─ scene_generator ──► AI background (image model)
                                  ├─ composite + text + logo
                                  └─► feed / story / carousel / mockup PNGs
                                                       │
                                                       ▼
                             [6] output_writer ──► output/{Brand}/{Product}/
```

`core/pipeline.py` runs these steps in order. The **CLI (`main.py`) and the dashboard (`app.py`) both call the same pipeline**, so they cannot behave differently.

## 2. Models: how many and what for

| # | Role | Where it runs | Default | Calls per product |
|---|---|---|---|---|
| 1 | **Multimodal LLM**: looks at the image (vision) **and** writes the copy | **Gemini** free tier (fallback: OpenRouter `:free`) | Gemini Flash, confirmed by benchmark (ADR-003) | 2 (vision + copy) |
| 2 | **Image generator**: paints the scene/background; paints the product only when the user picks *AI-generated photo* (flagged for a human check) | Pollinations (FLUX, no key) | ADR-007 | 1 (the story scene; the square is cut from its middle, carousel reuses it), +1 for an AI photo |
| 3 | **Background remover**: cuts the product out of its photo | Local (`rembg`) | `u2net` / `isnet-general-use` | 0 API calls |

So: **two external models + one small local model.**

- Vision and copy use the **same LLM by default**, but they are **two separate config keys** (`VISION_MODEL`, `COPY_MODEL`), so either can be swapped on its own if one model is better at seeing and another at writing.
- Image-generator calls **do not count** against the LLM daily cap.
- Gemini and OpenRouter both speak the **OpenAI-compatible chat protocol**, so one client handles both; the provider is pure config (ADR-003).
- Brand-PDF extraction reuses LLM #1 (1 call per upload, not per product).

## 3. Module layout

```
core/
├── schemas.py          Data contracts (Pydantic models) shared by every module
├── excel_loader.py     [1] catalog → list[Product]                         FR-1
├── brand_manager.py    [2] merge brand sources → BrandProfile; PDF extract FR-4
├── image_source.py     resolve local file / URL / upload → PIL image      FR-2
├── llm_client.py       OpenAI-compatible client (Gemini / OpenRouter): retry, timeout, JSON-validate,
│                       fallback model list, token/cost accounting         NFR-2/3/4, FR-14
├── vision_agent.py     [3] image + product → VisionAnalysis               FR-3
├── copy_engine.py      [4] product + brand + vision → AdCopy              FR-5/6/7/9
├── scene_generator.py  AI background provider chain + gradient fallback   ADR-001
├── visual_engine.py    [5] cut-out, composite, layout, text (incl. RTL)   FR-8/9/10
├── mockup.py           the feed ad inside an Instagram post               FR-15
├── text_render.py      fonts, Arabic shaping + RTL, fitting text in a box FR-10
├── output_writer.py    [6] folder structure, copy.json, metadata.json     FR-11
├── cache.py            on-disk cache of LLM / image results               NFR-7
└── pipeline.py         orchestrates 1→6 for one product; batch helper     FR-12
config/
├── settings.py         reads .env (model IDs, keys, timeouts, limits)     NFR-3
├── brand_sample.json   example structured brand profile                   FR-4
├── brands/             optional per-brand overrides (matched by name)
└── prompt_templates/   versioned prompt files (see §6)
assets/fonts/           Latin + Arabic fonts (open license)                ADR-005
main.py                 CLI
app.py                  Streamlit dashboard: drawing only                   FR-13
core/dashboard.py       the dashboard's decisions, testable without a browser FR-10/13/14
```

**Rule:** modules communicate only through the `schemas.py` objects. No module reads another module's internals.

## 4. Data contracts (`core/schemas.py`)

Pydantic is used so that every hand-off is typed and validated, including the LLM output.

```python
class Product:            # one catalog row
    id: int; name: str; brand_name: str; category: str
    description: str; features: list[str]          # Feature 1..5
    target_audience: str; price_range: str; usp: str
    tone: str; language: Language                  # en | ar | fr | es | de
    image_url: str | None; image_path: Path | None

class BrandProfile:
    brand_name: str; brand_voice: str
    brand_colors: list[HexColor]                    # [primary, accent, text]
    target_market: str; language: Language
    arabic_register: str = "msa_gulf"               # ADR-006
    logo_url: str | None
    sources: list[str]                              # which sources filled it (audit)

class VisionAnalysis:
    product_type: str
    dominant_colors: list[HexColor]
    materials_finish: list[str]                     # "brushed steel", "matte"
    mood: str                                       # "premium, minimal"
    visual_selling_points: list[str]                # what the photo shows well
    scene_prompt: str                               # suggested background scene
    cutout_quality_hint: str                        # "clean" | "busy background"

class CopyVariant:
    angle: Literal["pain_point", "emotional"]       # FR-6
    headline: str; hook: str; primary_text: str
    bullets: list[str]                              # exactly 3
    cta: str

class AdCopy:
    language: Language; direction: Literal["ltr", "rtl"]
    variants: list[CopyVariant]                     # exactly 2
    carousel: list[CarouselCard]                    # 3–5: hook → features → CTA
    captions: dict[Platform, Caption]               # instagram, tiktok, facebook, linkedin

class CampaignResult:                               # → metadata.json
    product: Product; brand: BrandProfile; vision: VisionAnalysis | None
    copy: AdCopy; files: list[Path]
    usage: list[CallUsage]                          # model, tokens, est. cost, latency
    fallbacks: list[str]                            # e.g. "scene: gradient"
    prompt_versions: dict[str, str]; created_at: datetime
```

The LLM returns an `AdCopyDraft` (variants, carousel, captions); our code adds `language` and `direction` to make the `AdCopy`, so the text direction is never trusted from the model.

`copy.json` = `AdCopy` serialised. `metadata.json` = `CampaignResult` minus the copy text.

## 5. Per-product flow & call budget

| Step | Input | Output | External calls | Fallback if it fails |
|---|---|---|---|---|
| Load row | xlsx | `Product` | 0 | skip row, log error |
| Brand | row + config/PDF/edits | `BrandProfile` | 0 (PDF: 1 per upload) | catalog-derived defaults; missing colours: vision colours → cut-out → raw photo → default palette (see note) |
| Image | path / URL / upload | PIL image | 0–1 HTTP | URL → local → error for this product only |
| Vision | image + product | `VisionAnalysis` | **1 LLM** | local colour extraction + catalog text only |
| Copy | product + brand + vision | `AdCopy` | **1 LLM** | 1 repair retry → next model → template-based copy from catalog fields |
| Cut-out | image | RGBA product | 0 (local) | use the original image inside a rounded frame |
| Scenes | vision.scene_prompt + brand | 2 backgrounds | 1–2 image | brand-colour gradient |
| Compose | everything above | PNGs | 0 | — |
| Write | `CampaignResult` | files | 0 | — |

**Note: the reference photos are lifestyle shots, not white studio shots** (found in Phase 2; e.g. black headphones on a bright-yellow background). Colours taken from the raw photo therefore describe the *background*, not the product. Hence the colour fallback order above: the vision model's product colours (Phase 3) and the background-removed cut-out (Phase 5) come before the raw photo. This also makes background-removal quality more important than first assumed (ADR-001).

**Budget:** 2 LLM calls per product → 10 products = 20 calls. Measured limits: Gemini free tier = **20 requests per day per model**, so vision and copy run on two models (10 calls each); OpenRouter backup = 50 free requests per day per account (ADR-003). Cached results cost 0 on reruns (measured: a 2-product rerun went from 204 s to 7 s with 0 requests).

**Why one copy call returns everything** (both variants, carousel, captions): it keeps the call count at 2 and keeps the copy consistent across formats. The cost is a larger JSON response, which is handled by schema validation + one repair retry.

## 6. Prompt strategy

Templates live in `config/prompt_templates/` as plain text files with placeholders, and are **versioned in the filename** (`copy_v2.txt`; old versions are kept for history). The version used is written to `metadata.json`, so a change in output quality can be traced to the prompt change that caused it.

```
prompt_templates/
├── vision_v1.txt              system + user prompt for VisionAnalysis
├── copy_v1.txt / copy_v2.txt  system + user prompt for the ad copy (v2 is active)
├── brand_pdf_v1.txt           PDF text → BrandProfile
└── languages/
    ├── en.md                  style notes, banned phrases, platform norms
    ├── ar.md                  register (ADR-006), banned phrases, example phrasing
    ├── fr.md · es.md · de.md
```

Prompt design principles (from spec §8):
1. **Facts in, facts out:** the prompt contains the features, the USP, the price, **and the vision findings**; it requires each headline to cite at least one concrete fact.
2. **Native writing:** "Write directly in {language} for {target_market}. Do not translate from English."
3. **Banned phrase list** injected per language, plus a post-check that flags any banned phrase that still appears (it is logged as a quality warning, the output is not rejected).
4. **Brand voice first:** the voice and tone come from `BrandProfile`, not from generic "make it catchy" instructions.
5. **JSON only:** response format = the `AdCopy` JSON schema; temperature is set separately for vision (low) and copy (moderate).

## 7. Visual composition (`visual_engine.py`)

**Formats:** feed `1080×1080`, story `1080×1920`, carousel 3–5 × `1080×1080`, Instagram mockup.

**Steps per creative:**
1. Background = AI scene (or gradient), resized/cropped to the format.
2. Product cut-out scaled to ~55–65% of the safe area, with a soft shadow for depth.
3. Text block (headline, hook, CTA button) in a reserved zone that never overlaps the product's bounding box.
4. Brand colours: primary for overlays, accent for the CTA button; the text colour is picked by contrast check (WCAG ≥ 4.5:1) against the actual pixels behind it.
5. Logo: from `logo_url` (a link, or a file path from the project root) if available, otherwise a text wordmark of the brand name. On the reading-start side (right for Arabic). A logo that can't be loaded → the wordmark, plus a fallback note.

**Text fitting:** shrink the font size until the text fits the box in ≤ N lines, then wrap by words. This handles long German words and long Arabic phrases.

**Arabic / RTL (FR-10):** `arabic-reshaper` connects the letters, and `python-bidi` sets the visual order; the text is right-aligned and the layout is mirrored (text block on the right). This works on Windows without system libraries (Pillow's `raqm` backend often isn't available there). The line direction is **passed in from the ad's language** (`base_dir`): left alone, the bidi library guesses it from the first letter, and an Arabic line starting with a Latin word ("1680D المقاوم…", live run #23) came out left-to-right.

**Fonts:** a Latin sans + an Arabic font with OFL license, bundled in `assets/fonts/` so the output looks the same on every machine. Candidates: *Inter / Montserrat* + *Cairo / IBM Plex Sans Arabic* (chosen in Phase 5).

**Carousel (FR-9):** card 1 = hook on the hero scene; cards 2..N-1 = one feature each (from the copy's carousel cards) with a product crop; last card = CTA. All cards share a background family and progress dots.

**Dashboard (FR-13, Phase 7):** `app.py` only draws; every decision is in `core/dashboard.py`, and ads are made by the same `pipeline.run_product` as the CLI (with `write=False`: nothing is written until the user presses Save).
- Streamlit reruns the script on every click, so `st.session_state` holds what must survive: the variants per product (ADR-004, last 5), the user's brand edits per product (merged, so an edit is never lost on a later regenerate), and the brand-PDF result **per brand** (so it can't leak onto another brand's product).
- The brand form's widget keys include the product ID and a hash of the brand shown, and the page reruns after each generation. Otherwise stale form values (filled before the vision step) would override the product colours, or one product's language would carry over to the next.
- Copy is shown in `<div dir="rtl|ltr">` blocks, HTML-escaped because the text comes from an LLM.
- The `.zip` and the output folder are built by one function (`output_writer.campaign_files`), so they always hold the same files.
- The brand PDF is read with prompt `brand_pdf_v1` (schema `BrandPdfDraft`, validated field by field); hex codes written in the PDF beat the LLM's colours.
- The product photo is shown **side by side** with feed A and B (the brief's live preview); the photo options are the catalog photo, an AI-generated photo, an upload, or a link (a link that fails is reported).
- **Generate / New angle are locked while an ad is being made:** the click sets `state.job` in a button callback, which Streamlit runs *before* redrawing, so in that run the buttons are drawn disabled; a `finally` always unlocks them.
- **Rate limit:** at most 5 new AI ads per 10 minutes (`dashboard.RateLimiter`, a sliding window shared by every tab, because they all spend one API key). Only ads that made a live AI request count; with AI off there is no limit. In memory: a quota guard, not user accounts.
- Tested with Streamlit's `AppTest` (no browser) and a fake LLM.

**Mockup (FR-15, `core/mockup.py`):** feed ad A pasted 1:1 into a drawn Instagram post: avatar (the logo, or the brand's initials on its primary colour) with the story ring, a handle made from the brand name, *Sponsored*, the *Shop now* bar in the brand colour, the action icons (redrawn from Instagram's 24-unit outlines), likes, and the caption cut after **two lines** with *… more*, as Instagram does, which is why the hook must come first. Arabic ads are mirrored and use Arabic interface words. Saved as `mockup_instagram.png`, in the `.zip`, and shown in the dashboard's *Instagram preview* tab.

## 8. Error handling & resilience (NFR-2)

| Failure | Detection | Response |
|---|---|---|
| 5xx / 429 / connection reset | HTTP status, transport error | backoff 3 s → 6 s (max 3 tries); for 429, wait for `Retry-After` (clamped to 0–60 s) |
| Timeout | no reply within `LLM_TIMEOUT_S` | move to the next model at once (an overloaded model tends to time out again; found in the Phase 3 benchmark) |
| Free model unavailable | 404 / provider error | next model in the fallback list (OpenRouter `models` array) |
| Invalid JSON | Pydantic validation | 1 repair retry with the error message included in the prompt |
| Daily cap reached | 429 + quota message (Gemini: `PerDay` quota ID or a long `retryDelay`; OpenRouter: `free-models-per-day`) | skip that model (Gemini) or the whole provider (OpenRouter's cap is per account) for the rest of the session, with no waiting; when nothing is left, finish from cache/templates and report it |
| Shared free pool busy | OpenRouter 429 with `upstream` in the error | next model at once (waiting did not help in the live test) |
| Rejected API key | 401, or 400 mentioning the API key | skip every model on that provider (the key, not the model, is wrong) |
| Error inside an HTTP 200 | `error` object and no `choices` (OpenRouter) | treated like the HTTP status it contains |
| Image service down | timeout / non-image response | gradient background |
| Bad catalog row / missing image | loader / image_source | skip that product, continue the batch |

Every fallback is appended to `CampaignResult.fallbacks` → `metadata.json`, and shown in the CLI summary and the dashboard. Nothing degrades silently. API keys are masked (`***`) in every error message, because error text ends up in `metadata.json`.

## 9. Caching (`cache.py`)

`CachedLLM` wraps the LLM client with the same `complete_json` interface, so the vision and copy steps don't know whether an answer is live or replayed.

Key = SHA-256 of *(step, model ID, schema, temperature, the full system + user prompt text, image pixels)*. The prompt text already contains the product row, the brand profile, the vision findings, the prompt version and the variation number, so a change to any of them is automatically a miss (this is simpler and stricter than listing those fields one by one). Entries are JSON under `.cache/llm/<step>/` (git-ignored), written atomically.

- **Only successes are cached:** a failed call (quota out) is never stored, so the next run tries the AI again.
- A corrupt entry, or one that no longer fits the schema, is a miss, never a crash.
- `--refresh` ignores stored answers; `--offline` makes no AI calls but still replays stored answers.
- The dashboard's "Regenerate" adds a variation number to the prompt, so it is naturally a miss for the copy step only (vision is replayed).
- Scene images have their own cache (`.cache/scenes/`, by prompt + size + seed).

## 10. Configuration (`.env`)

```
# Primary LLM provider (ADR-003)
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=             # Gemini key from Google AI Studio
VISION_MODEL=            # Gemini Flash model ID (confirmed in Phase 3)
COPY_MODEL=              # same as VISION_MODEL unless the benchmark says otherwise

# Fallback provider (optional)
FALLBACK_BASE_URL=https://openrouter.ai/api/v1
FALLBACK_API_KEY=
FALLBACK_MODELS=         # comma-separated :free model IDs
LLM_FALLBACK_MODELS=     # other models on the PRIMARY key, tried first (e.g. gemini-3.8-flash)

IMAGE_PROVIDER=pollinations   # ADR-007
LLM_TIMEOUT_S=60
MAX_REQUESTS_PER_MIN=10       # stays under every free tier's per-minute cap
REFERENCE_INPUT_PRICE_PER_MTOK=    # paid price for the cost estimate (FR-14);
REFERENCE_OUTPUT_PRICE_PER_MTOK=   # empty = Gemini 3.5 Flash list price: 1.50 / 9.00
```

**Cost tracker (FR-14):** both providers return token usage on every response. Free models really cost $0, so the dashboard shows both the **actual cost ($0)** and an **estimated cost at a reference paid model's price**, which shows what the system would cost at scale. Input and output tokens are priced separately, and the estimate is applied in the pipeline, so an answer replayed from the cache is priced at today's reference price (it used to keep the estimate of the day it was made). The CLI prints tokens and cost per product; the 10 demo ads come to about $0.18 at the paid price.

## 11. Dependencies

| Package | Why |
|---|---|
| `pandas`, `openpyxl` | read the xlsx/csv catalog |
| `pydantic` | typed contracts + LLM output validation |
| `httpx` | HTTP with timeouts (the retry/backoff policy is our own ~40 lines in `llm_client.py`, so it is testable and needs no extra library) |
| `python-dotenv` | `.env` config |
| `Pillow` | composition, text rendering, mockups |
| `rembg` (+ `onnxruntime`) | local background removal |
| `arabic-reshaper`, `python-bidi` | Arabic shaping + RTL ordering |
| `pypdf` | brand-PDF text extraction |
| `streamlit` | dashboard |
| `pytest` | tests |

The OpenRouter API is OpenAI-compatible, but we call it with `httpx` directly: one small dependency, and full control over retries, the fallback list, and reading usage data.

## 12. Testing approach

- **Unit (no network):** loader on the real catalog; brand merge order; schema validation of sample LLM responses; text fitting; RTL shaping; output folder naming.
- **Offline end-to-end:** the pipeline with a **fake LLM client + gradient backgrounds**, which proves the whole flow (including fallbacks) works without any API.
- **Live smoke test:** 1 English + 1 Arabic product against the real APIs (also used as the ADR-003 benchmark).

## 13. Build phases (from the brief's order)

| Phase | Deliverable | Verifies |
|---|---|---|
| 1 | `schemas.py`, `excel_loader.py`, `settings.py` | FR-1 |
| 2 | `brand_manager.py` (+ PDF) | FR-4 |
| 3 | `llm_client.py`, `vision_agent.py`, model benchmark → ADR-003 | FR-3, NFR-2/3/4 |
| 4 | prompt templates, `copy_engine.py` | FR-5/6/7 |
| 5 | `image_source.py`, `scene_generator.py`, `visual_engine.py`, fonts | FR-2/8/9/10 |
| 6 | `output_writer.py`, `cache.py`, `pipeline.py`, `main.py` → run the 10 products | FR-11/12, §9 |
| 7 | `app.py` dashboard | FR-13 |
| 8 | Instagram mockup, cost fix, logo, AI-generated photo, rate limit + button lock, README | FR-13/14/15 |
| 9 | README, final run, video | §9 |

One commit (at least) per phase; each phase updates `ai-usage-log.md`.

## 14. Design decisions (owner: Omar) — all accepted 2026-11-05

| ID | Decision |
|---|---|
| **DD-1** | **Pollinations** (FLUX, no key/signup) as the primary scene generator; gradient as the guaranteed fallback; a second provider only if needed → [ADR-007](decisions/007-image-generation-provider.md) |
| **DD-2** | **One LLM** (Gemini Flash) for vision and copy, with two separate settings so they can be split later → [ADR-003](decisions/003-llm-model-selection.md) |
| **DD-3** | **Extra modules** beyond the brief's list (`schemas`, `llm_client`, `pipeline`, `cache`, …) so that shared code is written once; the brief's 5 core files keep their roles |
| **DD-4** | Instagram mockup saved as **PNG** (in `output/` and the zip) and also shown in the dashboard |
