"""ChunkValidator: verifies the five chunking rules before emitting LDUs."""

import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from refinery.models import LDU


class RuleViolation(BaseModel):
    """A single chunking rule violation."""

    rule_id: str = ""
    message: str = ""
    chunk_id: str = ""


class ValidationResult(BaseModel):
    """Result of validating a list of LDUs against the chunking constitution."""

    valid: bool = True
    violations: List[RuleViolation] = Field(default_factory=list)


def _content_str(ldu: LDU) -> str:
    """Extract searchable string from LDU content."""
    if ldu.content is None:
        return ""
    if isinstance(ldu.content, str):
        return ldu.content
    if isinstance(ldu.content, list):
        return " ".join(str(row) for row in ldu.content)
    return str(ldu.content)


class ChunkValidator:
    """Validates List[LDU] against the five chunking rules."""

    # Patterns that suggest cross-refs should be resolved
    CROSS_REF_PATTERNS = [
        re.compile(r"Table\s+\d+", re.IGNORECASE),
        re.compile(r"Figure\s+\d+", re.IGNORECASE),
        re.compile(r"see\s+Table\s+\d+", re.IGNORECASE),
        re.compile(r"see\s+Figure\s+\d+", re.IGNORECASE),
    ]

    def validate(self, ldus: List[LDU], max_tokens_for_list: Optional[int] = None) -> ValidationResult:
        violations: List[RuleViolation] = []
        seen_non_root_section = False

        for ldu in ldus:
            # Rule 1: Table/header - table LDU must have content (full table)
            if ldu.kind == "table":
                if ldu.content is None or (isinstance(ldu.content, list) and len(ldu.content) == 0):
                    violations.append(
                        RuleViolation(
                            rule_id="table_header",
                            message="Table LDU has no content (table must not be split from header)",
                            chunk_id=ldu.chunk_id or "(no id)",
                        )
                    )

            # Rule 2: Figure caption in metadata or content
            if ldu.kind == "figure":
                caption = (ldu.metadata or {}).get("caption") if ldu.metadata else None
                content_has_caption = bool(_content_str(ldu).strip())
                if not caption and not content_has_caption:
                    violations.append(
                        RuleViolation(
                            rule_id="figure_caption",
                            message="Figure LDU must have caption in metadata or content",
                            chunk_id=ldu.chunk_id or "(no id)",
                        )
                    )

            # Rule 3: Numbered list - list LDU must not exceed max_tokens (single LDU unless over limit)
            if ldu.kind == "list" and max_tokens_for_list is not None:
                if ldu.token_count > max_tokens_for_list:
                    violations.append(
                        RuleViolation(
                            rule_id="numbered_list",
                            message=f"List LDU exceeds max_tokens_for_list ({ldu.token_count} > {max_tokens_for_list}); must be split or capped",
                            chunk_id=ldu.chunk_id or "(no id)",
                        )
                    )

            # Rule 4: Section headers - chunks after a section must inherit parent_section
            if ldu.parent_section and (ldu.parent_section or "").strip() and ldu.parent_section != "(root)":
                seen_non_root_section = True
            if seen_non_root_section and (ldu.parent_section is None or (ldu.parent_section or "").strip() == ""):
                violations.append(
                    RuleViolation(
                        rule_id="section_header",
                        message="Chunk must have parent_section after a section heading has been seen",
                        chunk_id=ldu.chunk_id or "(no id)",
                    )
                )

            # Rule 5: Cross-refs - if content mentions Table N / Figure N, metadata should have cross_refs or raw
            content = _content_str(ldu)
            for pat in self.CROSS_REF_PATTERNS:
                if pat.search(content):
                    cross_refs = (ldu.metadata or {}).get("cross_refs") if ldu.metadata else None
                    if cross_refs is None:
                        violations.append(
                            RuleViolation(
                                rule_id="cross_refs",
                                message="Content mentions table/figure reference but metadata has no cross_refs",
                                chunk_id=ldu.chunk_id or "(no id)",
                            )
                        )
                    break

        return ValidationResult(valid=len(violations) == 0, violations=violations)
