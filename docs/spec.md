# Spec — AI Ad Generation Workflow

> **Status:** v0.4 — all decisions D-1..D-6 accepted by owner; Phase 8 additions (2026-11-07) marked below
> **Owner:** Omar
> **Source brief:** the hiring task's brief (kept out of this repository)
> **Process:** Spec-Driven Development. This spec is the contract. Code is written *against* it, and any change in scope is made here first, then in code.

---

## 1. Problem

E-commerce brands need a steady stream of ad variants (per product × per platform × per language × per angle) to find what converts. Producing each one by hand (copywriter + designer + translator) is slow and expensive, so most small and mid-size brands test far fewer variants than they should.

**This system** takes a product catalog plus brand guidelines and produces complete, publish-ready ad campaigns: copy, visuals, and platform captions. It runs with no manual steps between input and output.

## 2. Users & core scenarios

| User | Scenario |
|---|---|
| **Marketer (dashboard)** | Picks a product, reviews/edits the brand profile, generates, previews the ads in a feed mockup, regenerates if unhappy, downloads a `.zip`. |
| **Operator (CLI)** | Runs a batch over N products in one command and gets an organized `output/` folder. |
| **Brand owner** | Uploads a brand-guidelines PDF instead of filling in a form. |

## 3. Scope

### In scope
- Catalog ingestion (`.xlsx` primary, `.csv` fallback)
- Product image sourcing: reference image, user upload/URL, or AI-generated scene (see §6, D-1)
- Multimodal product analysis (image + catalog features)
- Ad copy, A/B angles, platform captions + hashtags
- Visual creatives: feed `1080×1080`, story `1080×1920`, carousel (3–5 cards)
- Brand profile layer: catalog-derived defaults, JSON/YAML config, PDF extraction
- Streamlit dashboard; batch CLI
- Bonus: carousel, A/B variants, cost tracker, social mockup

### Out of scope
- Publishing to ad platforms (Meta/TikTok APIs), scheduling, analytics
- Video ads
- User accounts / multi-tenant storage
- Paid-only model dependencies (see NFR-1)

## 4. Inputs

### 4.1 Product catalog — `data/products_master.xlsx` (sheet `Physical Products`)
30 rows, 17 columns, no missing values. Verified 2026-11-05.

| Column | Use |
|---|---|
| `#` | Stable product ID |
| `Product Name`, `Brand Name`, `Category / Niche` | Identity, output folder names |
| `Short Description`, `Feature 1..5`, `USP` | Copy source material and carousel cards |
| `Target Audience`, `Price Range (USD)` | Copy targeting and positioning |
| `Tone / Style` | Default brand voice |
| `Suggested Language` | Output language (English 10, Arabic 11, French 4, Spanish 3, German 2) |
| `Image URL`, `Local Image Path` | Reference image (local first, URL fallback) |

**Key observation:** every product has its **own brand** (30 brands). The brand profile is therefore **per product**, not global.

### 4.2 Brand profile
```json
{
  "brand_name": "Al-Andalus Parfums",
  "brand_voice": "Royal, mysterious, poetic, premium luxury",
  "brand_colors": ["#1A1A1A", "#D4AF37", "#FFFFFF"],
  "target_market": "GCC & Middle East Luxury Shoppers",
  "language": "Arabic",
  "logo_url": "https://..."
}
```
**Resolution order (later overrides earlier):**
1. Defaults derived from the catalog row (name, voice ← `Tone / Style`, language, audience)
2. Structured config file (`config/brands/*.json|yaml`), matched by `brand_name`
3. Values extracted by an LLM from an uploaded brand-guidelines PDF
4. Manual edits in the dashboard

Missing fields are filled in deterministically (e.g. colors from the dominant colors of the product image, a text logo from the brand name). Generation never fails because a brand field is missing.

### 4.3 Product image
One of: local reference file · reference URL · user upload / pasted URL in the dashboard · an **AI-generated product photo** (dashboard option, added in Phase 8; always flagged for a human check, see ADR-001).

## 5. Outputs

```
output/{Brand_Name}/{Product_Name}/
├── copy.json
├── creative_feed.png          1080×1080
├── creative_story.png         1080×1920
├── carousel_slide_1..N.png    1080×1080, N = 3–5
├── mockup_instagram.png       (bonus)
└── metadata.json
```
Folder names are slugified (filesystem-safe, ASCII), and the original names are kept in `metadata.json`.

### 5.1 `copy.json` (shape — finalized in design.md)
```json
{
  "language": "en", "direction": "ltr",
  "variants": [
    { "angle": "pain_point", "headline": "", "hook": "", "primary_text": "",
      "bullets": ["", "", ""], "cta": "" },
    { "angle": "emotional", "...": "..." }
  ],
  "carousel": [ { "title": "", "body": "" } ],
  "captions": {
    "instagram": { "text": "", "hashtags": [] },
    "tiktok":    { "text": "", "hashtags": [] },
    "facebook":  { "text": "", "hashtags": [] },
    "linkedin":  { "text": "", "hashtags": [] }
  }
}
```

### 5.2 `metadata.json`
Product ID, source row, the brand profile actually used, image source, the vision analysis result, the models used, prompt template versions, per-call token usage and estimated cost, timings, fallbacks triggered, and a timestamp.

## 6. Functional requirements

Each requirement has acceptance criteria (AC) that we can test.

