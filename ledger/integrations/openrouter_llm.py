"""
OpenRouter shim: same ``messages.create(...)`` surface as Gemini/Anthropic shims used by BaseApexAgent.

Uses OpenRouter's OpenAI-compatible API: https://openrouter.ai/docs
Keys: OPENROUTER_API_KEY or OPENROUTER_KEY (``sk-or-v1-...``).

If you only have a key in GEMINI_API_KEY but it is actually an OpenRouter key, use
``--llm openrouter`` or set OPENROUTER_API_KEY and use ``--llm auto``.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import Any

import httpx

from ledger.integrations.llm_key_utils import is_likely_openrouter_key

_LOG = logging.getLogger(__name__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def default_openrouter_model() -> str:
    """Model slug on OpenRouter, e.g. ``google/gemini-2.0-flash-001``."""
    return os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")


def resolve_openrouter_api_key() -> str | None:
    """
    Prefer OPENROUTER_API_KEY / OPENROUTER_KEY.
    Accept a misplaced OpenRouter key stored under GEMINI_API_KEY / GOOGLE_API_KEY with a warning.
    """
    k = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
    if k:
        return k.strip()
    g = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if g and is_likely_openrouter_key(g):
        _LOG.warning(
            "Using OpenRouter-shaped key from GEMINI_API_KEY/GOOGLE_API_KEY; "
            "prefer OPENROUTER_API_KEY=... for clarity."
        )
        return g.strip()
    return None


def _approx_cost_usd(input_tokens: int, output_tokens: int) -> float:
    # Rough OpenRouter / mid-tier model placeholder for telemetry only.
    return round(input_tokens / 1e6 * 0.50 + output_tokens / 1e6 * 1.50, 6)


class OpenRouterMessages:
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
        user_text = ""
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content")
                user_text = c if isinstance(c, str) else str(c)

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system or ""},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Optional attribution (OpenRouter recommends setting these)
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://localhost"),
            "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "Axiom Ledger"),
        }
        _LOG.debug("openrouter_chat model=%s max_tokens=%s", model, max_tokens)

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            try:
                resp = await client.post(OPENROUTER_CHAT_URL, json=payload, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                _LOG.exception(
                    "openrouter_http_error status=%s body=%s",
                    e.response.status_code,
                    (e.response.text or "")[:500],
                )
                raise
            except Exception:
                _LOG.exception("openrouter_request_failed model=%s", model)
                raise
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {data!r}"[:500])

        msg = choices[0].get("message") or {}
        text = (msg.get("content") or "").strip()

        usage = data.get("usage") or {}
        inp = int(usage.get("prompt_tokens", 0) or 0)
        out = int(usage.get("completion_tokens", 0) or 0)
        total_cost = usage.get("total_cost")
        if total_cost is not None:
            try:
                cost = float(total_cost)
            except (TypeError, ValueError):
                cost = _approx_cost_usd(inp, out)
        else:
            cost = _approx_cost_usd(inp, out)

        _LOG.info(
            "openrouter_response model=%s prompt_tokens=%s output_tokens=%s output_chars=%s approx_cost_usd=%s",
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


class OpenRouterShimClient:
    """Passed as ``client`` to BaseApexAgent; ``self.model`` should be an OpenRouter model id."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or resolve_openrouter_api_key()
        if not key:
            raise ValueError(
                "OpenRouter API key missing: set OPENROUTER_API_KEY or OPENROUTER_KEY "
                "(or place an OpenRouter ``sk-or-v1-...`` key in GEMINI_API_KEY only if you "
                "intend OpenRouter — prefer OPENROUTER_API_KEY for clarity)."
            )
        self.messages = OpenRouterMessages(key)
