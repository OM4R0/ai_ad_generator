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
