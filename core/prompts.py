"""Prompt templates (design.md §6).

Templates live in `config/prompt_templates/<name>.txt`, so prompts can be
edited without touching code. The file name carries the version
(`vision_v1`), and that version is recorded in metadata.json.

File format:
    === SYSTEM ===
    ...system prompt...
    === USER ===
    ...user prompt with $placeholders...

Placeholders use `$name` (string.Template), so JSON braces in the prompt
need no escaping. A missing value raises KeyError instead of silently
sending "$name" to the model.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from config.settings import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "config" / "prompt_templates"
_SYSTEM, _USER = "=== SYSTEM ===", "=== USER ==="


def load_prompt(name: str, prompts_dir: Path = PROMPTS_DIR, **values: str) -> tuple[str, str]:
    """Load `<name>.txt` and fill its placeholders. Returns (system, user)."""
    text = (prompts_dir / f"{name}.txt").read_text(encoding="utf-8-sig")
    if _SYSTEM not in text or _USER not in text:
        raise ValueError(f"prompt {name!r} needs '{_SYSTEM}' and '{_USER}' sections")
    system, user = text.split(_SYSTEM, 1)[1].split(_USER, 1)
    return (
        Template(system.strip()).substitute(values),
        Template(user.strip()).substitute(values),
    )


def read_text_file(path: Path) -> str:
    """Read a plain text resource (e.g. a language guide); '' if it doesn't exist."""
    return path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""
