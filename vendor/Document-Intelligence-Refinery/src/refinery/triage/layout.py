"""Layout complexity detection: columns, table_heavy, figure_heavy (images + rects + curves)."""

from typing import Any, List, Literal

from refinery.triage.config import TriageRules, load_triage_rules

LayoutComplexity = Literal[
    "single_column", "multi_column", "table_heavy", "figure_heavy", "mixed"
]


def _page_area(page: Any) -> float:
    w = float(getattr(page, "width", 1) or 1)
    h = float(getattr(page, "height", 1) or 1)
    return w * h


def _image_area(images: List[Any]) -> float:
    total = 0.0
    for im in images:
        x0, x1 = im.get("x0", 0), im.get("x1", 0)
        top, bottom = im.get("top", 0), im.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _rect_area(rects: List[Any]) -> float:
    total = 0.0
    for r in rects:
        x0, x1 = r.get("x0", 0), r.get("x1", 0)
        top, bottom = r.get("top", 0), r.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _curve_area(curves: List[Any]) -> float:
    total = 0.0
    for c in curves:
        x0, x1 = c.get("x0", 0), c.get("x1", 0)
        top, bottom = c.get("top", 0), c.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _table_area(page: Any) -> float:
    """Sum of table bbox areas on the page (pdfplumber find_tables)."""
    total = 0.0
    try:
        tables = page.find_tables() if hasattr(page, "find_tables") else []
        for t in tables:
            bbox = getattr(t, "bbox", None)
            if bbox and len(bbox) >= 4:
                x0, top, x1, bottom = bbox[0], bbox[1], bbox[2], bbox[3]
                total += (x1 - x0) * (bottom - top)
    except Exception:
        pass
    return total


def _column_count_from_chars(page: Any) -> int:
    """Heuristic: cluster char x0 positions into columns; return number of columns."""
    chars = list(getattr(page, "chars", []) or [])
    if not chars:
        return 1
    # Group by approximate x0 (e.g. 50pt tolerance)
    tolerance = 50.0
    positions: List[float] = []
    for c in chars:
        x0 = c.get("x0")
        if x0 is not None:
            positions.append(float(x0))
    if not positions:
        return 1
    positions.sort()
    columns = 1
    last = positions[0]
    for x in positions[1:]:
        if x > last + tolerance:
            columns += 1
            last = x
    return min(columns, 5)  # cap for sanity


def classify_page_layout(page: Any, rules: TriageRules) -> LayoutComplexity:
    """Classify a single page's layout (single_column, multi_column, table_heavy, figure_heavy, mixed)."""
    area = _page_area(page)
    if area <= 0:
        return "single_column"

    table_a = _table_area(page)
    image_a = _image_area(list(getattr(page, "images", []) or []))
    rect_a = _rect_area(list(getattr(page, "rects", []) or []))
    curve_a = _curve_area(list(getattr(page, "curves", []) or []))
    figure_a = image_a + rect_a + curve_a

    table_ratio = table_a / area
    figure_ratio = figure_a / area
    columns = _column_count_from_chars(page)

    table_heavy = table_ratio >= rules.table_ratio.high
    fig_heavy = figure_ratio >= rules.figure_ratio.high
    multi_col = columns >= 2

    if table_heavy and fig_heavy:
        return "mixed"
    if table_heavy:
        return "table_heavy"
    if fig_heavy:
        return "figure_heavy"
    if multi_col:
        return "multi_column"
    return "single_column"


def aggregate_layout_complexity(
    page_layouts: List[LayoutComplexity],
) -> LayoutComplexity:
    """Doc-level layout: if any page is table_heavy or figure_heavy, prefer that; else majority."""
    if not page_layouts:
        return "single_column"
    if any(p == "mixed" for p in page_layouts):
        return "mixed"
    table_count = sum(1 for p in page_layouts if p == "table_heavy")
    figure_count = sum(1 for p in page_layouts if p == "figure_heavy")
    multi_count = sum(1 for p in page_layouts if p == "multi_column")
    single_count = sum(1 for p in page_layouts if p == "single_column")
    n = len(page_layouts)
    if table_count >= n // 2:
        return "table_heavy"
    if figure_count >= n // 2:
        return "figure_heavy"
    if multi_count > single_count:
        return "multi_column"
    return "single_column"


def detect_layout(
    pages: List[Any],
    rules: TriageRules | None = None,
) -> LayoutComplexity:
    """Detect document-level layout complexity from a list of pages."""
    rules = rules or load_triage_rules()
    page_layouts = [classify_page_layout(p, rules) for p in pages]
    return aggregate_layout_complexity(page_layouts)
