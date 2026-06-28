from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CanonicalRule:
    rule_id: str
    entity: str
    property: str
    operator: str
    value: float
    unit: str
    source_text: str
    source_page: int
    confidence: float
    extraction_metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_wheelchair_rules(pdf_path: Path, wheelchair_pages_only: bool = True) -> list[CanonicalRule]:
    """Compatibility wrapper for the full document-ingestion extraction path."""
    from .document_intelligence import ingest_pdf
    from .requirement_extraction import RequirementExtractionAgent

    document = ingest_pdf(pdf_path)
    return RequirementExtractionAgent().extract(document, wheelchair_pages_only=wheelchair_pages_only)


def save_canonical_rules(rules: list[CanonicalRule], output_path: Path, source_pdf: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "source_pdf": str(source_pdf),
                "rule_count": len(rules),
                "rules": [rule.to_dict() for rule in rules],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
