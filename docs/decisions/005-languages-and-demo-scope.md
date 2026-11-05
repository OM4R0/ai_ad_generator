# ADR-005 — Languages and demo scope

- **Status:** Accepted — 2026-11-05
- **Spec ref:** D-5, §9

## Context
The brief requires at least 2 languages and at least 10 products. The catalog assigns each product a language: EN 10, AR 11, FR 4, ES 3, DE 2.

## Decision
1. **Support every catalog language.** The pipeline is language-generic: the language is a prompt parameter. Arabic is the only language that needs special handling (RTL, letter shaping, an Arabic font).
2. **Focus quality on English and Arabic.** These are the languages the owner can personally verify. FR/ES/DE are generated with the same rules but presented as "supported", not as the showcase.
3. **Start with 10 products** (the brief's minimum): 10 × 2 calls = 20 LLM requests, which fits comfortably in the free daily cap (ADR-003) and leaves room for reruns. Scaling to all 30 is an optional later step, because the pipeline doesn't change.

**The initial 10** (different niche each; 5 EN + 5 AR):

| # | Product | Language |
|---|---|---|
| 1 | AeroGlide Carbon Running Shoes | English |
| 13 | AeroSonic Pro ANC Wireless Headphones | English |
| 19 | Nautilus 300M Automatic Diver Wristwatch | English |
| 26 | PrecisionFlow Electric Temperature Gooseneck Kettle | English |
| 29 | QuickLock Rapid Selectable Dumbbell Pair | English |
| 10 | Sultan Royal Amber Oud Eau de Parfum | Arabic |
| 7 | Radiance Glow 15% Vitamin C + Ferulic Serum | Arabic |
| 16 | PrecisionBlade X9 Titanium Beard Trimmer | Arabic |
| 23 | NomadPro Waterproof 16-inch Laptop Backpack | Arabic |
| 27 | CrispWave 6.5L Digital Turbo Air Fryer | Arabic |

FR/ES/DE are covered by the same pipeline and are demonstrated on one product each only once the first 10 are solid.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| EN + AR only | Ignores the catalog's `Suggested Language` column for 9 products, which would mean generating the wrong language for them. |
| Showcase all 5 equally | The owner cannot vouch for French/Spanish/German quality; a reviewer who speaks one of them could spot errors. |

## Consequences
- Font set: one Latin font + one Arabic font (both open-license).
- Interview position: "It works in any language; I focused quality where I can verify it."
