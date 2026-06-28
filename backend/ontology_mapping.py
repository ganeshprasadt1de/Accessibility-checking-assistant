from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from rdflib import Graph, Literal as RDFLiteral, RDF, RDFS, URIRef

from .canonical_rules import CanonicalRule


MappingReviewStatus = Literal["PENDING", "APPROVED", "REJECTED", "ESCALATED", "SUPERSEDED", "ARCHIVED"]


@dataclass(frozen=True)
class MappingCandidate:
    candidate_id: str
    rule_id: str
    ifc_target: str
    ontology_class: str
    ontology_property: str
    target_type: str
    confidence: float
    scores: dict[str, float]
    evidence: dict
    reasoning: str
    source_entities: list[str]


@dataclass(frozen=True)
class MappingConflict:
    conflict_id: str
    rule_id: str
    candidate_ids: list[str]
    status: str
    rationale: str


@dataclass(frozen=True)
class MappingReviewDecision:
    mapping_candidate_id: str
    rule_id: str
    status: MappingReviewStatus
    reviewer: str
    timestamp: str
    rationale: str


@dataclass(frozen=True)
class OntologyMapping:
    rule_id: str
    entity: str
    property: str
    ontology_class: str
    ontology_property: str
    ifc_target: str
    mapping_candidate_id: str
    confidence: float
    scores: dict[str, float]
    evidence: dict
    reasoning: str
    review_decision: dict


class OntologyMappingAgent:
    name = "ontology_mapping_agent"

    def __init__(self) -> None:
        self.audit: dict = {}

    def map_rules(
        self,
        rules: list[CanonicalRule],
        data_graph: Graph,
    ) -> list[OntologyMapping]:
        profiles = _class_profiles(data_graph)
        all_candidates: list[MappingCandidate] = []
        selected: list[OntologyMapping] = []
        comparisons: list[dict] = []
        conflicts: list[MappingConflict] = []
        rejected: list[dict] = []
        unmapped: list[dict] = []

        for rule_index, rule in enumerate(rules, start=1):
            candidates = _mapping_candidates(rule, profiles, rule_index)
            all_candidates.extend(candidates)
            eligible = [item for item in candidates if item.evidence.get("semantic_gate_passed")]
            gate_rejected = [item for item in candidates if not item.evidence.get("semantic_gate_passed")]
            for item in gate_rejected:
                rejected.append(
                    {
                        "rule_id": rule.rule_id,
                        "mapping_candidate_id": item.candidate_id,
                        "ifc_target": item.ifc_target,
                        "ontology_property": item.ontology_property,
                        "reason": item.evidence.get("semantic_gate_reason", "semantic gate rejected this candidate"),
                    }
                )
            if not eligible:
                unmapped.append(
                    {
                        "rule_id": rule.rule_id,
                        "entity": rule.entity,
                        "property": rule.property,
                        "reason": "no runtime IFC class/property evidence passed the generic semantic mapping gate",
                    }
                )
                continue
            ranked = sorted(eligible, key=lambda item: item.confidence, reverse=True)
            winner = ranked[0]
            comparisons.extend(_pairwise_comparisons(rule.rule_id, ranked))
            conflict = _mapping_conflict(rule.rule_id, ranked)
            if conflict:
                conflicts.append(conflict)
            for loser in ranked[1:]:
                rejected.append(
                    {
                        "rule_id": rule.rule_id,
                        "mapping_candidate_id": loser.candidate_id,
                        "ifc_target": loser.ifc_target,
                        "ontology_property": loser.ontology_property,
                        "reason": f"lower evidence score than {winner.candidate_id}",
                    }
                )
            selected.append(
                OntologyMapping(
                    rule_id=rule.rule_id,
                    entity=rule.entity,
                    property=rule.property,
                    ontology_class=winner.ontology_class,
                    ontology_property=winner.ontology_property,
                    ifc_target=winner.ifc_target,
                    mapping_candidate_id=winner.candidate_id,
                    confidence=winner.confidence,
                    scores=winner.scores,
                    evidence=winner.evidence,
                    reasoning=winner.reasoning,
                    review_decision={
                        "mapping_candidate_id": winner.candidate_id,
                        "rule_id": rule.rule_id,
                        "status": "PENDING",
                        "reviewer": "",
                        "timestamp": "",
                        "rationale": "Mapping selected by scoring and waiting for human review.",
                    },
                )
            )

        self.audit = {
            "agent": self.name,
            "created_at": _now(),
            "candidate_count": len(all_candidates),
            "selected_count": len(selected),
            "all_mapping_candidates": [asdict(item) for item in all_candidates],
            "all_scores": [
                {
                    "mapping_candidate_id": item.candidate_id,
                    "rule_id": item.rule_id,
                    "scores": item.scores,
                    "final_score": item.confidence,
                }
                for item in all_candidates
            ],
            "pairwise_comparisons": comparisons,
            "conflicts": [asdict(item) for item in conflicts],
            "selected_mappings": [asdict(item) for item in selected],
            "rejected_mappings": rejected,
            "unmapped_rules": unmapped,
            "review_decisions": [],
            "runtime_discovery": {
                "class_profile_count": len(profiles),
                "classes": [
                    {
                        "class_uri": profile["class_uri"],
                        "subject_count": profile["subject_count"],
                        "numeric_predicate_count": len(profile["numeric_predicates"]),
                    }
                    for profile in profiles
                ],
            },
        }
        return selected


