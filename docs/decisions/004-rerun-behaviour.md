# ADR-004 — Rerun behaviour

- **Status:** Accepted — 2026-11-05
- **Spec ref:** D-4, FR-11, FR-13

## Context
Regenerating a product produces files with the same names in the same folder (`output/{Brand}/{Product}/creative_feed.png`). We must define what happens to the previous files.

## Decision
- **CLI:** overwrite. `output/` always holds exactly one clean campaign per product, matching the structure required by the brief.
- **Dashboard:** "Regenerate" keeps previous variants **in the session** so the user can compare them side by side; the chosen variant is what gets saved and exported to `.zip`.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| Versioned folders (`v1/`, `v2/`) on disk | Deviates from the required structure and clutters `output/`. |

## Consequences
- Session variants are lost when the browser session ends, which is acceptable (the user exports what they want).
- `metadata.json` records the generation timestamp, so a rerun can always be identified.
