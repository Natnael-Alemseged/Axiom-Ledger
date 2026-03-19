"""Backward compatibility: re-export from refinery.models and refinery.strategies / refinery.agents."""

from refinery.models import (
    Bbox,
    ExtractedDocument,
    ExtractedPage,
    ExtractedTable,
    ExtractedFigure,
    TextBlock,
    FontInfo,
)
from refinery.strategies.base import Extractor, load_extraction_rules
from refinery.strategies.fast_text import FastTextExtractor
from refinery.strategies.layout_docling import LayoutExtractor, DoclingDocumentAdapter
from refinery.strategies.vision_openrouter import VisionExtractor
from refinery.agents.extractor import run_extraction

__all__ = [
    "Bbox",
    "ExtractedDocument",
    "ExtractedPage",
    "ExtractedTable",
    "ExtractedFigure",
    "TextBlock",
    "FontInfo",
    "Extractor",
    "load_extraction_rules",
    "FastTextExtractor",
    "LayoutExtractor",
    "DoclingDocumentAdapter",
    "VisionExtractor",
    "run_extraction",
]
