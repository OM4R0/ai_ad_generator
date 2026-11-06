# ADR-001 — Hybrid product visuals

- **Status:** Accepted — 2026-11-05
- **Spec ref:** D-1, FR-2, FR-8, NFR-2

## Context
The brief allows any image strategy: reference photos, fully AI-generated visuals, web search, or uploads. In e-commerce the ad must show **the exact product the customer receives**.

## Decision
Use a hybrid pipeline:
1. Take the **real product photo** (reference file, URL, or user upload).
2. **Remove its background** locally (`rembg`, free, offline).
3. Generate a **scene/background** with a free image model, prompted from the vision analysis and brand profile.
4. **Composite** the product onto the scene, then add the headline, CTA, and logo in brand colours (Pillow).

**Fallback:** if scene generation fails, use a brand-colour gradient background. The pipeline never stops.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| AI draws the whole product | Image models hallucinate the shape, logos, and details. An ad showing a different product is misleading and causes returns. |
| Raw photo + text overlay only | Safe but visually flat; weak on "eye-catching creatives". |
| Web image search | Needs API keys, has licensing risk, and may return a different product variant. |

## Consequences
- The product is always 100% authentic. The creativity goes into the scene, layout, and copy.
- Adds a background-removal dependency (one model download of roughly 170 MB on first run).
- Background removal quality varies with the photo. The vision step can flag poor cut-outs.

## Finding (Phase 5, 2026-11-05)
The hybrid approach assumes the reference photo shows the product. Looking at all 30 photos, **only about a third do**: others show a different item (an iced coffee for the kettle, a blender for the air fryer) or another company's product (Chanel, Nike, Clinique, Dolce & Gabbana). The brief calls the photos "reference only" for this reason.
- The vision step now reports `photo_matches_product`, `photo_shows` and `visible_brands` (same request, no extra cost), and the pipeline turns them into warnings.
- Which photos to use for the demo is an owner decision (matching products vs. uploading correct photos).

## Amendment (Phase 8, 2026-11-07): an AI-generated product photo, as an option
- **Why:** the brief's dashboard asks for the option to "let AI generate a new one", and only about a third of the catalog photos show the right product.
- **Decision (owner):** the default stays the real photo. *AI-generated photo* is an explicit choice in the dashboard: Pollinations paints the product from its catalog description (name, description, 3 features) on a plain background, with its own guard rails and cache entry. The ad always carries the warning *"the product photo is AI-generated: check that it shows the real product before publishing"*.
- *New angle* keeps the same AI photo (seed 0): a new angle changes the copy and the background, not the product.
- Without the image service (AI off), the catalog photo is used and the reason is noted.
- **Live check:** the kettle (#26, whose catalog photo is an iced coffee) came back in 7 s as a recognisable gooseneck kettle with one wrong detail, which is exactly why the warning exists.
