"""Extraction strategies: FastText, Layout (Docling), Vision (OpenRouter + fallbacks)."""

from refinery.strategies.base import Extractor, load_extraction_rules
from refinery.strategies.fast_text import FastTextExtractor
from refinery.strategies.layout_docling import LayoutExtractor
from refinery.strategies.vision_openrouter import VisionExtractor

__all__ = [
    "Extractor",
    "load_extraction_rules",
    "FastTextExtractor",
    "LayoutExtractor",
    "VisionExtractor",
]
