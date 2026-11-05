# ADR-007 — Image generation provider (scene backgrounds)

- **Status:** Accepted — 2026-11-05
- **Spec ref:** NFR-1, NFR-2 · design DD-1 · builds on ADR-001

## Context
Per ADR-001 the image model paints **only the background scene**, never the product. Budget is $0. Free options as of late 2026 (third-party sources, unverified):
- **Pollinations:** FLUX models over a plain HTTP GET; no key, no signup; rate-limited for anonymous use; may add watermarks in some cases.
- **Cloudflare Workers AI:** reportedly around 230 FLUX images/day; needs a free account + token.
- **Hugging Face Inference:** a very small monthly free credit.
- **Gemini image models:** reportedly not on the free tier.

None of them is production-grade.

## Decision
1. **Primary: Pollinations**, because it needs zero setup for the owner and for a reviewer cloning the repo.
2. **Guaranteed fallback: a brand-colour gradient** rendered locally. The pipeline never depends on the image service being up.
3. The provider sits behind a small interface in `scene_generator.py` (`generate(prompt, size) -> Image | None`), so a second provider (e.g. Cloudflare) can be added later **only if Pollinations proves unreliable** in Phase 5.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| Cloudflare as the primary | Requires an account + token, which adds friction for reviewers running the repo. |
| Hugging Face | Free credit is too small for repeated runs. |
| No AI scenes (gradient only) | Loses the main creative value of ADR-001. |

## Consequences
- Scene images are cached (design §9), so regenerating the copy doesn't regenerate the backgrounds.
- If a watermark appears, the composition crops/covers the corner, or we fall back to the gradient. To be checked in Phase 5.
