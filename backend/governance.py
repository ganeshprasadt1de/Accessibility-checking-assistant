from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


ConflictStatus = Literal["unresolved", "accepted", "rejected", "escalated"]


@dataclass(frozen=True)
class ConflictRecord:
    conflict_id: str
    rule_id: str
    conflicting_candidates: list[str]
    supporting_evidence: list[dict]
    confidence_comparison: list[dict]
    resolution_status: ConflictStatus = "unresolved"


@dataclass(frozen=True)
class PublicationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PublicationInventory:
    status: str
    checks: list[PublicationCheck]


def unresolved_conflicts(governance_audit: dict | None) -> list[dict]:
    if not governance_audit:
        return []
    return [
        item
        for item in governance_audit.get("conflict_records", governance_audit.get("conflict_groups", []))
        if item.get("resolution_status", "unresolved") == "unresolved"
    ]


def unresolved_duplicates(governance_audit: dict | None) -> list[dict]:
    if not governance_audit:
        return []
    groups = governance_audit.get("duplicate_groups", governance_audit.get("duplicate_inventory", []))
    unresolved: list[dict] = []
    for group in groups:
        if group.get("resolution_status") == "unresolved":
            unresolved.append(group)
            continue
        if group.get("members") and not group.get("selected_candidate"):
            unresolved.append(group)
            continue
        if group.get("members") and not group.get("merge_rationale"):
            unresolved.append(group)
    return unresolved


def save_governance_audit(
    output_path: Path,
    extraction_audit: dict,
    review_audit: dict,
    publication_inventory: dict,
) -> None:
    governance = extraction_audit.get("candidate_governance", {})
    payload = {
        "candidate_history": governance.get("candidates", []),
        "duplicate_inventory": governance.get("duplicate_groups", []),
        "conflict_inventory": governance.get("conflict_records", governance.get("conflict_groups", [])),
        "review_inventory": review_audit,
        "publication_inventory": publication_inventory,
        "publication_checks": publication_inventory.get("checks", []),
        "provenance_completeness": _provenance_completeness(review_audit),
        "decision_chain": {
            "candidate_count": governance.get("candidate_count", 0),
            "selected_candidate_count": governance.get("selected_candidate_count", 0),
            "approved_count": len(review_audit.get("approved", [])),
            "rejected_count": len(review_audit.get("rejected", [])),
            "publication_status": publication_inventory.get("status", "unknown"),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def publication_inventory(status: str, checks: list[PublicationCheck]) -> dict:
    return asdict(PublicationInventory(status=status, checks=checks))


def _provenance_completeness(review_audit: dict) -> list[dict]:
    records = []
    required_metadata = [
        "candidate_id",
        "evidence_chunk_id",
        "evidence_text",
        "retrieval_rank",
        "selection_score",
        "selection_explanation",
        "duplicate_group_id",
    ]
    for item in review_audit.get("approved", []):
        rule = item.get("rule", {})
        metadata = rule.get("extraction_metadata", {})
        missing = [
            key
            for key in required_metadata
            if metadata.get(key) is None or metadata.get(key) == ""
        ]
        if not rule.get("source_page"):
            missing.append("source_page")
        if not rule.get("source_text"):
            missing.append("source_text")
        if rule.get("confidence") is None:
            missing.append("confidence")
        selection_scores = metadata.get("selection_scores", {})
        if selection_scores.get("retrieval_support_score") is None and metadata.get("retrieval_score") is None:
            missing.append("retrieval_score")
        decision = item.get("decision", {})
        if not decision.get("candidate_id"):
            missing.append("review_candidate_id")
        if not decision.get("evidence_references"):
            missing.append("review_evidence_references")
        records.append(
            {
                "rule_id": rule.get("rule_id"),
                "candidate_id": metadata.get("candidate_id"),
                "complete": not missing,
                "missing": missing,
            }
        )
    return records
