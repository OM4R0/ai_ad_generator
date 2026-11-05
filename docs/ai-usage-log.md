# AI Usage Log

How I used an AI coding assistant (Claude Code) on this project: what I asked for, what it proposed, and what **I** decided. The assistant drafts; I review, decide, and own every decision.

**Format:** each entry = context → what I asked → what the AI proposed → my decision / correction → why.

---

## Session 1 — 2026-11-05 — Understanding & planning (no code)

### 1.1 Understanding the brief
- **Asked:** Explain the task from a business point of view before doing anything. I explicitly told the AI *not* to write code yet.
- **AI proposed:** A business summary (why brands need many ad variants, A/B testing value) and a set of open questions: image source, LLM provider, language priority.
- **My decision:** English first, then Arabic. OpenRouter with free models (budget $0).

### 1.2 How product images should be produced
- **Context:** I assumed that for an ad campaign the AI should generate the image.
- **AI proposed:** Three options. It argued against letting the AI draw the product itself, because it hallucinates the product's shape/details, which is unacceptable in e-commerce, where the customer must see the real item. It recommended a hybrid: real photo → remove background → AI-generated scene → composite with brand text.
- **My decision:** Hybrid accepted (ADR-001). I changed my initial assumption once I understood the risk of hallucinating the product itself.

### 1.3 Repo inspection
- **AI found:** (1) All skeleton files were empty (0 bytes). (2) The catalog and images were inside a git-ignored folder (`Omar-Hiring/`), so a reviewer cloning the repo would get **no data**. (3) Each of the 30 products has its own brand, and languages are mixed (EN 10, AR 11, FR 4, ES 3, DE 2).
- **My decision:** Move the catalog + images to the repo root. Treat the brand profile as **per product** (derived from the catalog row, overridable).

### 1.4 Process: Spec-Driven Development
- **Asked:** Work in SDD so that every part is documented and I can defend it in the interview.
- **AI proposed:** `docs/spec.md` (what), `docs/design.md` (how), `docs/decisions/` (ADRs: decision + rejected alternatives), this log, and one commit per phase.
- **My decision:** Accepted. Start with `spec.md` only and review it before moving on.

### 1.5 Reviewing the spec and the six decisions
- **Asked:** The AI to walk me through the spec section by section in Arabic until I understood every part, then to explain each open decision with its options.
- **Questions I pushed back on:**
  - *Why more than two languages if the brief only asks for two?* The AI admitted it isn't required. The real reason: the catalog assigns FR/ES/DE to 9 products, and supporting them costs almost nothing. The risk: I can't verify French/Spanish/German quality.
  - *What exactly does "overwrite" mean (D-4)?* It was clarified with a concrete file-path example.
  - *What is D-5 actually asking?* The brief says "at least 10 selected products", so we choose which ones.
