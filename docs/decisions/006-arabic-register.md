# ADR-006 — Arabic register

- **Status:** Accepted — 2026-11-05
- **Spec ref:** D-6, FR-5, §8

## Context
The Arabic products target GCC / Middle East shoppers. Arabic ad copy can read as stiff (formal MSA) or as machine-translated.

## Decision
**Modern Standard Arabic with a light Gulf flavour.** It is widely understood across the Arab world and close to the target market. Copy is written natively in Arabic, never translated from English. The register is a brand-profile setting that can be changed in the dashboard.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| Formal MSA | Correct but reads like an official notice; weak for ads. |
| Full Gulf dialect | Closest to the market, but free LLMs write dialect unreliably. |
| Levantine | Doesn't match the catalog's target market. |

## Consequences
- The Arabic prompt template carries the register instructions plus example phrasing.
- Arabic output is reviewed by the owner (native speaker) during Phase 4.
