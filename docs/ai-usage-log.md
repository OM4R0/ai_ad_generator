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