- **My decisions:**
  - Support all languages, **focus quality on EN + AR** (ADR-005).
  - ~~Try all 30 products~~. **Revised:** start with **10 products** (the brief's minimum). 30 is too much to start with given the free limits, and the pipeline is identical for 10 or 30 (ADR-005).
  - Moved the catalog files into `data/` to keep the repo root tidy.
  - Accepted D-1, D-2, D-4, D-6 as recommended. D-3 is deferred to a live model benchmark (ADR-003).
- **AI research:** Checked OpenRouter's free-tier limits (third-party sources: 20/min, 50/day under $10 of credits). This led to a new design constraint: **at most 2 LLM calls per product + caching** (NFR-7).

### 1.6 Design document
- **Asked:** "How many models do we need? Isn't it one for text and one for images?"
- **AI answer:** Two external models + one local one. (1) One **multimodal LLM** does both vision and copy (configurable as two separate models). (2) An **image generator** paints only the background scene. (3) A small **local background remover** (no API). Image-generator calls don't count against the OpenRouter cap.
- **AI research:** Free image-generation options (late 2026): Pollinations (FLUX, no key, rate-limited), Cloudflare Workers AI (free account), Hugging Face (tiny free credit). No free option is production-grade, which is another reason for the gradient fallback.
- **AI drafted:** `docs/design.md` (pipeline, data contracts, call budget, prompt strategy, layout/RTL approach, error handling, caching, dependencies, tests, phases).
- **My decision:** Asked for each design question to be re-explained with a concrete example (the Oud perfume), then accepted DD-1..DD-4.

### 1.7 Switching the LLM provider to Gemini
- **Context:** I have a Gemini API key.
- **My decision:** Use Gemini as the primary provider. Start with **one model** for vision + copy; move to two models only if the quality isn't good enough.
- **AI proposal (accepted):** Keep OpenRouter as a fallback, and use Gemini's **OpenAI-compatible endpoint** so a single client serves both providers and switching is config-only. The AI also flagged that Gemini's free-tier limits are reported inconsistently online (from ~1,500/day down to ~20/day), so they must be checked on my own key in Phase 3 (ADR-003, ADR-007).

---

## Session 1 (cont.) — Phase 1: data contracts + catalog loader

- **Built:** `core/schemas.py` (all data contracts from design §4), `config/settings.py` (env-driven config), `core/excel_loader.py` (FR-1), plus 18 tests. A project virtual env (`.venv`), `requirements.txt`, `.env.example`.
- **Bug found by the tests, not by eye:** in the `.xlsx`, the `Image URL` cells **display "View Photo 🔗"**, and the real URL is stored as the cell's *hyperlink*. pandas reads only the visible text, so every product had a broken URL. The CSV has the real URLs. **Fix:** read the hyperlink targets with openpyxl; a test now asserts that xlsx and csv give identical products.
- **Why this matters:** a hidden data-quality issue in the provided input. Without the xlsx-vs-csv test it would only have shown up later as "image download failed".
- **Design choices in the loader:** bad rows are skipped and reported, never fatal; the CSV is the fallback if the xlsx can't be read; non-URL links become `None` (the local image still works).

### Code review of Phase 1
- **Asked:** The AI to explain its weak points honestly. It listed 3; I asked it to fix them **and look for more**.
- **Found and fixed 7 issues**, each with a regression test (18 → 35 tests):
  1. The fallback caught *every* exception and could hide real bugs → now only file-read errors.
  2. Image paths assumed the catalog lived in `data/` → now resolved against the project root.
  3. A duplicate ID was reported as "row 0" → now the real row + where the ID was first seen.
  4. An ID of `inf` **crashed the whole batch** (`OverflowError` wasn't caught), which contradicts the spec's "a bad row never stops the batch" → now a row error.
  5. Blank rows were never dropped (`dropna` does nothing when empty cells are `""`) and would be reported as errors → now ignored, with row numbers still correct.
  6. An empty value in `.env` (e.g. `LLM_BASE_URL=`) overrode the default with `""` → empty now means "use the default".
  7. The contract accepted two A/B variants with the same angle and empty bullets; it trusted the LLM's text direction → now rejected / derived from the language.
- **Lesson:** "tests pass" ≠ "no bugs". Issues 4 and 5 only showed up when I deliberately tested odd inputs.
- **Also asked for:** `docs/interview-prep.md`, a study file with "what I must understand" + expected questions per phase, updated after each phase.

---

## Phase 2 — Brand manager (test-driven)

- **Process:** TDD. 21 tests were written **first** (they failed: the module was empty), then the implementation made them pass. Full suite: 56 tests.
- **Built:** `core/brand_manager.py` with the 4-source resolution order (catalog → config file → PDF → manual edits), `BrandPatch` (a partial profile where blank values never erase), JSON + YAML config loading matched by the `brand_name` inside the file, PDF text extraction, hex-code extraction, image dominant colours, and a default palette.
- **Design decisions made here:**
  - **Hex codes written in the PDF beat the LLM's colours.** An explicit `#0B1F3A` is a fact; the LLM's colour is a guess.
  - **The LLM is injected** into `patch_from_pdf` (a plain function argument), so the brand module doesn't depend on the LLM client and is testable with a fake.
  - **LLM failure → text-only result + warning**, never a crash (the colours still come through).
  - Config files are matched by the `brand_name` **inside** the file, not by file name, so the file can be named anything.
- **Finding:** running on real photos showed the reference images are **lifestyle shots** (black headphones on a yellow background). Raw-photo colours describe the background, not the product. **Decision:** don't patch it now; in the pipeline the colour order becomes vision colours (Phase 3) → cut-out (Phase 5) → raw photo → default. Recorded in design.md §5.
- **Edge-case review (TDD), requested by me:** I asked the AI to check every special case, test-first, and document them. It listed 15 edge cases and wrote 29 tests **before** touching the code: 18 failed, revealing **15 real problems**, and 11 passed (already handled, kept as regression guards). The most important ones:
  - **E2:** one invalid field in the LLM's reply (e.g. colour `"gold"`) discarded the *whole* reply → now validated field by field.
  - **E11:** uploading another brand's PDF renamed the product's brand (an oud ad under the wrong company name) → a PDF can't rename the brand; only a manual edit can.
  - **E12:** transparent pixels of background-removed cut-outs counted as **black**. This would have broken Phase 5's colour extraction before it was even built.
  - **E4/E5:** Windows-specific and YAML-specific traps (BOM, `#` = comment) that silently ignored a config.
  - **E14:** text colour chosen by a crude brightness rule → now WCAG contrast (mid-grey went from 3.9 to 4.8).
  - **E15:** an invalid brand config stopped the product → now skipped + recorded in a new `BrandProfile.warnings` field.
- Full list with before/after and test names: `docs/edge-cases.md`. Tests: 56 → 85.
- **Samples added (Phase 2):** `config/brands/al-andalus-parfums.json` (the brief's example brand), `data/sample_brand_guidelines.pdf` (generated by `scripts/make_sample_brand_pdf.py`, no extra dependency).

---

## Session 2 — Phase 3: LLM client + vision (deadline: Saturday night)

- **Context:** I told the AI the deadline (Saturday night) and asked it to stick strictly to the brief from now on. It proposed a day-by-day plan: deeper reviews are postponed until the end-to-end pipeline works.
- **Built test-first:** `core/llm_client.py` (one OpenAI-compatible client for Gemini + OpenRouter: retries, Retry-After, JSON repair, fallback models, rate limiting, token/cost accounting), `core/prompts.py` (prompt files with versions), `config/prompt_templates/vision_v1.txt`, `core/vision_agent.py` (+ local fallback). All tests use a fake HTTP server, so they spend no real requests.
- **Live benchmark on my key** (ADR-003): `gemini-3.8-flash` was overloaded (timeouts, 503), `gemini-2.5-flash` returned 404 (retired for new users), and **`gemini-3.5-flash` worked**: valid JSON first try, 16–21 s, and it reported the headphones as **black**, solving the "yellow background" problem from Phase 2.
- **The live run changed the code:** timeouts now skip to the next model (instead of 3 × 60 s), and a new `LLM_FALLBACK_MODELS` setting tries other Gemini models on the same key.
- **Also found:** the Windows console can't print Arabic (cp1252), so the CLI must force UTF-8 output. Arabic products have their features written in Arabic in the catalog, which helps native Arabic copy.