def save_mappings(mappings: list[OntologyMapping], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([asdict(item) for item in mappings], indent=2, ensure_ascii=False), encoding="utf-8")


def save_mapping_audit(audit: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_mapping_reviews(mappings: list[OntologyMapping], decisions: list) -> list[OntologyMapping]:
    by_mapping_id = {
        decision.mapping_id: decision
        for decision in decisions
        if getattr(decision, "mapping_id", "") and getattr(decision, "status", "") == "APPROVED"
    }
    reviewed = []
    for item in mappings:
        decision = by_mapping_id.get(item.mapping_candidate_id)
        if decision is None:
            continue
        reviewed.append(
            OntologyMapping(
                rule_id=item.rule_id,
                entity=item.entity,
                property=item.property,
                ontology_class=item.ontology_class,
                ontology_property=item.ontology_property,
                ifc_target=item.ifc_target,
                mapping_candidate_id=item.mapping_candidate_id,
                confidence=item.confidence,
                scores=item.scores,
                evidence=item.evidence,
                reasoning=item.reasoning,
                review_decision={
                    "mapping_candidate_id": item.mapping_candidate_id,
                    "rule_id": item.rule_id,
                    "status": decision.status,
                    "reviewer": decision.reviewer,
                    "timestamp": decision.review_timestamp,
                    "rationale": decision.rationale,
                    "review_id": decision.review_id,
                    "decision_id": decision.decision_id,
                },
            )
        )
    return reviewed


def _class_profiles(data_graph: Graph) -> list[dict]:
    profiles: dict[str, dict] = {}
    for subject, class_uri in data_graph.subject_objects(RDF.type):
        class_text = str(class_uri)
        item = profiles.setdefault(
            class_text,
            {
                "class_uri": class_text,
                "subjects": set(),
                "labels": [],
                "numeric_predicates": {},
                "all_predicates": {},
            },
        )
        item["subjects"].add(str(subject))
    for class_text, item in profiles.items():
        for subject_text in sorted(item["subjects"]):
            subject = URIRef(subject_text)
            label = data_graph.value(subject, RDFS.label)
            if label is not None:
                item["labels"].append(str(label))
            for predicate, value in data_graph.predicate_objects(subject):
                predicate_text = str(predicate)
                item["all_predicates"][predicate_text] = item["all_predicates"].get(predicate_text, 0) + 1
                if _is_numeric_literal(value):
                    bucket = item["numeric_predicates"].setdefault(predicate_text, {"count": 0, "examples": []})
                    bucket["count"] += 1
                    if len(bucket["examples"]) < 5:
                        bucket["examples"].append(str(value))
    result = []
    for item in profiles.values():
        result.append(
            {
                "class_uri": item["class_uri"],
                "subjects": sorted(item["subjects"]),
                "subject_count": len(item["subjects"]),
                "labels": item["labels"][:20],
                "numeric_predicates": item["numeric_predicates"],
                "all_predicates": item["all_predicates"],
            }
        )
    return sorted(result, key=lambda profile: profile["class_uri"])


def _mapping_candidates(rule: CanonicalRule, profiles: list[dict], rule_index: int) -> list[MappingCandidate]:
    candidates: list[MappingCandidate] = []
    for profile in profiles:
        for predicate_uri, predicate_info in profile["numeric_predicates"].items():
            scores = _score_mapping(rule, profile, predicate_uri, predicate_info)
            final_score = _weighted_score(scores)
            if final_score <= 0.05:
                continue
            gate_passed, gate_reason = _semantic_gate(scores)
            candidate_id = f"M{rule_index:03d}-{len(candidates) + 1:03d}"
            class_uri = profile["class_uri"]
            evidence = {
                "class_uri": class_uri,
                "predicate_uri": predicate_uri,
                "subject_count": profile["subject_count"],
                "predicate_count": predicate_info["count"],
                "sample_values": predicate_info["examples"],
                "sample_labels": profile["labels"][:5],
                "semantic_gate_passed": gate_passed,
                "semantic_gate_reason": gate_reason,
            }
            candidates.append(
                MappingCandidate(
                    candidate_id=candidate_id,
                    rule_id=rule.rule_id,
                    ifc_target=class_uri,
                    ontology_class=class_uri,
                    ontology_property=predicate_uri,
                    target_type="rdf_class_property",
                    confidence=round(final_score, 6),
                    scores={key: round(value, 6) for key, value in scores.items()},
                    evidence=evidence,
                    reasoning=_mapping_reason(rule, class_uri, predicate_uri, scores),
                    source_entities=profile["subjects"][:10],
                )
            )
    return sorted(candidates, key=lambda item: item.confidence, reverse=True)


def _semantic_gate(scores: dict[str, float]) -> tuple[bool, str]:
    if scores["property_compatibility"] < 0.30:
        return False, "property compatibility below generic threshold"
    if scores["target_semantic_relevance"] <= 0.0 and scores["property_compatibility"] < 0.60:
        return False, "target evidence missing and property compatibility is not strong enough"
    return True, "passed generic semantic evidence gate"


def _score_mapping(rule: CanonicalRule, profile: dict, predicate_uri: str, predicate_info: dict) -> dict[str, float]:
    rule_tokens = _rule_tokens(rule)
    class_tokens = _tokens(_local_name(profile["class_uri"]))
    property_tokens = _tokens(_local_name(predicate_uri))
    label_tokens = _tokens(" ".join(profile["labels"][:20]))
    target_tokens = class_tokens | label_tokens
    graph_support = min(1.0, math.log1p(float(predicate_info["count"])) / math.log1p(max(float(profile["subject_count"]), 1.0)))
    retrieval_support = _retrieval_support(rule)
    return {
        "target_semantic_relevance": _overlap(rule_tokens, target_tokens),
        "semantic_relevance": _overlap(rule_tokens, class_tokens | property_tokens | label_tokens),
        "ontology_consistency": min(1.0, float(predicate_info["count"]) / max(float(profile["subject_count"]), 1.0)),
        "property_compatibility": _overlap(_tokens(rule.property) | _tokens(rule.rule_id), property_tokens),
        "measurement_compatibility": _measurement_compatibility(rule, property_tokens, predicate_info),
        "graph_support": graph_support,
        "retrieval_support": retrieval_support,
    }


def _weighted_score(scores: dict[str, float]) -> float:
    weights = {
        "semantic_relevance": 0.25,
        "ontology_consistency": 0.15,
        "property_compatibility": 0.25,
        "measurement_compatibility": 0.15,
        "graph_support": 0.15,
        "retrieval_support": 0.05,
    }
    return sum(scores[key] * weight for key, weight in weights.items())


def _mapping_reason(rule: CanonicalRule, class_uri: str, predicate_uri: str, scores: dict[str, float]) -> str:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    evidence = ", ".join(f"{name}={value:.3f}" for name, value in ordered[:3])
    return (
        f"Rule {rule.rule_id} was mapped to {_local_name(class_uri)} / {_local_name(predicate_uri)} "
        f"from runtime RDF evidence. Strongest signals: {evidence}."
    )


def _pairwise_comparisons(rule_id: str, ranked: list[MappingCandidate]) -> list[dict]:
    if not ranked:
        return []
    winner = ranked[0]
    rows = []
    for loser in ranked[1:]:
        delta = round(winner.confidence - loser.confidence, 6)
        rows.append(
            {
                "rule_id": rule_id,
                "winner": winner.candidate_id,
                "loser": loser.candidate_id,
                "score_delta": delta,
                "reason": _comparison_reason(winner, loser),
                "supporting_evidence": {
                    "winner": winner.evidence,
                    "loser": loser.evidence,
                },
            }
        )
    return rows


def _comparison_reason(winner: MappingCandidate, loser: MappingCandidate) -> str:
    better = [
        key
        for key, value in winner.scores.items()
        if value > loser.scores.get(key, 0.0)
    ]
    if not better:
        return "winner selected by higher final score with similar component scores"
    return "winner has stronger " + ", ".join(better[:3])


def _mapping_conflict(rule_id: str, ranked: list[MappingCandidate]) -> MappingConflict | None:
    if len(ranked) < 2:
        return None
    top = ranked[0]
    competing = [
        item
        for item in ranked[1:]
        if item.confidence >= 0.35 and top.confidence - item.confidence <= 0.15
    ]
    if not competing:
        return None
    return MappingConflict(
        conflict_id=f"mapping-conflict-{rule_id}",
        rule_id=rule_id,
        candidate_ids=[top.candidate_id] + [item.candidate_id for item in competing],
        status="unresolved",
        rationale=f"Competing mappings require explicit review before {top.candidate_id} can be published.",
    )


def _rule_tokens(rule: CanonicalRule) -> set[str]:
    return _tokens(" ".join([rule.rule_id, rule.entity, rule.property, rule.unit]))


def _tokens(text: str) -> set[str]:
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", split.lower())
        if len(token) >= 2 and token not in {"http", "https", "www", "org", "com", "the", "and", "for"}
    }


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _measurement_compatibility(rule: CanonicalRule, property_tokens: set[str], predicate_info: dict) -> float:
    examples = []
    for raw in predicate_info.get("examples", []):
        try:
            examples.append(abs(float(raw)))
        except (TypeError, ValueError):
            continue
    numeric_signal = 1.0 if examples else 0.0
    unit_tokens = _tokens(rule.unit)
    unit_signal = _overlap(unit_tokens, property_tokens) if unit_tokens else 0.5
    if rule.unit == "%":
        unit_signal = max(unit_signal, 1.0 if any(value <= 100 for value in examples) else 0.0)
    return min(1.0, (numeric_signal * 0.7) + (unit_signal * 0.3))


def _retrieval_support(rule: CanonicalRule) -> float:
    scores = rule.extraction_metadata.get("selection_scores", {})
    value = scores.get("retrieval_support_score", rule.extraction_metadata.get("retrieval_score", 0.0))
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _is_numeric_literal(value) -> bool:
    if not isinstance(value, RDFLiteral):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _local_name(uri: str) -> str:
    text = str(uri).rstrip("/")
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    return text.rsplit("/", 1)[-1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
