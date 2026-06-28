from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .canonical_rules import CanonicalRule


ReviewState = Literal["PENDING", "APPROVED", "REJECTED", "ESCALATED", "SUPERSEDED", "ARCHIVED"]
ReviewType = Literal["RULE", "MAPPING", "DUPLICATE_GROUP", "CONFLICT_GROUP"]


@dataclass(frozen=True)
class ReviewQueueItem:
    review_id: str
    review_type: ReviewType
    candidate_id: str
    rule_id: str
    mapping_id: str
    evidence_chunk: str
    source_page: int | None
    confidence: float | None
    retrieval_score: float | None
    selection_score: float | None
    review_state: ReviewState = "PENDING"
    group_id: str = ""
    group_members: list[str] | None = None
    comparison_rationale: str = ""


@dataclass(frozen=True)
class ReviewDecision:
    decision_id: str
    review_id: str
    rule_id: str
    status: ReviewState
    reviewer: str
    rationale: str
    review_timestamp: str
    candidate_id: str
    mapping_id: str
    evidence_references: list[str]
    attachments: list[str] | None = None
    notes: str = ""

    @property
    def reviewer_notes(self) -> str:
        return self.rationale


@dataclass(frozen=True)
class GovernanceEvent:
    event_id: str
    timestamp: str
    actor: str
    event_type: str
    artifact: dict


@dataclass(frozen=True)
class PendingRule:
    rule: CanonicalRule
    queue_item: ReviewQueueItem
    status: ReviewState = "PENDING"


@dataclass(frozen=True)
class ApprovedRule:
    rule: CanonicalRule
    decision: ReviewDecision


@dataclass(frozen=True)
class RejectedRule:
    rule: CanonicalRule
    decision: ReviewDecision


@dataclass(frozen=True)
class EscalatedRule:
    rule: CanonicalRule
    decision: ReviewDecision


