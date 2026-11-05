# ADR-002 — Streamlit for the dashboard

- **Status:** Accepted — 2026-11-05
- **Spec ref:** D-2, FR-13

## Context
We need a multi-panel UI (selector, brand editor, side-by-side preview, carousel, regenerate, zip export) with correct Arabic RTL, built quickly in Python.

## Decision
Use **Streamlit**.

## Alternatives rejected
| Option | Why rejected |
|---|---|
| Gradio | Built for model demos (input → output); a multi-section dashboard with persistent state is awkward. |
| Custom web app (React/FastAPI) | Full control, but weeks of work. The brief weights the dashboard at 10% against 60% for functionality and copy quality, so the time is better spent there. |

## Consequences
- Fast iteration; `st.session_state` holds the regenerated variants (ADR-004).
- RTL is handled with injected HTML/CSS (`dir="rtl"`) for the Arabic copy blocks.
- Fine-grained layout control is limited, which is acceptable for this scope.
