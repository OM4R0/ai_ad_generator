# Architecture Decision Records

One file per significant decision: **context → decision → alternatives rejected → consequences.**
Status values: `Proposed` · `Accepted` · `Superseded by ADR-xxx`.

| ADR | Decision | Status |
|---|---|---|
| [001](001-hybrid-product-visuals.md) | Hybrid visuals: real product photo + AI scene | Accepted |
| [002](002-streamlit-dashboard.md) | Streamlit for the dashboard | Accepted |
| [003](003-llm-model-selection.md) | Gemini direct (one multimodal model) primary, OpenRouter fallback, one OpenAI-compatible client | Accepted (model ID confirmed in Phase 3) |
| [004](004-rerun-behaviour.md) | CLI overwrites; dashboard keeps variants in session | Accepted |
| [005](005-languages-and-demo-scope.md) | Support all catalog languages, focus quality on EN + AR; start with 10 products | Accepted |
| [006](006-arabic-register.md) | Arabic register: MSA with a light Gulf flavour | Accepted |
| [007](007-image-generation-provider.md) | Pollinations for scene backgrounds; gradient fallback | Accepted |
