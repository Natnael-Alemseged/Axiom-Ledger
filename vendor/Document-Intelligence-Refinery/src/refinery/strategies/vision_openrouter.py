"""Strategy C: Vision-augmented extraction via OpenRouter with budget_guard and retry."""

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from refinery.models import DocumentProfile, Bbox, ExtractedDocument, ExtractedPage, ExtractedTable, ExtractedFigure, TextBlock
from refinery.strategies.base import load_extraction_rules
from refinery.strategies.config_models import ExtractionRules

logger = logging.getLogger(__name__)

# Approximate USD per 1K tokens for common vision models (fallback if usage not in response)
DEFAULT_INPUT_USD_PER_1K = 0.0001
DEFAULT_OUTPUT_USD_PER_1K = 0.0003


def _render_pdf_to_images(pdf_path: Path, dpi: int = 150) -> List[bytes]:
    """Render PDF pages to PNG bytes using PyMuPDF (fitz)."""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PyMuPDF (fitz) is required for VisionExtractor")
    doc = fitz.open(pdf_path)
    images = []
    for i in range(len(doc)):
        page = doc[i]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def _parse_vision_json(raw: str) -> Optional[dict]:
    """Extract JSON object from model output (handle markdown code blocks)."""
    raw = (raw or "").strip()
    # Try raw parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try to find ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try first { ... } block
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


class VisionRetryHandler:
    """Re-prompt or retry on malformed JSON from the vision model."""

    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    def parse_with_retry(self, raw: str, retries: int | None = None) -> Optional[dict]:
        retries = retries if retries is not None else self.max_retries
        out = _parse_vision_json(raw)
        if out is not None:
            return out
        for _ in range(retries):
            out = _parse_vision_json(raw)
            if out is not None:
                return out
        return None


def _doc_from_vision_pages(pages_data: List[dict], doc_id: str, source_path: Optional[Path], strategy_used: str) -> ExtractedDocument:
    """Build ExtractedDocument from list of per-page parsed JSON from vision model."""
    pages_out: List[ExtractedPage] = []
    for i, data in enumerate(pages_data):
        text_blocks = []
        tables = []
        figures = []
        reading_order = []
        elem_id = 0
        for blk in data.get("text_blocks", data.get("texts", [])):
            if isinstance(blk, dict):
                text = blk.get("text", blk.get("content", ""))
            else:
                text = str(blk)
            bbox = None
            if isinstance(blk, dict) and "bbox" in blk:
                b = blk["bbox"]
                if isinstance(b, (list, tuple)) and len(b) >= 4:
                    bbox = Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
            tb = TextBlock.from_text_bbox(text, bbox, page_index=i)
            eid = f"p{i}-e{elem_id}"
            elem_id += 1
            reading_order.append(eid)
            text_blocks.append(tb)
        for tbl in data.get("tables", []):
            rows = tbl if isinstance(tbl, list) else tbl.get("data", tbl.get("rows", []))
            bbox = None
            if isinstance(tbl, dict) and "bbox" in tbl:
                b = tbl["bbox"]
                if isinstance(b, (list, tuple)) and len(b) >= 4:
                    bbox = Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
            et = ExtractedTable.from_data_bbox(rows, bbox, page_index=i)
            eid = f"p{i}-e{elem_id}"
            elem_id += 1
            reading_order.append(eid)
            tables.append(et)
        for fig in data.get("figures", []):
            bbox = None
            if isinstance(fig, dict) and "bbox" in fig:
                b = fig["bbox"]
                if isinstance(b, (list, tuple)) and len(b) >= 4:
                    bbox = Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
            caption = isinstance(fig, dict) and fig.get("caption") or None
            figures.append(ExtractedFigure(bbox=bbox, page_index=i, caption=caption))
            eid = f"p{i}-e{elem_id}"
            elem_id += 1
            reading_order.append(eid)
        pages_out.append(
            ExtractedPage(
                page_index=i,
                text_blocks=text_blocks,
                tables=tables,
                figures=figures,
                reading_order=reading_order,
            )
        )
    return ExtractedDocument(
        doc_id=doc_id,
        source_path=source_path,
        page_count=len(pages_out),
        strategy_used=strategy_used,
        pages=pages_out,
        status="completed",
    )


