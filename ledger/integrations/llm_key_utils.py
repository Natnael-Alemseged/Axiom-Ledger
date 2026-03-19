"""
Helpers to distinguish API key types.

OpenRouter keys (``sk-or-v1-...``) must not be passed to Google's Generative Language API.
Google AI Studio keys typically start with ``AIza``.
"""

from __future__ import annotations

import os


def is_likely_openrouter_key(key: str | None) -> bool:
    if not key:
        return False
    k = key.strip()
    return k.startswith("sk-or-") or k.startswith("sk-or-v1")


def is_likely_google_ai_studio_key(key: str | None) -> bool:
    """Heuristic: Google AI Studio / Gemini API keys often start with AIza."""
    if not key:
        return False
    k = key.strip()
    if is_likely_openrouter_key(k):
        return False
    return k.startswith("AIza")


def effective_google_generative_key() -> str | None:
    """First env value that is usable for Google's API (not an OpenRouter key)."""
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        k = os.environ.get(env_name)
        if k and not is_likely_openrouter_key(k):
            return k.strip()
    return None