class HumanReviewWorkflow:
    def __init__(self) -> None:
        self.events: list[GovernanceEvent] = []
        self.queue: list[ReviewQueueItem] = []
        self.decisions: list[ReviewDecision] = []

    def create_queue(self, rules: list[CanonicalRule], mappings: list, candidate_governance: dict, mapping_audit: dict) -> list[ReviewQueueItem]:
        mapping_by_rule = {item.rule_id: item for item in mappings}
        queue: list[ReviewQueueItem] = []
        for index, rule in enumerate(rules, start=1):
            metadata = rule.extraction_metadata
            mapping = mapping_by_rule.get(rule.rule_id)
            queue.append(
                ReviewQueueItem(
                    review_id=f"RV-RULE-{index:04d}",
                    review_type="RULE",
                    candidate_id=str(metadata.get("candidate_id", "")),
                    rule_id=rule.rule_id,
                    mapping_id=str(getattr(mapping, "mapping_candidate_id", "")),
                    evidence_chunk=str(metadata.get("evidence_chunk_id", "")),
                    source_page=rule.source_page,
                    confidence=rule.confidence,
                    retrieval_score=_retrieval_score(rule),
                    selection_score=_selection_score(rule),
                )
            )
            if mapping is not None:
                queue.append(
                    ReviewQueueItem(
                        review_id=f"RV-MAP-{index:04d}",
                        review_type="MAPPING",
                        candidate_id=str(metadata.get("candidate_id", "")),
                        rule_id=rule.rule_id,
                        mapping_id=str(mapping.mapping_candidate_id),
                        evidence_chunk=str(metadata.get("evidence_chunk_id", "")),
                        source_page=rule.source_page,
                        confidence=float(mapping.confidence),
                        retrieval_score=_retrieval_score(rule),
                        selection_score=_selection_score(rule),
                        comparison_rationale=str(mapping.reasoning),
                    )
                )
        for index, group in enumerate(candidate_governance.get("duplicate_groups", []), start=1):
            queue.append(
                ReviewQueueItem(
                    review_id=f"RV-DUP-{index:04d}",
                    review_type="DUPLICATE_GROUP",
                    candidate_id=str(group.get("selected_candidate", "")),
                    rule_id=str(group.get("rule_id", "")),
                    mapping_id="",
                    evidence_chunk="",
                    source_page=None,
                    confidence=None,
                    retrieval_score=None,
                    selection_score=None,
                    group_id=str(group.get("duplicate_group_id", "")),
                    group_members=list(group.get("group_members", [])),
                    comparison_rationale=str(group.get("merge_rationale", "")),
                )
            )
        conflict_sources = list(candidate_governance.get("conflict_records", [])) + list(mapping_audit.get("conflicts", []))
        for index, group in enumerate(conflict_sources, start=1):
            queue.append(
                ReviewQueueItem(
                    review_id=f"RV-CONF-{index:04d}",
                    review_type="CONFLICT_GROUP",
                    candidate_id=str((group.get("conflicting_candidates") or group.get("candidate_ids") or [""])[0]),
                    rule_id=str(group.get("rule_id", "")),
                    mapping_id="",
                    evidence_chunk="",
                    source_page=None,
                    confidence=None,
                    retrieval_score=None,
                    selection_score=None,
                    group_id=str(group.get("conflict_id", "")),
                    group_members=list(group.get("conflicting_candidates") or group.get("candidate_ids") or []),
                    comparison_rationale=str(group.get("rationale", group.get("conflict_type", ""))),
                )
            )
        self.queue = queue
        for item in queue:
            self._event("system", "review_queued", asdict(item))
        return queue

    def create_pending(self, rules: list[CanonicalRule]) -> list[PendingRule]:
        queue = [
            ReviewQueueItem(
                review_id=f"RV-RULE-{index:04d}",
                review_type="RULE",
                candidate_id=str(rule.extraction_metadata.get("candidate_id", "")),
                rule_id=rule.rule_id,
                mapping_id="",
                evidence_chunk=str(rule.extraction_metadata.get("evidence_chunk_id", "")),
                source_page=rule.source_page,
                confidence=rule.confidence,
                retrieval_score=_retrieval_score(rule),
                selection_score=_selection_score(rule),
            )
            for index, rule in enumerate(rules, start=1)
        ]
        self.queue = queue
        return [PendingRule(rule=rule, queue_item=item) for rule, item in zip(rules, queue)]

    def decide(self, item: ReviewQueueItem, decision: ReviewState, reviewer: str, rationale: str, attachments: list[str] | None = None, notes: str = "") -> tuple[ReviewQueueItem, ReviewDecision]:
        _assert_decision(decision, reviewer, rationale)
        _validate_transition(item.review_state, decision)
        updated = replace(item, review_state=decision)
        record = ReviewDecision(
            decision_id=f"DEC-{len(self.decisions) + 1:05d}",
            review_id=item.review_id,
            rule_id=item.rule_id,
            status=decision,
            reviewer=reviewer.strip(),
            rationale=rationale.strip(),
            review_timestamp=_now(),
            candidate_id=item.candidate_id,
            mapping_id=item.mapping_id,
            evidence_references=_evidence_references(item),
            attachments=attachments or [],
            notes=notes,
        )
        self.decisions.append(record)
        self.queue = [updated if queued.review_id == item.review_id else queued for queued in self.queue]
        self._event(reviewer.strip(), f"review_{decision.lower()}", {"queue_item": asdict(updated), "decision": asdict(record)})
        return updated, record

    def approve(self, pending: PendingRule, reviewer: str, rationale: str) -> ApprovedRule:
        updated, decision = self.decide(pending.queue_item, "APPROVED", reviewer, rationale)
        return ApprovedRule(rule=pending.rule, decision=decision)

    def reject(self, pending: PendingRule, reviewer: str, notes: str) -> RejectedRule:
        _updated, decision = self.decide(pending.queue_item, "REJECTED", reviewer, notes)
        return RejectedRule(rule=pending.rule, decision=decision)

    def escalate(self, pending: PendingRule, reviewer: str, rationale: str) -> EscalatedRule:
        _updated, decision = self.decide(pending.queue_item, "ESCALATED", reviewer, rationale)
        return EscalatedRule(rule=pending.rule, decision=decision)

    def apply_cli_decisions(
        self,
        rules: list[CanonicalRule],
        reviewer: str,
        rationale: str,
        reject_rule_ids: set[str] | None = None,
        escalate_rule_ids: set[str] | None = None,
    ) -> tuple[list[ApprovedRule], list[RejectedRule], list[EscalatedRule]]:
        if not self.queue:
            raise RuntimeError("Review decisions require a persisted review queue.")
        _assert_decision("APPROVED", reviewer, rationale)
        reject_rule_ids = reject_rule_ids or set()
        escalate_rule_ids = escalate_rule_ids or set()
        rule_by_id = {rule.rule_id: rule for rule in rules}
        approved: list[ApprovedRule] = []
        rejected: list[RejectedRule] = []
        escalated: list[EscalatedRule] = []
        for item in list(self.queue):
            if item.review_type != "RULE":
                continue
            rule = rule_by_id.get(item.rule_id)
            if rule is None:
                continue
            if item.rule_id in reject_rule_ids:
                updated, decision = self.decide(item, "REJECTED", reviewer, rationale)
                rejected.append(RejectedRule(rule=rule, decision=decision))
            elif item.rule_id in escalate_rule_ids or not item.mapping_id:
                updated, decision = self.decide(item, "ESCALATED", reviewer, rationale)
                escalated.append(EscalatedRule(rule=rule, decision=decision))
            else:
                updated, decision = self.decide(item, "APPROVED", reviewer, rationale)
                approved.append(ApprovedRule(rule=rule, decision=decision))
        for item in list(self.queue):
            if item.review_type == "RULE":
                continue
            if item.rule_id in reject_rule_ids:
                self.decide(item, "REJECTED", reviewer, rationale)
            elif item.rule_id in escalate_rule_ids or (item.review_type == "MAPPING" and not item.mapping_id):
                self.decide(item, "ESCALATED", reviewer, rationale)
            else:
                self.decide(item, "APPROVED", reviewer, rationale)
        return approved, rejected, escalated

    def save(
        self,
        approved: list[ApprovedRule],
        rejected: list[RejectedRule],
        escalated: list[EscalatedRule],
        output_path: Path,
    ) -> dict:
        payload = human_review_audit(self.queue, self.decisions, approved, rejected, escalated)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload

    def save_event_log(self, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps([asdict(item) for item in self.events], indent=2, ensure_ascii=False), encoding="utf-8")

    def record_event(self, actor: str, event_type: str, artifact: dict) -> None:
        self._event(actor, event_type, artifact)

    def _event(self, actor: str, event_type: str, artifact: dict) -> None:
        self.events.append(
            GovernanceEvent(
                event_id=f"GE-{len(self.events) + 1:06d}",
                timestamp=_now(),
                actor=actor,
                event_type=event_type,
                artifact=artifact,
            )
        )