def _estimate_cost_usd(usage: dict, model_id: str) -> float:
    """Estimate USD from usage (prompt_tokens, completion_tokens). Uses defaults if missing."""
    prompt = int(
        usage.get("prompt_tokens") or usage.get("promptTokenCount") or 0
    )
    completion = int(
        usage.get("completion_tokens") or usage.get("completionTokenCount") or 0
    )
    if "native_total_cost" in usage and usage["native_total_cost"] is not None:
        return float(usage["native_total_cost"])
    return (prompt / 1000.0 * DEFAULT_INPUT_USD_PER_1K) + (completion / 1000.0 * DEFAULT_OUTPUT_USD_PER_1K)


class VisionExtractor:
    """Extract using a vision model. Tries OpenRouter first, then Groq, Google Gemini, SambaNova on failure. Budget cap per document."""

    def __init__(
        self,
        extraction_rules: ExtractionRules | None = None,
        api_key: Optional[str] = None,
    ):
        self.rules = extraction_rules or load_extraction_rules()
        self.api_key = api_key or _get_openrouter_api_key()
        self.retry_handler = VisionRetryHandler(max_retries=self.rules.vision.max_retries)

    def _model_for_profile(self, profile: DocumentProfile) -> str:
        """Dynamic model selection: cheap/fast vs quality by layout_complexity."""
        if profile.layout_complexity in ("table_heavy", "figure_heavy", "mixed"):
            return self.rules.vision.model_quality
        return self.rules.vision.model_cheap

    def extract(self, pdf_path: Path, profile: DocumentProfile) -> tuple[ExtractedDocument, float]:
        openrouter_key = self.api_key or _get_openrouter_api_key()
        groq_key = _get_groq_api_key()
        google_key = _get_google_api_key()
        sambanova_key = _get_sambanova_api_key()
        if not any([openrouter_key, groq_key, google_key, sambanova_key]):
            raise RuntimeError(
                "At least one of OPENROUTER_API_KEY/OPENROUTER_KEY, GROQ_API_KEY, "
                "GOOGLE_API_KEY/GEMINI_API_KEY, or SAMBANOVA_KEY is required for VisionExtractor"
            )
        images = _render_pdf_to_images(pdf_path)
        doc_id = profile.doc_id or ""
        budget_usd = self.rules.vision.budget_per_document_usd
        spent_usd = 0.0
        model_id = self._model_for_profile(profile)
        pages_data: List[dict] = []
        strategy_used = "vision"

        extraction_prompt = """Extract all text and structure from this document page image.
Return a JSON object with exactly these keys:
- "text_blocks": list of {"text": "...", "bbox": [x0, top, x1, bottom] or null}
- "tables": list of {"data": [[cell, ...], ...], "bbox": [x0, top, x1, bottom] or null}
- "figures": list of {"caption": "..." or null, "bbox": [x0, top, x1, bottom] or null}
Use points (72 DPI) for bbox. Return only valid JSON, no markdown."""

        for i, img_bytes in enumerate(images):
            if spent_usd >= budget_usd:
                logger.info("Vision budget exceeded (%.4f >= %.4f USD), stopping at page %d", spent_usd, budget_usd, i)
                doc = _doc_from_vision_pages(pages_data, doc_id, pdf_path, strategy_used)
                doc.status = "truncated_budget"
                doc.page_count = len(images)
                return doc, 1.0
            if spent_usd > 0 and spent_usd + 0.05 >= budget_usd:
                logger.info("Estimated next page would exceed budget, stopping at page %d", i)
                doc = _doc_from_vision_pages(pages_data, doc_id, pdf_path, strategy_used)
                doc.status = "truncated_budget"
                doc.page_count = len(images)
                return doc, 1.0

            b64 = base64.b64encode(img_bytes).decode("ascii")
            url = f"data:image/png;base64,{b64}"
            messages = [
                {"role": "user", "content": [{"type": "text", "text": extraction_prompt}, {"type": "image_url", "image_url": {"url": url}}]},
            ]
            result = _request_vision_page(
                messages, extraction_prompt, b64, model_id,
                openrouter_key, groq_key, google_key, sambanova_key,
            )
            if result is None:
                logger.warning("All vision providers failed for page %d", i)
                pages_data.append({"text_blocks": [], "tables": [], "figures": []})
                continue
            raw, usage, provider = result
            cost = _estimate_cost_usd(usage, model_id)
            spent_usd += cost
            logger.info("Vision page %d (%s): cost ~%.4f USD, total ~%.4f USD", i, provider, cost, spent_usd)
            parsed = self.retry_handler.parse_with_retry(raw)
            if parsed:
                pages_data.append(parsed)
            else:
                pages_data.append({"text_blocks": [{"text": raw, "bbox": None}], "tables": [], "figures": []})

        doc = _doc_from_vision_pages(pages_data, doc_id, pdf_path, strategy_used)
        doc.metadata["vision_cost_usd"] = spent_usd
        return doc, 1.0


