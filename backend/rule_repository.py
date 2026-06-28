from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .canonical_rules import CanonicalRule


class RuleRepository(Protocol):
    def save(self, rules: list[CanonicalRule], source_pdf: Path, metadata: dict, output_path: Path) -> None:
        ...

    def load(self, input_path: Path) -> list[CanonicalRule]:
        ...


@dataclass(frozen=True)
class JSONRuleRepository:
    def save(self, rules: list[CanonicalRule], source_pdf: Path, metadata: dict, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "source_pdf": str(source_pdf),
                    "rule_count": len(rules),
                    "metadata": metadata,
                    "rules": [rule.to_dict() for rule in rules],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def load(self, input_path: Path) -> list[CanonicalRule]:
        data = json.loads(input_path.read_text(encoding="utf-8"))
        return [CanonicalRule(**item) for item in data.get("rules", [])]
