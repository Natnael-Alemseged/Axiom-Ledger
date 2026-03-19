"""
Google Gemini shim: same surface as AsyncAnthropic.messages.create(...) used by BaseApexAgent.
Uses GEMINI_API_KEY or GOOGLE_API_KEY from the environment.
"""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any

from ledger.integrations.llm_key_utils import is_likely_openrouter_key

_LOG = logging.getLogger(__name__)

_GEMINI_INSTALL_HINT = (
    "Install with: .venv/bin/pip install 'google-generativeai>=0.8.0,<1.0' "
    "or pip install -r requirements.txt"
)


def _import_genai():
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise ImportError(
            "Missing package for Gemini LLM. " + _GEMINI_INSTALL_HINT
        ) from e
    return genai


def _approx_cost_usd(input_tokens: int, output_tokens: int) -> float:
    # Approximate Gemini 2.0 Flash–class list pricing (USD); telemetry only.
    return round(input_tokens / 1e6 * 0.075 + output_tokens / 1e6 * 0.30, 6)


def default_gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


class GeminiMessages:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def create(
        self,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> SimpleNamespace:
        genai = _import_genai()

        user_text = ""
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content")
                user_text = c if isinstance(c, str) else str(c)

        gen_cfg: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": 0.2,
        }
        _LOG.debug("gemini_generate model=%s max_output_tokens=%s", model, max_tokens)

        def _sync_generate():
            genai.configure(api_key=self._api_key)
            gm = genai.GenerativeModel(
                model_name=model,
                system_instruction=(system or None),
            )
            return gm.generate_content(user_text, generation_config=gen_cfg)

        try:
            # google-generativeai is sync-first; keep the agent async API non-blocking.
            response = await asyncio.to_thread(_sync_generate)
        except Exception:
            _LOG.exception("gemini_generate_failed model=%s", model)
            raise
        text = ""
        try:
            text = (response.text or "").strip()
        except (ValueError, AttributeError):
            parts = []
            for cand in getattr(response, "candidates", None) or []:
                for part in getattr(cand.content, "parts", None) or []:
                    if hasattr(part, "text") and part.text:
                        parts.append(part.text)
            text = "\n".join(parts).strip()

        inp = out = 0
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            inp = int(getattr(um, "prompt_token_count", 0) or 0)
            out = int(getattr(um, "candidates_token_count", 0) or 0)

        cost = _approx_cost_usd(inp, out)
        _LOG.info(
            "gemini_response model=%s prompt_tokens=%s output_tokens=%s output_chars=%s approx_cost_usd=%s",
            model,
            inp,
            out,
            len(text),
            cost,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(
                input_tokens=inp,
                output_tokens=out,
                cost_usd=cost,
            ),
        )


class GeminiShimClient:
    """Passed as ``client`` to BaseApexAgent; ``self.model`` should be a Gemini model id."""

    def __init__(self, api_key: str | None = None) -> None:
        _import_genai()  # fail fast with install hint before any agent work
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError(
                "Gemini API key missing: set GEMINI_API_KEY or GOOGLE_API_KEY in the environment."
            )
        if is_likely_openrouter_key(key):
            raise ValueError(
                "GEMINI_API_KEY/GOOGLE_API_KEY looks like an OpenRouter key (sk-or-v1-...). "
                "Google's API expects an AI Studio key (often starts with AIza). "
                "Use OPENROUTER_API_KEY with --llm openrouter, or set a real Gemini key for --llm gemini."
            )
        self.messages = GeminiMessages(key)