def _get_openrouter_api_key() -> Optional[str]:
    import os
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")


def _get_groq_api_key() -> Optional[str]:
    import os
    return os.environ.get("GROQ_API_KEY")


def _get_google_api_key() -> Optional[str]:
    import os
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def _get_sambanova_api_key() -> Optional[str]:
    import os
    return os.environ.get("SAMBANOVA_KEY") or os.environ.get("SAMBANOVA_API_KEY")


def _vision_request_openrouter(
    api_key: str, model: str, messages: List[dict], timeout: float = 120.0
) -> tuple[str, dict] | None:
    """Send vision request to OpenRouter. Returns (content, usage) or None on failure."""
    try:
        import httpx
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return content, usage
    except Exception as e:
        logger.debug("OpenRouter vision request failed: %s", e)
        return None


def _vision_request_groq(
    api_key: str, model: str, messages: List[dict], timeout: float = 120.0
) -> tuple[str, dict] | None:
    """Send vision request to Groq (OpenAI-compatible). Returns (content, usage) or None."""
    try:
        import httpx
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return content, usage
    except Exception as e:
        logger.debug("Groq vision request failed: %s", e)
        return None


def _vision_request_google(
    api_key: str, model: str, prompt_text: str, image_b64: str, timeout: float = 120.0
) -> tuple[str, dict] | None:
    """Send vision request to Google Gemini REST. Returns (content, usage) or None."""
    try:
        import httpx
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        body = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt_text},
                    {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                ],
            }],
            "generationConfig": {"maxOutputTokens": 4096},
        }
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        content = "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))
        usage = data.get("usageMetadata") or {}
        return content, usage
    except Exception as e:
        logger.debug("Google Gemini vision request failed: %s", e)
        return None


def _vision_request_sambanova(
    api_key: str, model: str, messages: List[dict], timeout: float = 120.0
) -> tuple[str, dict] | None:
    """Send vision request to SambaNova (OpenAI-compatible). Returns (content, usage) or None."""
    try:
        import httpx
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://api.sambanova.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return content, usage
    except Exception as e:
        logger.debug("SambaNova vision request failed: %s", e)
        return None


GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GOOGLE_VISION_MODEL = "gemini-2.0-flash"
SAMBANOVA_VISION_MODEL = "Meta-Llama-3.1-8B-Instruct"


def _request_vision_page(
    messages: List[dict],
    prompt_text: str,
    image_b64: str,
    model_id: str,
    openrouter_key: Optional[str],
    groq_key: Optional[str],
    google_key: Optional[str],
    sambanova_key: Optional[str],
) -> tuple[str, dict, str] | None:
    """Try providers in order: OpenRouter -> Groq -> Google Gemini -> SambaNova. Returns (content, usage, provider_name) or None."""
    if openrouter_key:
        out = _vision_request_openrouter(openrouter_key, model_id, messages)
        if out:
            return out[0], out[1], "openrouter"
    if groq_key:
        out = _vision_request_groq(groq_key, GROQ_VISION_MODEL, messages)
        if out:
            return out[0], out[1], "groq"
    if google_key:
        out = _vision_request_google(google_key, GOOGLE_VISION_MODEL, prompt_text, image_b64)
        if out:
            return out[0], out[1], "google"
    if sambanova_key:
        out = _vision_request_sambanova(sambanova_key, SAMBANOVA_VISION_MODEL, messages)
        if out:
            return out[0], out[1], "sambanova"
    return None
