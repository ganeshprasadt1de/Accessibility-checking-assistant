from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from rdflib import Graph, Literal, Namespace

from .governance import PublicationCheck, publication_inventory, unresolved_conflicts, unresolved_duplicates
from .review_workflow import ApprovedRule

ACC = Namespace("https://example.org/wheelchair-accessibility#")


@dataclass(frozen=True)
class PublishedRuleLibrary:
    approved_rule_count: int
    shacl_shapes_path: str
    validation_status: str
    rules: list[dict]
    publication_inventory: dict


class ApprovedRuleLibrary:
    name = "approved_rule_library"

    def publish(
        self,
        approved_rules: list[ApprovedRule],
        shacl_shapes_path: Path,
        output_path: Path,
        governance_audit: dict | None = None,
        min_confidence: float = 0.5,
        human_review_audit: dict | None = None,
        mappings: list | None = None,
    ) -> PublishedRuleLibrary:
        checks = self._publication_checks(approved_rules, governance_audit or {}, min_confidence, human_review_audit or {}, mappings or [])
        failed = [check for check in checks if not check.passed]
        if failed:
            inventory = publication_inventory("failed", checks)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps({"publication_inventory": inventory}, indent=2, ensure_ascii=False), encoding="utf-8")
            raise RuntimeError("Rule library publication failed: " + "; ".join(check.detail for check in failed))
        self._assert_parseable_shacl(shacl_shapes_path)
        self._annotate_shacl_provenance(shacl_shapes_path, approved_rules)
        library = PublishedRuleLibrary(
            approved_rule_count=len(approved_rules),
            shacl_shapes_path=str(shacl_shapes_path),
            validation_status="shape_graph_parseable",
            rules=[
                {
                    "rule": item.rule.to_dict(),
                    "review_decision": asdict(item.decision),
                }
                for item in approved_rules
            ],
            publication_inventory=publication_inventory("published", checks),
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(asdict(library), indent=2, ensure_ascii=False), encoding="utf-8")
        return library

    def _publication_checks(
        self,
        approved_rules: list[ApprovedRule],
        governance_audit: dict,
        min_confidence: float,
        human_review_audit: dict,
        mappings: list,
    ) -> list[PublicationCheck]:
        checks: list[PublicationCheck] = []
        checks.append(PublicationCheck("approved_review_exists", bool(approved_rules), "approved review records are required"))
        checks.append(PublicationCheck("review_queue_exists", bool(human_review_audit.get("review_queue")), "human review queue records are required"))
        checks.append(PublicationCheck("decision_records_exist", bool(human_review_audit.get("decision_records")), "human review decision records are required"))
        unresolved = unresolved_conflicts(governance_audit)
        checks.append(PublicationCheck("no_unresolved_conflicts", not unresolved, f"unresolved conflicts: {len(unresolved)}"))
        duplicate_unresolved = unresolved_duplicates(governance_audit)
        checks.append(
            PublicationCheck(
                "no_unresolved_duplicate_groups",
                not duplicate_unresolved,
                f"unresolved duplicate groups: {len(duplicate_unresolved)}",
            )
        )
        for item in approved_rules:
            metadata = item.rule.extraction_metadata
            selection_scores = metadata.get("selection_scores", {})
            prefix = item.rule.rule_id
            rule_reviews = _queue_items(human_review_audit, prefix, "RULE")
            mapping_reviews = _queue_items(human_review_audit, prefix, "MAPPING")
            duplicate_reviews = _queue_items(human_review_audit, prefix, "DUPLICATE_GROUP")
            conflict_reviews = _queue_items(human_review_audit, prefix, "CONFLICT_GROUP")
            approved_mapping_ids = {review.get("mapping_id") for review in mapping_reviews if review.get("review_state") == "APPROVED"}
            reviewed_mapping = next((mapping for mapping in mappings if getattr(mapping, "rule_id", "") == prefix), None)
            checks.append(
                PublicationCheck(
                    f"{prefix}:approved_review_record",
                    item.decision.status == "APPROVED"
                    and bool(item.decision.reviewer)
                    and item.decision.reviewer != "preprocess-reviewer"
                    and bool(item.decision.rationale)
                    and item.decision.candidate_id == metadata.get("candidate_id"),
                    f"{prefix} must have explicit approval tied to candidate",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:evidence_exists",
                    bool(item.rule.source_text) and bool(metadata.get("evidence_text")),
                    f"{prefix} must retain source text and evidence text",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:source_chunk_exists",
                    bool(metadata.get("evidence_chunk_id")) and metadata.get("evidence_chunk_id") in item.decision.evidence_references,
                    f"{prefix} must reference a reviewed source chunk",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:confidence_threshold",
                    item.rule.confidence >= min_confidence,
                    f"{prefix} confidence {item.rule.confidence:.3f} must be >= {min_confidence:.3f}",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:provenance_complete",
                    self._has_complete_provenance(item, selection_scores),
                    f"{prefix} must include complete candidate governance provenance",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:review_identity_complete",
                    bool(item.decision.review_id) and bool(item.decision.decision_id),
                    f"{prefix} must include review_id and decision_id",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:rule_review_approved",
                    any(review.get("review_state") == "APPROVED" for review in rule_reviews),
                    f"{prefix} must have an approved rule review queue item",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:mapping_review_approved",
                    reviewed_mapping is not None
                    and reviewed_mapping.mapping_candidate_id in approved_mapping_ids
                    and reviewed_mapping.review_decision.get("status") == "APPROVED",
                    f"{prefix} must have an approved mapping review",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:duplicate_review_resolved",
                    not duplicate_reviews or all(review.get("review_state") == "APPROVED" for review in duplicate_reviews),
                    f"{prefix} duplicate groups must be reviewed",
                )
            )
            checks.append(
                PublicationCheck(
                    f"{prefix}:conflict_review_resolved",
                    not conflict_reviews or all(review.get("review_state") == "APPROVED" for review in conflict_reviews),
                    f"{prefix} conflict groups must be reviewed",
                )
            )
        return checks

    def _has_complete_provenance(self, item: ApprovedRule, selection_scores: dict) -> bool:
        metadata = item.rule.extraction_metadata
        required_metadata = [
            "candidate_id",
            "evidence_chunk_id",
            "evidence_text",
            "retrieval_rank",
            "selection_score",
            "selection_explanation",
            "duplicate_group_id",
        ]
        if any(metadata.get(key) is None or metadata.get(key) == "" for key in required_metadata):
            return False
        if item.rule.source_page is None or item.rule.source_text == "" or item.rule.confidence is None:
            return False
        has_retrieval_score = (
            selection_scores.get("retrieval_support_score") is not None
            or metadata.get("retrieval_score") is not None
        )
        if not has_retrieval_score:
            return False
        return (
            item.decision.candidate_id == metadata.get("candidate_id")
            and bool(item.decision.evidence_references)
            and str(metadata.get("evidence_chunk_id")) in item.decision.evidence_references
        )

    def _assert_parseable_shacl(self, shacl_shapes_path: Path) -> None:
        graph = Graph()
        try:
            graph.parse(shacl_shapes_path, format="turtle")
        except Exception as exc:
            raise RuntimeError(f"Generated SHACL is invalid and cannot be published: {exc}") from exc

    def _annotate_shacl_provenance(self, shacl_shapes_path: Path, approved_rules: list[ApprovedRule]) -> None:
        graph = Graph()
        graph.parse(shacl_shapes_path, format="turtle")
        graph.bind("acc", ACC)
        shapes = list(graph.subjects(ACC.generatedFromRule, None))
        for approved in approved_rules:
            metadata = approved.rule.extraction_metadata
            for shape in shapes:
                generated_from = str(graph.value(shape, ACC.generatedFromRule) or "")
                referenced = {part.strip() for part in generated_from.split(",")}
                if approved.rule.rule_id not in referenced:
                    continue
                graph.add((shape, ACC.candidateId, Literal(str(metadata.get("candidate_id")))))
                graph.add((shape, ACC.sourceChunkId, Literal(str(metadata.get("evidence_chunk_id")))))
                graph.add((shape, ACC.sourcePage, Literal(approved.rule.source_page)))
                graph.add((shape, ACC.reviewDecision, Literal(approved.decision.status)))
                graph.add((shape, ACC.reviewId, Literal(approved.decision.review_id)))
                graph.add((shape, ACC.decisionId, Literal(approved.decision.decision_id)))
                graph.add((shape, ACC.approvalTimestamp, Literal(approved.decision.review_timestamp)))
                graph.add((shape, ACC.reviewedBy, Literal(approved.decision.reviewer)))
                graph.add((shape, ACC.ruleConfidence, Literal(float(approved.rule.confidence))))
                graph.add((shape, ACC.retrievalScore, Literal(float(metadata.get("selection_scores", {}).get("retrieval_support_score", metadata.get("retrieval_score"))))))
                graph.add((shape, ACC.selectionScore, Literal(float(metadata.get("selection_score")))))
        graph.serialize(destination=shacl_shapes_path, format="turtle")


def _queue_items(human_review_audit: dict, rule_id: str, review_type: str) -> list[dict]:
    return [
        item
        for item in human_review_audit.get("review_queue", [])
        if item.get("rule_id") == rule_id and item.get("review_type") == review_type
    ]
