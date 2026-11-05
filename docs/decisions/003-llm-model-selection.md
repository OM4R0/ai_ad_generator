# ADR-003 — LLM selection (text + vision)

- **Status:** Proposed — final choice in Phase 3
- **Spec ref:** D-3, NFR-1, NFR-3, NFR-7

## Context
Budget is $0, so we use OpenRouter `:free` models. Free models rotate and get rate-limited at peak hours. Published limits as of late 2026 (third-party sources, to be verified against our key with `GET /api/v1/key`):
- **20 requests/minute** on free models
- **50 requests/day** on accounts with under $10 of lifetime credit purchases; **1,000/day** at $10 or more (reported as a permanent unlock)

## Decision (process, not model)
1. Benchmark 2–3 free vision-capable models on the **same 2 products** (1 English, 1 Arabic).
2. Score them on: JSON validity, Arabic naturalness, use of concrete facts, cliché count, latency.
3. Pick a primary model plus an ordered fallback list (OpenRouter `models` array).
4. Record the scores here and set the status to Accepted.

Model IDs live in `.env`/config only (NFR-3).

## Consequences
- The daily cap shapes the design: **at most 2 LLM calls per product** (1 vision + 1 copy call that returns all variants, captions, and the carousel in one JSON), plus a **result cache** so reruns and dashboard reloads don't spend requests.
- 30 products × 2 = 60 calls, which is over 50/day. Options, owner decides: spread the run over 2 days, or make a one-time $10 credit purchase (unlocks 1,000/day; the credits are not spent on free models).