| ID | Requirement | Acceptance criteria |
|---|---|---|
| **FR-1** | Load catalog | Loads all 30 rows from `.xlsx` into typed product objects; falls back to `.csv`; a bad row is reported and skipped and does not crash the batch. |
| **FR-2** | Resolve product image | Local path → URL → user upload; the result is always a valid RGB image or an explicit, logged error. |
| **FR-3** | Vision analysis | Returns structured attributes (product type, dominant colors, material/finish, mood, visual selling points). Copy and scene prompts must **use** these (can be checked in `metadata.json`). |
| **FR-4** | Brand profile | Supports the 4 sources in §4.2; PDF extraction returns valid JSON in the brand schema. |
| **FR-5** | Ad copy | Headline, hook, primary text, 3 bullets, CTA, written natively in the target language (not translated from English), and obeying the brand voice. |
| **FR-6** | A/B angles | 2 distinct angles per product (default: pain-point vs. emotional/aspirational). |
| **FR-7** | Captions | One caption each for Instagram, TikTok, Facebook, and LinkedIn, following the conventions of that platform (length, emoji density, hashtag count). |
| **FR-8** | Creatives | Feed and story PNGs at exact sizes; the **real product image is visibly present**; brand colors are used; text never overflows or overlaps the product. |
| **FR-9** | Carousel | 3–5 cards: hook → features → CTA, visually consistent. |
| **FR-10** | RTL | Arabic text in images is shaped (connected letters) and right-aligned; the dashboard renders Arabic RTL. |
| **FR-11** | Output | Exact folder structure as in §5; reruns overwrite cleanly or version (D-4). |
| **FR-12** | CLI batch | One command generates N products with no manual steps; prints a summary (success/fail/fallbacks/cost). |
| **FR-13** | Dashboard | Product selector, image source choice (incl. an AI-generated photo), brand editor + PDF upload, side-by-side preview (the product photo next to the creatives), carousel preview, regenerate, `.zip` export. Generate is locked while an ad is being made. |
| **FR-14** | Cost tracker | Shows tokens and estimated USD per product. |
| **FR-15** | Social mockup | The creative is rendered inside a realistic Instagram feed frame. |

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| **NFR-1 Zero cost** | Must run end-to-end on free tiers (Gemini free tier, with OpenRouter `:free` models as the fallback + free image services + local libraries). Paid models are optional and enabled only through config. |
| **NFR-2 Resilience** | Every external call has a timeout, retries, and a defined fallback. **A failing AI service downgrades quality but never stops the pipeline.** Every fallback is recorded in `metadata.json`. |
| **NFR-3 Swappable providers** | Model names and endpoints are set in `.env`/config, never hardcoded, because free model availability changes. |
| **NFR-4 Structured LLM output** | All LLM responses are JSON validated against a schema, with retry on invalid output. |
| **NFR-5 Code quality** | Type hints, modular `core/`, no global state, docstrings on public functions, clear errors. |
| **NFR-6 Reproducibility** | `pip install -r requirements.txt` + `.env` is enough to run; no secrets in the repo. |
| **NFR-7 Rate limits** | The batch respects free-tier rate limits (throttling + backoff). **At most 2 LLM calls per product** (vision + copy), and results are cached so reruns don't spend requests (ADR-003). The dashboard allows at most **5 new AI ads per 10 minutes** (cached answers don't count). |

## 8. Copy quality bar (anti-cliché rules)

This bar is enforced in the prompts and checked when we review the outputs.

- **Banned openers/phrases** (per language list): e.g. "Elevate your…", "Unleash…", "Game-changer", "Look no further", "In today's fast-paced world", "Discover the…"
- Every headline must reference **a concrete fact** from the catalog (spec, material, number, USP), not just an adjective.
- Arabic copy is written for the target market (Modern Standard Arabic with a Gulf-friendly register by default) and never as a literal translation.
- Match the length limits of each platform; the hook must work in the first ~125 characters (the Instagram truncation point).

## 9. Verification (definition of done)

- [x] ≥ 10 products generated end-to-end via the CLI with no manual steps (10/10, live, 2026-11-07)
- [x] ≥ 2 languages (English + Arabic minimum; target: all 5): EN, AR, FR in the demo; ES, DE supported
- [x] ≥ 2 formats (single feed/story + carousel): feed A/B, story, carousel, Instagram mockup
- [x] Product image is visibly integrated in every creative
- [x] Dashboard: all FR-13 items, tested with Streamlit's `AppTest`
- [x] README with setup + run commands · [ ] walkthrough video 3–5 min

## 10. Constraints & assumptions

- Budget: **$0**. LLM access via the Gemini free tier (primary) and OpenRouter free models (fallback). Free-tier limits are unstable and to be verified on our keys in Phase 3 (ADR-003). At 2 calls per product, the 10-product demo needs 20 calls.
- Priority order: **English first, then Arabic**, then FR/ES/DE (we get the other languages almost for free once the pipeline is generic).
- Platform: developed on Windows / Python 3.13.

## 11. Decisions (owner: Omar)

Full reasoning and rejected alternatives are in [`decisions/`](decisions/README.md).

| ID | Decision | ADR | Status |
|---|---|---|---|
| **D-1** | Hybrid visuals: real photo, background removed, AI-generated scene, composited; gradient fallback | [001](decisions/001-hybrid-product-visuals.md) | Accepted |
| **D-2** | Streamlit dashboard | [002](decisions/002-streamlit-dashboard.md) | Accepted |
| **D-3** | Gemini direct as the primary LLM (one multimodal model), OpenRouter free models as the fallback; model ID confirmed by benchmark | [003](decisions/003-llm-model-selection.md) | Accepted |
| **D-4** | CLI overwrites; dashboard keeps variants in its session | [004](decisions/004-rerun-behaviour.md) | Accepted |
| **D-5** | Support all catalog languages, focus quality on EN + AR; start with 10 products, scale to 30 later if the limits allow | [005](decisions/005-languages-and-demo-scope.md) | Accepted |
| **D-6** | Arabic register: MSA with a light Gulf flavour | [006](decisions/006-arabic-register.md) | Accepted |
