# ADR-003 — LLM provider and model selection (text + vision)

- **Status:** Accepted (provider) — 2026-11-05 · Model ID confirmed by benchmark in Phase 3
- **Spec ref:** D-3, NFR-1, NFR-3, NFR-7 · design DD-2

## Context
Budget is $0. The owner has a **Google Gemini API key** (Google AI Studio free tier). Free-tier limits change without notice and third-party sources disagree; they are to be confirmed on our own key/dashboard:
- **Gemini (AI Studio, free):** Flash / Flash-Lite models free; reported limits range from ~15 req/min and ~1,500 req/day down to ~20 req/day after a Dec 2025 cut. Limits are per Google Cloud project and reset at midnight Pacific time.
- **OpenRouter `:free`:** 20 req/min; 50 req/day under $10 of lifetime credit purchases, 1,000/day at $10 or more.

## Decision
1. **Primary provider: Gemini direct**, using a **single multimodal Flash model** for both vision and copy (design DD-2).
2. **Fallback provider: OpenRouter `:free` models**, used only if Gemini fails or its quota runs out.
3. **One client for both.** Gemini and OpenRouter both expose an **OpenAI-compatible chat endpoint**, so `llm_client.py` speaks one protocol and the provider is just `base_url + api_key + model` in `.env`. Switching provider = changing config, not code.
4. `VISION_MODEL` and `COPY_MODEL` are separate settings. If the single model is weak at Arabic copy, we switch `COPY_MODEL` to a second model (owner's rule: "start with one; go to two if it isn't good enough").

## Phase 3 benchmark (vision step) — 2026-11-05
Same 2 products (headphones #13 EN, oud #10 AR) with the `vision_v1` prompt, on the owner's key:

| Model | Result | Latency | JSON valid 1st try | Product colours (not background) |
|---|---|---|---|---|
| `gemini-3.8-flash` (newest) | ❌ timeout (60 s), then HTTP 503 "overloaded" | — | — | — |
| `gemini-2.5-flash` | ❌ HTTP 404 "no longer available to new users" | — | — | — |
| **`gemini-3.5-flash`** | ✅ | 16–21 s | ✅ both | ✅ headphones `#1C1D21` (black), not the yellow background |

**Chosen:** `gemini-3.5-flash` as `VISION_MODEL` (and `COPY_MODEL`, via DD-2). `gemini-3.8-flash` and `gemini-3.5-flash-lite` are listed in `LLM_FALLBACK_MODELS` (same key). Copy quality is checked in Phase 4; if the Arabic copy isn't good enough, `COPY_MODEL` is switched on its own.

**What the benchmark changed in the code:**
- A timeout now moves on to the next model immediately (3 × 60 s retries on an overloaded model wasted 3 minutes).
- New setting `LLM_FALLBACK_MODELS`: other models on the **same** key, tried before the other provider.
- The live run proved the fallback path works: when every model failed, the local analysis kicked in and the pipeline continued.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| OpenRouter only | Lower free daily cap (50); free models rotate out without notice. |
| Google's own Gemini SDK | Locks the code to one provider; the OpenAI-compatible endpoint keeps OpenRouter as a drop-in fallback. |
| Two models from day one | More config and more failure points before we know there's a quality problem. |

## Consequences
- The daily cap still shapes the design: **at most 2 LLM calls per product** + a **result cache** (NFR-7). 10 products = 20 calls.
- Gemini's free tier may use prompts to improve Google's products. That is acceptable here (public catalog data, no personal data), and it is noted in the README.

## Quota finding (Phase 5 stress test, 2026-11-05)
The Gemini 429 body said: *"Generate content requests per day limit exceeded on Free Tier… limit: 20, model: gemini-3.6-flash… retry in 13h30m"* (`gemini-3.5-flash` is served as 3.6-flash).
- **20 requests per day per model.** 10 products × 2 calls = one model's whole day.
- **Decision:** split the steps across models (DD-2 made this a config change): `VISION_MODEL=gemini-3.5-flash-lite` (describing a photo is the easier task), `COPY_MODEL=gemini-3.5-flash` (the stronger model writes). Two separate daily quotas.
- **Code:** a daily-quota 429 now skips that model at once and for the rest of the session (no pointless retries); a per-minute 429 is waited out.
- **Operational rule:** run the 10-product batch once after the daily reset, cache the results, and record the video from the cache.

## OpenRouter backup, tested live (Phase 6, 2026-11-05)
The owner added an OpenRouter key so the backup is real, not only planned. The key is on the
free tier (**50 free requests per day per account**). Of 454 OpenRouter models, 16 are free, and only
a few both **read images** (needed by the vision step) and **return JSON** (`response_format`).

Live test with our real prompts (vision on headphones #13, Arabic copy on backpack #23), Gemini switched off:

| Model | Vision | Arabic copy | Notes |
|---|---|---|---|
| `nvidia/nemotron-3.5-super-vl:free` | ✅ black headphones, correct | ✅ valid, a little stiff ("الحقيبة تنقع") | slow: 83 s for the copy |
| `openrouter/free` (router) | ✅ | ✅ the most natural Arabic of the three | picks a random free model each time (this time `nemotron-3-nano`) |
| `google/gemma-4-31b-it:free` | ❌ HTTP 429 | ❌ HTTP 429 | not our limit: Google's shared free pool was busy (`limit_source: upstream_provider_shared_pool`) |

**Decision:** `FALLBACK_MODELS=nvidia/nemotron-3.5-super-vl:free,openrouter/free,google/gemma-4-31b-it:free`
(ordered by evidence: the two that passed come first; gemma is last because it couldn't be tested).

**What the live test changed in the code** (all test-first, `tests/test_openrouter.py`):
- **One 429, three meanings:** a daily quota (Gemini per model, OpenRouter per account) skips for the
  session; OpenRouter's busy shared pool moves to the next model at once (waiting 3 s + 6 s didn't help);
  a per-minute limit is waited out.
- **OpenRouter's daily cap is per account**, so all OpenRouter models are skipped, not just one.
- **A rejected key is a provider problem, not a model problem:** seen live, an invalid Gemini key was
  tried on all 3 Gemini models (≈18 s wasted per call with request pacing). Now the whole provider is skipped.
- **Errors inside an HTTP 200 reply** (OpenRouter does this) are classified like the HTTP status they contain.
- **Reply text as a list of parts**, or `<think>` reasoning before the JSON: handled (was a crash / a wasted repair call).
- **`served_model`** records which model really answered (a router like `openrouter/free` picks one itself), so `metadata.json` tells the truth.
- **Keys are masked** (`***`) in every error message and log line, because these end up in `metadata.json`.
- **OpenRouter alone is enough:** with no Gemini key, the pipeline runs on the backup instead of refusing.
- **`Settings.config_warnings()`**: the CLI warns at start-up if, e.g., `FALLBACK_API_KEY` is set but
  `FALLBACK_MODELS` is empty (this happened: the backup would silently never have been used).

**Live hand-over proof:** with a deliberately invalid Gemini key, the vision step moved to OpenRouter on its own and
answered correctly in 26 s. In the first live CLI run, copy found `gemini-3.5-flash` and `gemini-3.8-flash` out of
quota, skipped them without waiting, and OpenRouter wrote the ad.