def require_approved_rules(approved: list[ApprovedRule]) -> list[CanonicalRule]:
    if not approved:
        raise RuntimeError("No approved canonical rules are available for SHACL generation.")
    invalid = [
        item.rule.rule_id
        for item in approved
        if item.decision.status != "APPROVED"
        or not item.decision.reviewer.strip()
        or item.decision.reviewer == "preprocess-reviewer"
        or not item.decision.rationale.strip()
        or not item.decision.candidate_id
        or not item.decision.review_id
        or not item.decision.decision_id
        or not item.decision.evidence_references
    ]
    if invalid:
        raise RuntimeError(f"Approved rules are missing authentic review records: {', '.join(invalid)}")
    return [item.rule for item in approved]


def human_review_audit(
    queue: list[ReviewQueueItem],
    decisions: list[ReviewDecision],
    approved: list[ApprovedRule],
    rejected: list[RejectedRule],
    escalated: list[EscalatedRule],
) -> dict:
    queue_rows = [asdict(item) for item in queue]
    decision_rows = [asdict(item) for item in decisions]
    return {
        "pending_reviews": [item for item in queue_rows if item["review_state"] == "PENDING"],
        "approved_reviews": [item for item in queue_rows if item["review_state"] == "APPROVED"],
        "rejected_reviews": [item for item in queue_rows if item["review_state"] == "REJECTED"],
        "escalated_reviews": [item for item in queue_rows if item["review_state"] == "ESCALATED"],
        "superseded_reviews": [item for item in queue_rows if item["review_state"] == "SUPERSEDED"],
        "archived_reviews": [item for item in queue_rows if item["review_state"] == "ARCHIVED"],
        "review_queue": queue_rows,
        "decision_records": decision_rows,
        "approved": [{"rule": item.rule.to_dict(), "decision": asdict(item.decision)} for item in approved],
        "rejected": [{"rule": item.rule.to_dict(), "decision": asdict(item.decision)} for item in rejected],
        "escalated": [{"rule": item.rule.to_dict(), "decision": asdict(item.decision)} for item in escalated],
        "duplicate_groups": [item for item in queue_rows if item["review_type"] == "DUPLICATE_GROUP"],
        "conflict_groups": [item for item in queue_rows if item["review_type"] == "CONFLICT_GROUP"],
        "publication_decisions": [],
        "publication_failures": [],
    }


def _assert_decision(decision: ReviewState, reviewer: str, rationale: str) -> None:
    if decision not in {"APPROVED", "REJECTED", "ESCALATED", "SUPERSEDED", "ARCHIVED"}:
        raise RuntimeError(f"Unsupported review decision: {decision}")
    if not reviewer or not reviewer.strip():
        raise RuntimeError("Reviewer is required for review decisions.")
    if reviewer == "preprocess-reviewer":
        raise RuntimeError("preprocess-reviewer is not an authentic human reviewer.")
    if not rationale or not rationale.strip():
        raise RuntimeError("Review rationale is required for review decisions.")


def _validate_transition(current: ReviewState, target: ReviewState) -> None:
    allowed = {
        "PENDING": {"APPROVED", "REJECTED", "ESCALATED", "ARCHIVED"},
        "ESCALATED": {"APPROVED", "REJECTED", "ARCHIVED"},
        "APPROVED": {"SUPERSEDED", "ARCHIVED"},
        "REJECTED": {"SUPERSEDED", "ARCHIVED"},
        "SUPERSEDED": {"ARCHIVED"},
        "ARCHIVED": set(),
    }
    if target not in allowed[current]:
        raise RuntimeError(f"Invalid review transition: {current} -> {target}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retrieval_score(rule: CanonicalRule) -> float | None:
    scores = rule.extraction_metadata.get("selection_scores", {})
    value = scores.get("retrieval_support_score", rule.extraction_metadata.get("retrieval_score"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _selection_score(rule: CanonicalRule) -> float | None:
    try:
        return float(rule.extraction_metadata.get("selection_score"))
    except (TypeError, ValueError):
        return None


def _evidence_references(item: ReviewQueueItem) -> list[str]:
    refs = []
    if item.evidence_chunk:
        refs.append(item.evidence_chunk)
    if item.source_page:
        refs.append(f"page:{item.source_page}")
    if item.group_id:
        refs.append(item.group_id)
    if item.mapping_id:
        refs.append(item.mapping_id)
    return refs
