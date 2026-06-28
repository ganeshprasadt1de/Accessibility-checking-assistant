from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .canonical_rules import CanonicalRule
from .document_intelligence import ImageChunk, StructuredDocument, TableChunk, TextChunk
from .governance import ConflictRecord
from .llm_provider import OllamaLLMProvider


_NORMATIVE_TERMS = (
    "shall",
    "must",
    "required",
    "requirement",
    "permitted",
    "prohibited",
    "should",
    "minimum",
    "maximum",
    "muss",
    "m?ssen",
    "muessen",
    "darf",
    "d?rfen",
    "duerfen",
    "zul?ssig",
    "zulaessig",
    "unzul?ssig",
    "unzulaessig",
    "erforderlich",
    "mindestens",
    "h?chstens",
    "hoechstens",
)
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "ist",
    "und",
    "der",
    "die",
    "das",
    "den",
    "dem",
    "ein",
    "eine",
    "einer",
    "von",
    "mit",
    "f?r",
    "fuer",
    "zur",
    "zum",
    "bei",
    "auf",
}

_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_RULE_SCHEMA = _ROOT / "rules" / "canonical_rule_schema.json"



@dataclass(frozen=True)
class RequirementEvidence:
    chunk_id: str
    chunk_type: str
    page_number: int
    text: str


@dataclass(frozen=True)
class ScoredEvidence:
    evidence: RequirementEvidence
    individual_scores: dict[str, float]
    final_score: float
    rank: int
    selected: bool
    selection_reason: str
    exclusion_reason: str

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.evidence.chunk_id,
            "chunk_type": self.evidence.chunk_type,
            "page_number": self.evidence.page_number,
            "individual_scores": {key: round(value, 6) for key, value in self.individual_scores.items()},
            "final_score": round(self.final_score, 6),
            "rank": self.rank,
            "selected": self.selected,
            "selection_reason": self.selection_reason,
            "exclusion_reason": self.exclusion_reason,
            "text_preview": self.evidence.text[:500],
        }


class RequirementExtractionAgent:
    """Extracts canonical rules from document chunks. It never emits SHACL."""

    name = "qwen_requirement_extraction_agent"

    def __init__(self, llm: OllamaLLMProvider | None = None) -> None:
        self.llm = llm or OllamaLLMProvider()
        self.metrics = {"llm_calls": 0, "rules_from_llm": 0, "rules_rejected": 0}
        self.audit_events: list[dict] = []
        self.retrieval_audit: dict = {}
        self.governance_audit: dict = {}
        self._candidate_counter = 0

    def extract(self, document: StructuredDocument, wheelchair_pages_only: bool = True) -> list[CanonicalRule]:
        self.audit_events = []
        self.retrieval_audit = {}
        self.governance_audit = {}
        self._candidate_counter = 0
        evidence_items = [
            evidence
            for evidence in self._iter_evidence(document)
            if _clean_text(evidence.text)
        ]
        evidence_items, self.retrieval_audit = select_agent_evidence(evidence_items)
        llm_rules = self._extract_with_llm(evidence_items)
        merged, self.governance_audit = _best_rules(llm_rules, self.retrieval_audit)
        self.metrics["rules_from_llm"] = len(llm_rules)
        self.metrics["rules_rejected"] = len([event for event in self.audit_events if event.get("status") == "rejected"])
        return merged

    def _extract_with_llm(self, evidence_items: list[RequirementEvidence]) -> list[CanonicalRule]:
        evidence_by_id = {item.chunk_id: item for item in evidence_items}
        rules = []
        for batch_index, batch in enumerate(_evidence_batches(evidence_items), start=1):
            rules.extend(self._extract_batch_with_llm(batch, evidence_by_id, batch_index))
        return rules

    def _extract_batch_with_llm(
        self,
        evidence_items: list[RequirementEvidence],
        evidence_by_id: dict[str, RequirementEvidence],
        batch_index: int,
    ) -> list[CanonicalRule]:
        evidence_text = "\n\n".join(f"[{item.chunk_type} {item.chunk_id} page {item.page_number}]\n{item.text[:1500]}" for item in evidence_items)
        canonical_rule_lines = _canonical_rule_prompt_lines()
        prompt = f"""
You are a requirement extraction agent for technical standards.
Return only JSON with this shape:
{{"rules":[{{"rule_id":"","entity":"","property":"","operator":"","value":0,"unit":"","source_text":"","source_page":0,"confidence":0.0,"evidence_chunk_id":"","evidence_chunk_type":""}}]}}

Extract only measurement rules that match the configured canonical vocabulary.
Use only these canonical rule_id values when the evidence explicitly supports them:
{canonical_rule_lines}

Scan every evidence block independently, including table rows, list items, captions, OCR text, and vision observations.
Do not stop after the first matching rule in a batch.
The evidence may be in any language; use the semantic meaning of the configured canonical vocabulary.
Only output a rule when the value, operator, and requirement are explicitly present in the evidence.
Every rule must include the evidence_chunk_id exactly as shown in square brackets.
If a batch has no supported numeric rule, return {{"rules":[]}}.
Do not generate SHACL, RDF, or ontology mappings.
Do not add missing rules.
Do not output general accessibility statements, feature descriptions, or non-numeric requirements.

Evidence:
{evidence_text}
"""
        self.metrics["llm_calls"] += 1
        self.retrieval_audit.setdefault("batches", []).append(
            {
                "batch_index": batch_index,
                "chunk_ids": [item.chunk_id for item in evidence_items],
                "prompt": prompt,
            }
        )
        try:
            data = self.llm.generate_json(prompt)
        except Exception as exc:
            self.audit_events.append(
                {
                    "status": "rejected",
                    "rule_source": "agent",
                    "agent_invocation": {"provider": self.llm.name, "batch_index": batch_index},
                    "evidence_used": [item.chunk_id for item in evidence_items],
                    "rejection_reason": f"agent_call_failed: {exc}",
                }
            )
            return []
        rules: list[CanonicalRule] = []
        for item in data.get("rules", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                self._record_rejected_agent_rule({"raw_output": item}, evidence_items, batch_index, "malformed_agent_rule")
                continue
            evidence = _resolve_evidence(item, evidence_by_id)
            if evidence is None:
                self._record_rejected_agent_rule(item, evidence_items, batch_index, "missing_or_untraceable_evidence")
                continue
            try:
                normalized = _normalize_llm_rule(item)
                if normalized is None:
                    self._record_rejected_agent_rule(item, evidence_items, batch_index, "unsupported_or_incomplete_rule")
                    continue
                value = _float_or_none(normalized.get("value"))
                if value is None:
                    self._record_rejected_agent_rule(item, evidence_items, batch_index, "missing_numeric_value")
                    continue
                source_text = _clean_text(str(item.get("source_text") or ""))
                if not source_text:
                    source_text = _snippet(evidence.text, str(normalized.get("value") or ""))
                self._candidate_counter += 1
                candidate_id = f"C{self._candidate_counter:04d}"
                rules.append(
                    CanonicalRule(
                        rule_id=normalized["rule_id"],
                        entity=normalized["entity"],
                        property=normalized["property"],
                        operator=normalized["operator"],
                        value=float(value),
                        unit=normalized["unit"],
                        source_text=source_text[:500],
                        source_page=evidence.page_number,
                        confidence=_confidence(item.get("confidence")),
                        extraction_metadata={
                            "extractor": self.name,
                            "llm_used": True,
                            "agent_generated": True,
                            "rule_source": "agent_decision",
                            "llm_provider": self.llm.name,
                            "candidate_id": candidate_id,
                            "evidence_chunk_id": evidence.chunk_id,
                            "evidence_chunk_type": evidence.chunk_type,
                            "evidence_text": evidence.text[:1500],
                            "agent_batch_index": batch_index,
                            "raw_agent_rule": item,
                        },
                    )
                )
                self.audit_events.append(
                    {
                        "status": "accepted",
                        "candidate_id": candidate_id,
                        "rule_source": "agent",
                        "agent_invocation": {"provider": self.llm.name, "batch_index": batch_index},
                        "evidence_used": {
                            "chunk_id": evidence.chunk_id,
                            "chunk_type": evidence.chunk_type,
                            "source_page": evidence.page_number,
                            "text": evidence.text[:500],
                        },
                        "agent_output": item,
                        "output_rule": rules[-1].to_dict(),
                    }
                )
            except (KeyError, TypeError, ValueError):
                self._record_rejected_agent_rule(item, evidence_items, batch_index, "normalization_failed")
                continue
        return [rule for rule in rules if rule.rule_id and rule.value]

    def _record_rejected_agent_rule(
        self,
        item: dict,
        evidence_items: list[RequirementEvidence],
        batch_index: int,
        reason: str,
    ) -> None:
        self.audit_events.append(
            {
                "status": "rejected",
                "rule_source": "agent",
                "agent_invocation": {"provider": self.llm.name, "batch_index": batch_index},
                "evidence_used": [evidence.chunk_id for evidence in evidence_items],
                "agent_output": item,
                "rejection_reason": reason,
            }
        )

    def _iter_evidence(self, document: StructuredDocument) -> Iterable[RequirementEvidence]:
        text_by_page: dict[int, list[str]] = {}
        for chunk in document.text_chunks:
            text_by_page.setdefault(chunk.page_number, []).append(chunk.text)
            yield _text_evidence(chunk)
        for chunk in document.table_chunks:
            text_by_page.setdefault(chunk.page_number, []).append(chunk.text)
            yield _table_evidence(chunk)
        for chunk in document.image_chunks:
            ranking = chunk.metadata.get("vision_ranking", {}) if isinstance(chunk.metadata, dict) else {}
            vision_text = chunk.vision_text if not ranking or ranking.get("selected_for_vision") else ""
            text = " ".join(part for part in [chunk.ocr_text, vision_text] if part)
            if not text:
                continue
            yield RequirementEvidence(
                chunk_id=chunk.chunk_id,
                chunk_type="image",
                page_number=chunk.page_number,
                text=text,
            )
        for page_number, parts in sorted(text_by_page.items()):
            yield RequirementEvidence(
                chunk_id=f"p{page_number:03d}-page-context",
                chunk_type="page_context",
                page_number=page_number,
                text=" ".join(parts),
            )


def _text_evidence(chunk: TextChunk) -> RequirementEvidence:
    return RequirementEvidence(chunk_id=chunk.chunk_id, chunk_type="text", page_number=chunk.page_number, text=chunk.text)


def _table_evidence(chunk: TableChunk) -> RequirementEvidence:
    return RequirementEvidence(chunk_id=chunk.chunk_id, chunk_type="table", page_number=chunk.page_number, text=chunk.text)


def _best_rules(candidates: list[CanonicalRule], retrieval_audit: dict | None = None) -> tuple[list[CanonicalRule], dict]:
    retrieval_index = _retrieval_index(retrieval_audit or {})
    scored = [_score_candidate_governance(rule, retrieval_index) for rule in candidates]
    _apply_contradiction_penalties(scored)
    by_rule: dict[str, list[dict]] = {}
    for item in scored:
        by_rule.setdefault(item["rule"].rule_id, []).append(item)

    selected: list[CanonicalRule] = []
    duplicate_groups = []
    conflict_groups = []
    all_candidates = []
    for rule_id, items in sorted(by_rule.items()):
        ranked = sorted(
            items,
            key=lambda item: (
                item["selection_score"],
                item["scores"]["source_specificity_score"],
                item["scores"]["measurement_specificity_score"],
                item["scores"]["requirement_completeness_score"],
                item["candidate_id"],
            ),
            reverse=True,
        )
        winner = ranked[0]
        selected_rule = _with_governance_metadata(winner, ranked)
        selected.append(selected_rule)
        duplicate_groups.append(_duplicate_group(rule_id, ranked, winner))
        conflict = _conflict_group(rule_id, ranked)
        if conflict:
            conflict_groups.append(conflict)
        all_candidates.extend(_candidate_summary(item, selected=item is winner) for item in ranked)

    governance_audit = {
        "strategy": "evidence_quality_candidate_selection",
        "selection_weights": _governance_weights(),
        "candidate_count": len(scored),
        "selected_candidate_count": len(selected),
        "candidates": all_candidates,
        "duplicate_groups": duplicate_groups,
        "conflict_records": conflict_groups,
        "conflict_groups": conflict_groups,
        "pairwise_comparisons": _pairwise_comparisons(by_rule),
        "score_distribution": _score_distribution(scored),
    }
    return selected, governance_audit


def _retrieval_index(retrieval_audit: dict) -> dict[str, dict]:
    return {
        item["chunk_id"]: item
        for item in retrieval_audit.get("selected_chunks", []) + retrieval_audit.get("excluded_chunks", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }


def _score_candidate_governance(rule: CanonicalRule, retrieval_index: dict[str, dict]) -> dict:
    evidence_chunk_id = str(rule.extraction_metadata.get("evidence_chunk_id", ""))
    retrieval = retrieval_index.get(evidence_chunk_id, {})
    scores = {
        "semantic_relevance_score": _candidate_semantic_relevance(rule),
        "measurement_specificity_score": _candidate_measurement_specificity(rule),
        "source_specificity_score": _candidate_source_specificity(rule),
        "requirement_completeness_score": _candidate_requirement_completeness(rule),
        "ontology_consistency_score": _candidate_ontology_consistency(rule),
        "retrieval_support_score": _candidate_retrieval_score(retrieval),
        "evidence_density_score": _candidate_evidence_density(rule),
        "contradiction_penalty": 0.0,
    }
    weights = _governance_weights()
    final = sum(scores[key] * weights[key] for key in weights)
    return {
        "candidate_id": str(rule.extraction_metadata.get("candidate_id", "")),
        "rule": rule,
        "scores": scores,
        "selection_score": final,
        "retrieval_rank": retrieval.get("rank"),
        "retrieval_final_score": retrieval.get("final_score"),
    }


def _governance_weights() -> dict[str, float]:
    return {
        "semantic_relevance_score": 0.05,
        "measurement_specificity_score": 0.20,
        "source_specificity_score": 0.45,
        "requirement_completeness_score": 0.15,
        "ontology_consistency_score": 0.01,
        "retrieval_support_score": 0.03,
        "evidence_density_score": 0.11,
        "contradiction_penalty": -0.15,
    }


def _apply_contradiction_penalties(scored: list[dict]) -> None:
    values_by_rule: dict[str, set[str]] = {}
    for item in scored:
        rule = item["rule"]
        values_by_rule.setdefault(rule.rule_id, set()).add(_normalized_requirement_key(rule))
    weights = _governance_weights()
    for item in scored:
        rule = item["rule"]
        contradiction_count = max(0, len(values_by_rule.get(rule.rule_id, set())) - 1)
        item["scores"]["contradiction_penalty"] = min(1.0, contradiction_count / 3.0)
        item["selection_score"] = sum(item["scores"][key] * weights[key] for key in weights)


def _candidate_source_specificity(rule: CanonicalRule) -> float:
    evidence = _candidate_evidence_text(rule)
    tokens = _tokens(evidence)
    if not tokens:
        return 0.0
    measurement_score = _measurement_density(evidence)
    direct_requirement = _direct_requirement_score(evidence)
    length = len(tokens)
    if 8 <= length <= 80:
        length_score = 1.0
    elif 81 <= length <= 180:
        length_score = 0.65
    elif 181 <= length <= 320:
        length_score = 0.35
    elif length > 320:
        length_score = 0.15
    else:
        length_score = 0.25
    structure_score = 1.0 if _candidate_chunk_type(rule) == "table" else 0.0
    return min(1.0, (measurement_score * 0.20) + (direct_requirement * 0.30) + (length_score * 0.20) + (structure_score * 0.30))


def _candidate_measurement_specificity(rule: CanonicalRule) -> float:
    evidence = _candidate_joined_text(rule)
    value_score = 0.6 if _value_appears(rule.value, rule.unit, evidence) else 0.0
    operator_score = 0.25 if _operator_is_consistent(rule) else 0.0
    unit_score = 0.15 if _unit_is_supported(rule.unit, evidence) else 0.0
    return min(1.0, value_score + operator_score + unit_score)


def _candidate_semantic_relevance(rule: CanonicalRule) -> float:
    evidence = _candidate_evidence_text(rule)
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    descriptor = " ".join(str(raw.get(key, "")) for key in ["entity", "property"])
    descriptor_tokens = _token_set(descriptor.replace("_", " "))
    evidence_tokens = _token_set(evidence)
    if not descriptor_tokens or not evidence_tokens:
        return 0.0
    return min(1.0, _jaccard(descriptor_tokens, evidence_tokens))


def _candidate_retrieval_score(retrieval: dict) -> float:
    rank = _float_or_none(retrieval.get("rank"))
    if rank is None or rank <= 0:
        return 0.0
    return max(0.0, 1.0 - min(rank, 200.0) / 200.0)


def _candidate_source_confidence(rule: CanonicalRule) -> float:
    return max(0.0, min(1.0, rule.confidence))


def _candidate_provenance_quality(rule: CanonicalRule) -> float:
    metadata = rule.extraction_metadata
    score = 0.0
    if metadata.get("candidate_id"):
        score += 0.2
    if metadata.get("evidence_chunk_id"):
        score += 0.25
    if rule.source_page:
        score += 0.2
    if metadata.get("agent_batch_index"):
        score += 0.15
    if metadata.get("raw_agent_rule"):
        score += 0.2
    return min(1.0, score)


def _candidate_requirement_completeness(rule: CanonicalRule) -> float:
    metadata = rule.extraction_metadata
    score = 0.0
    if metadata.get("evidence_chunk_id"):
        score += 0.15
    if _candidate_evidence_text(rule):
        score += 0.15
    if rule.source_page:
        score += 0.10
    if rule.source_text:
        score += 0.10
    if _value_appears(rule.value, rule.unit, _candidate_joined_text(rule)):
        score += 0.20
    if _operator_is_consistent(rule):
        score += 0.15
    if _direct_requirement_score(_candidate_joined_text(rule)) > 0.5:
        score += 0.15
    return min(1.0, score)


def _candidate_evidence_density(rule: CanonicalRule) -> float:
    evidence = _candidate_evidence_text(rule)
    tokens = _tokens(evidence)
    if not tokens:
        return 0.0
    measurement = _measurement_density(evidence)
    requirement = _requirement_confidence(evidence)
    compactness = max(0.0, 1.0 - max(0, len(tokens) - 120) / 360.0)
    return min(1.0, (measurement * 0.35) + (requirement * 0.40) + (compactness * 0.25))


def _candidate_ontology_consistency(rule: CanonicalRule) -> float:
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    raw_entity = _token_set(str(raw.get("entity", "")).replace("_", " "))
    raw_property = _token_set(str(raw.get("property", "")).replace("_", " "))
    canonical_entity = _token_set(str(rule.entity).replace("_", " "))
    canonical_property = _token_set(str(rule.property).replace("_", " "))
    evidence_tokens = _token_set(_candidate_evidence_text(rule).replace("_", " "))
    raw_alignment = _jaccard(raw_entity | raw_property, canonical_entity | canonical_property)
    evidence_alignment = _jaccard((raw_entity | raw_property | canonical_entity | canonical_property), evidence_tokens)
    return min(1.0, (raw_alignment * 0.35) + (evidence_alignment * 0.65))


def _candidate_context_consistency(rule: CanonicalRule) -> float:
    evidence = _candidate_evidence_text(rule)
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    source_page = _float_or_none(raw.get("source_page"))
    page_score = 0.25 if source_page is None or int(source_page) == rule.source_page else 0.0
    value_score = 0.35 if _value_appears(rule.value, rule.unit, evidence) else 0.0
    operator_score = 0.25 if _operator_is_consistent(rule) else 0.0
    source_text = _candidate_source_text_content(rule)
    source_score = 0.15 if source_text and _jaccard(_token_set(source_text), _token_set(evidence)) > 0.15 else 0.0
    return min(1.0, page_score + value_score + operator_score + source_score)


def _candidate_source_text_support(rule: CanonicalRule) -> float:
    source_text = _candidate_source_text_content(rule)
    if not source_text:
        return 0.0
    evidence = _candidate_evidence_text(rule)
    source_tokens = _token_set(source_text)
    evidence_tokens = _token_set(evidence)
    if not source_tokens or not evidence_tokens:
        return 0.0
    overlap = _jaccard(source_tokens, evidence_tokens)
    measurement_bonus = 0.35 if _value_appears(rule.value, rule.unit, source_text) else 0.0
    return min(1.0, overlap + measurement_bonus)


def _with_governance_metadata(winner: dict, ranked: list[dict]) -> CanonicalRule:
    rule = winner["rule"]
    rejected = [item["candidate_id"] for item in ranked if item is not winner]
    metadata = dict(rule.extraction_metadata)
    metadata.update(
        {
            "candidate_id": winner["candidate_id"],
            "retrieval_rank": winner["retrieval_rank"],
            "selection_score": round(winner["selection_score"], 6),
            "selection_scores": {key: round(value, 6) for key, value in winner["scores"].items()},
            "selection_explanation": _selection_explanation(winner),
            "governance_strategy": "candidate_evaluation_framework",
            "rejected_competing_candidates": rejected,
            "duplicate_group_id": f"dup-{rule.rule_id}",
        }
    )
    return replace(rule, extraction_metadata=metadata)


def _selection_explanation(item: dict) -> str:
    top = sorted(item["scores"].items(), key=lambda pair: pair[1], reverse=True)[:4]
    parts = ",".join(f"{key}={value:.3f}" for key, value in top)
    return f"selected_by_evidence_quality:{parts}"


def _duplicate_group(rule_id: str, ranked: list[dict], winner: dict) -> dict:
    return {
        "duplicate_group_id": f"dup-{rule_id}",
        "rule_id": rule_id,
        "group_members": [item["candidate_id"] for item in ranked],
        "selected_candidate": winner["candidate_id"],
        "rejected_candidates": [item["candidate_id"] for item in ranked if item is not winner],
        "merge_rationale": _selection_explanation(winner),
        "normalized_requirement_keys": {
            item["candidate_id"]: _normalized_requirement_key(item["rule"])
            for item in ranked
        },
    }


def _conflict_group(rule_id: str, ranked: list[dict]) -> dict | None:
    values: dict[str, list[str]] = {}
    for item in ranked:
        rule = item["rule"]
        key = f"{rule.operator}:{rule.value:g}:{rule.unit}"
        values.setdefault(key, []).append(item["candidate_id"])
    if len(values) <= 1:
        return None
    record = ConflictRecord(
        conflict_id=f"conflict-{rule_id}",
        rule_id=rule_id,
        conflicting_candidates=[item["candidate_id"] for item in ranked],
        supporting_evidence=[
            {
                "candidate_id": item["candidate_id"],
                "source_page": item["rule"].source_page,
                "evidence_chunk_id": item["rule"].extraction_metadata.get("evidence_chunk_id"),
                "source_text": item["rule"].source_text,
            }
            for item in ranked
        ],
        confidence_comparison=[
            {
                "candidate_id": item["candidate_id"],
                "confidence": item["rule"].confidence,
                "selection_score": round(item["selection_score"], 6),
                "normalized_value": _normalized_requirement_key(item["rule"]),
            }
            for item in ranked
        ],
        resolution_status="unresolved",
    )
    result = {
        "conflict_type": "normalized_value_or_operator_conflict",
        "values": values,
        "ranked_candidates": [item["candidate_id"] for item in ranked],
    }
    result.update(record.__dict__)
    return result


def _candidate_summary(item: dict, selected: bool) -> dict:
    rule = item["rule"]
    return {
        "candidate_id": item["candidate_id"],
        "rule_id": rule.rule_id,
        "value": rule.value,
        "unit": rule.unit,
        "operator": rule.operator,
        "source_page": rule.source_page,
        "source_chunk": rule.extraction_metadata.get("evidence_chunk_id"),
        "retrieval_rank": item["retrieval_rank"],
        "confidence": rule.confidence,
        "candidate_evaluation": {key: round(value, 6) for key, value in item["scores"].items()},
        "governance_scores": {key: round(value, 6) for key, value in item["scores"].items()},
        "selection_score": round(item["selection_score"], 6),
        "selected": selected,
        "selection_rationale": _selection_explanation(item) if selected else "lower_evidence_quality_than_selected_candidate",
        "supporting_evidence": _candidate_evidence_text(rule)[:500],
        "evidence_breakdown": {
            "source_text": rule.source_text,
            "evidence_text": _candidate_evidence_text(rule)[:1000],
            "raw_agent_rule": rule.extraction_metadata.get("raw_agent_rule"),
        },
    }


def _pairwise_comparisons(by_rule: dict[str, list[dict]]) -> list[dict]:
    comparisons = []
    for rule_id, items in sorted(by_rule.items()):
        ranked = sorted(items, key=lambda item: item["selection_score"], reverse=True)
        if not ranked:
            continue
        winner = ranked[0]
        for loser in ranked[1:]:
            comparisons.append(_candidate_comparison(rule_id, winner, loser))
    return comparisons


def _candidate_comparison(rule_id: str, winner: dict, loser: dict) -> dict:
    dimensions = [
        "semantic_relevance_score",
        "source_specificity_score",
        "measurement_specificity_score",
        "ontology_consistency_score",
        "retrieval_support_score",
        "evidence_density_score",
        "requirement_completeness_score",
        "contradiction_penalty",
    ]
    deltas = {
        key: round(winner["scores"].get(key, 0.0) - loser["scores"].get(key, 0.0), 6)
        for key in dimensions
    }
    positive = [key for key, value in deltas.items() if key != "contradiction_penalty" and value > 0]
    negative = [key for key, value in deltas.items() if key != "contradiction_penalty" and value < 0]
    if deltas.get("contradiction_penalty", 0.0) < 0:
        positive.append("lower_contradiction_penalty")
    elif deltas.get("contradiction_penalty", 0.0) > 0:
        negative.append("higher_contradiction_penalty")
    rationale = (
        "winner_has_stronger_" + ",".join(positive)
        if positive
        else "winner_selected_by_total_evidence_quality_score"
    )
    if negative:
        rationale += ";loser_stronger_" + ",".join(negative)
    return {
        "rule_id": rule_id,
        "winner_candidate_id": winner["candidate_id"],
        "loser_candidate_id": loser["candidate_id"],
        "winner_score": round(winner["selection_score"], 6),
        "loser_score": round(loser["selection_score"], 6),
        "score_delta": round(winner["selection_score"] - loser["selection_score"], 6),
        "component_deltas": deltas,
        "comparison_rationale": rationale,
        "winner_evidence": _candidate_evidence_text(winner["rule"])[:500],
        "loser_evidence": _candidate_evidence_text(loser["rule"])[:500],
    }


def _score_distribution(scored: list[dict]) -> dict:
    values = [item["selection_score"] for item in scored]
    if not values:
        return {"candidate_count": 0, "min": None, "max": None, "mean": None}
    return {
        "candidate_count": len(values),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "by_rule": {
            rule_id: {
                "count": len(items),
                "min": round(min(item["selection_score"] for item in items), 6),
                "max": round(max(item["selection_score"] for item in items), 6),
            }
            for rule_id, items in _group_scored_by_rule(scored).items()
        },
    }


def _group_scored_by_rule(scored: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for item in scored:
        result.setdefault(item["rule"].rule_id, []).append(item)
    return result


def _direct_requirement_score(text: str) -> float:
    if not text:
        return 0.0
    measurement = _measurement_density(text)
    constraint = _constraint_likelihood(text)
    normative = _normative_language(text)
    tokens = _tokens(text)
    compactness = 1.0 if len(tokens) <= 80 else max(0.0, 1.0 - (len(tokens) - 80) / 300.0)
    return min(1.0, (measurement * 0.30) + (constraint * 0.30) + (normative * 0.25) + (compactness * 0.15))


def _normalized_requirement_key(rule: CanonicalRule) -> str:
    return f"{rule.operator}:{rule.value:g}:{rule.unit.lower()}"


def _candidate_evidence_text(rule: CanonicalRule) -> str:
    return str(rule.extraction_metadata.get("evidence_text") or "")


def _candidate_joined_text(rule: CanonicalRule) -> str:
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    return " ".join(
        [
            _candidate_evidence_text(rule),
            str(rule.source_text or ""),
            str(raw.get("source_text", "")),
            str(raw.get("entity", "")),
            str(raw.get("property", "")),
        ]
    )


def _candidate_source_text_content(rule: CanonicalRule) -> str:
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    text = _clean_text(str(raw.get("source_text") or rule.source_text or ""))
    without_citations = re.sub(r"\[[^\]]+\]", " ", text)
    return _clean_text(without_citations)


def _candidate_chunk_type(rule: CanonicalRule) -> str:
    return str(rule.extraction_metadata.get("evidence_chunk_type", ""))


def _value_appears(value: float, unit: str, text: str) -> bool:
    lowered = text.lower()
    numeric_forms = {f"{value:g}", f"{value:.1f}", f"{value:.2f}".rstrip("0").rstrip(".")}
    if unit == "m":
        numeric_forms.update({f"{value * 100:g}", f"{value * 100:.0f}"})
    for number in numeric_forms:
        if re.search(rf"(?<!\d){re.escape(number)}(?:[,.]0+)?(?!\d)", lowered):
            return True
    return False


def _unit_is_supported(unit: str, text: str) -> bool:
    lowered = text.lower()
    if unit == "%":
        return "%" in lowered or "percent" in lowered or "prozent" in lowered
    if unit == "m":
        return any(token in lowered for token in [" m", "cm", "meter", "metre"])
    return unit.lower() in lowered


def _operator_is_consistent(rule: CanonicalRule) -> bool:
    raw = rule.extraction_metadata.get("raw_agent_rule", {})
    raw_operator = str(raw.get("operator", "")).strip().lower()
    canonical = str(rule.operator).strip().lower()
    if not raw_operator:
        return True
    return _operator_family(raw_operator) == _operator_family(canonical)


def _operator_family(value: str) -> str:
    if value in {">=", "≥"} or "min" in value or "least" in value:
        return "minimum"
    if value in {"<=", "≤"} or "max" in value or "most" in value:
        return "maximum"
    if value in {"=", "=="}:
        return "exact"
    return value


def _normalize_llm_rule(item: dict) -> dict | None:
    rule_id = str(item.get("rule_id", "")).strip()
    schema = _canonical_rule_by_id().get(rule_id)
    if not schema:
        return None
    value = _float_or_none(item.get("value"))
    if value is None:
        return None
    unit = str(item.get("unit") or "").lower()
    normalized_value = _normalized_value(value, unit, schema)
    expected = _float_or_none(schema.get("value"))
    tolerance = _float_or_none(schema.get("tolerance")) or 0.0
    if expected is None or not _close(normalized_value, expected, tolerance):
        return None
    return _llm_rule(
        item,
        rule_id,
        str(schema["entity"]),
        str(schema["property"]),
        str(schema["operator"]),
        float(expected),
        str(schema["unit"]),
    )


def _llm_rule(item: dict, rule_id: str, entity: str, prop: str, operator: str, value: float, unit: str) -> dict:
    result = dict(item)
    result.update({"rule_id": rule_id, "entity": entity, "property": prop, "operator": operator, "value": value, "unit": unit})
    return result


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(value) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        return 0.0
    return max(0.0, min(1.0, parsed))


def _close(value: float, expected: float, tolerance: float) -> bool:
    return abs(value - expected) <= tolerance


def _normalized_value(value: float, unit: str, schema: dict) -> float:
    if schema.get("dimension_unit") and ("cm" in unit or "centimeter" in unit or "centimet" in unit or value > 20):
        return value / 100
    return value


@lru_cache(maxsize=1)
def _canonical_rule_schema() -> list[dict]:
    if not _CANONICAL_RULE_SCHEMA.exists():
        raise RuntimeError(f"Canonical rule schema missing: {_CANONICAL_RULE_SCHEMA}")
    data = json.loads(_CANONICAL_RULE_SCHEMA.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    if not isinstance(rules, list) or not rules:
        raise RuntimeError(f"Canonical rule schema has no rules: {_CANONICAL_RULE_SCHEMA}")
    return [rule for rule in rules if isinstance(rule, dict)]


@lru_cache(maxsize=1)
def _canonical_rule_by_id() -> dict[str, dict]:
    return {
        str(rule.get("rule_id", "")): rule
        for rule in _canonical_rule_schema()
        if rule.get("rule_id")
    }


def _canonical_rule_prompt_lines() -> str:
    lines = []
    for rule in _canonical_rule_schema():
        lines.append(
            f"- {rule['rule_id']}: {rule.get('description', '')}, "
            f"operator {rule['operator']}, value {rule['value']} {rule['unit']}"
        )
    return "\n".join(lines)


def _snippet(text: str, token: str, size: int = 260) -> str:
    lowered = text.lower()
    index = lowered.find(token.lower()) if token else -1
    if index < 0:
        return text[:size].strip()
    start = max(0, index - size // 2)
    end = min(len(text), index + size // 2)
    return text[start:end].strip()


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def select_agent_evidence(evidence_items: list[RequirementEvidence], max_items: int = 96) -> tuple[list[RequirementEvidence], dict]:
    corpus_tokens = [_token_set(item.text) for item in evidence_items]
    document_frequency = Counter(token for tokens in corpus_tokens for token in tokens)
    scored = [_score_evidence(item, document_frequency, len(evidence_items), corpus_tokens) for item in evidence_items]
    ranked = sorted(scored, key=lambda item: (-item.final_score, item.evidence.page_number, item.evidence.chunk_id))
    selected: list[ScoredEvidence] = []
    selected_tokens: list[set[str]] = []
    selected_reasons: dict[str, str] = {}
    skipped_reasons: dict[str, str] = {}

    def add_item(item: ScoredEvidence, reason: str) -> bool:
        if len(selected) >= max_items:
            return False
        if item.evidence.chunk_id in selected_reasons:
            return False
        tokens = _token_set(item.evidence.text)
        overlap = max((_jaccard(tokens, existing) for existing in selected_tokens), default=0.0)
        if overlap > 0.92 and item.evidence.chunk_type == "page_context":
            skipped_reasons[item.evidence.chunk_id] = "near_duplicate_page_context"
            return False
        selected.append(item)
        selected_tokens.append(tokens)
        selected_reasons[item.evidence.chunk_id] = reason
        return True

    seed_limit = min(max_items, 24)
    for item in ranked:
        if len(selected) >= seed_limit:
            break
        add_item(item, "selected_by_global_generic_quality_score")

    by_page: dict[int, list[ScoredEvidence]] = {}
    for item in ranked:
        by_page.setdefault(item.evidence.page_number, []).append(item)

    seed_pages = []
    for item in selected:
        if item.evidence.page_number not in seed_pages:
            seed_pages.append(item.evidence.page_number)
    for page_number in seed_pages:
        local_added = 0
        for item in by_page.get(page_number, []):
            if item.evidence.chunk_type == "page_context":
                continue
            if item.individual_scores["requirement_confidence"] <= 0 and item.individual_scores["table_importance"] <= 0:
                continue
            if add_item(item, f"selected_as_fine_grained_support_for_page_{page_number}"):
                local_added += 1
            if local_added >= 6:
                break

    for item in ranked:
        if len(selected) >= max_items:
            break
        add_item(item, "selected_by_global_generic_quality_fill")

    selected_ids = {item.evidence.chunk_id for item in selected}
    audited: list[ScoredEvidence] = []
    for rank, item in enumerate(ranked, start=1):
        is_selected = item.evidence.chunk_id in selected_ids
        audited.append(
            ScoredEvidence(
                evidence=item.evidence,
                individual_scores=item.individual_scores,
                final_score=item.final_score,
                rank=rank,
                selected=is_selected,
                selection_reason=_selection_reason(item, selected_reasons.get(item.evidence.chunk_id, "")) if is_selected else "",
                exclusion_reason="" if is_selected else skipped_reasons.get(item.evidence.chunk_id, _exclusion_reason(max_items)),
            )
        )
    selected_evidence = [item.evidence for item in audited if item.selected]
    retrieval_audit = {
        "strategy": "generic_quality_scoring",
        "max_selected_chunks": max_items,
        "score_weights": _score_weights(),
        "selected_chunks": [item.to_dict() for item in audited if item.selected],
        "excluded_chunks": [item.to_dict() for item in audited if not item.selected],
        "batches": [],
    }
    return selected_evidence, retrieval_audit


def _score_evidence(evidence: RequirementEvidence, document_frequency: Counter, document_count: int, corpus_tokens: list[set[str]]) -> ScoredEvidence:
    text = evidence.text
    tokens = _token_set(text)
    scores = {
        "measurement_density": _measurement_density(text),
        "constraint_likelihood": _constraint_likelihood(text),
        "normative_language": _normative_language(text),
        "definition_likelihood": _definition_likelihood(text),
        "table_importance": _table_importance(evidence, text),
        "cross_reference": _cross_reference_score(text),
        "citation": _citation_score(text),
        "requirement_confidence": _requirement_confidence(text),
        "evidence_uniqueness": _evidence_uniqueness(tokens, document_frequency, document_count),
        "semantic_centrality": _semantic_centrality(tokens, corpus_tokens),
    }
    weights = _score_weights()
    final = sum(scores[key] * weights[key] for key in weights)
    return ScoredEvidence(evidence, scores, final, 0, False, "", "")


def _score_weights() -> dict[str, float]:
    return {
        "measurement_density": 0.17,
        "constraint_likelihood": 0.17,
        "normative_language": 0.14,
        "definition_likelihood": 0.05,
        "table_importance": 0.12,
        "cross_reference": 0.08,
        "citation": 0.05,
        "requirement_confidence": 0.12,
        "evidence_uniqueness": 0.05,
        "semantic_centrality": 0.05,
    }


def _measurement_density(text: str) -> float:
    token_count = max(1, len(_tokens(text)))
    number = r"\d+(?:[,.]\d+)?"
    unit = r"(?:%|[A-Za-z]{1,4}|[^\W\d_]{1,4})"
    measurements = re.findall(
        rf"(?:[<>=]|\u2264|\u2265)?\s*{number}\s*{unit}\b|"
        rf"{number}\s*(?:x|\u00d7)\s*{number}|"
        rf"{number}\s+(?:-|to|bis)\s+{number}",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    return min(1.0, len(measurements) / max(4.0, token_count / 20.0))


def _constraint_likelihood(text: str) -> float:
    markers = len(
        re.findall(
            r"(?:<=|>=|<|>|=)|(?:\b\d+(?:[,.]\d+)?\s+(?:-|to|bis)\s+\d+)|"
            r"\b(?:minimum|maximum|min|max|at\s+least|at\s+most|not\s+less\s+than|not\s+more\s+than|between|"
            r"mindestens|hoechstens|h.chstens|von|bis)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    return min(1.0, markers / 3.0)


def _normative_language(text: str) -> float:
    lowered = text.lower()
    hits = sum(1 for term in _NORMATIVE_TERMS if term in lowered)
    return min(1.0, hits / 4.0)


def _definition_likelihood(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return 0.0
    colon_score = 0.4 if ":" in stripped[:160] else 0.0
    short_label_score = 0.3 if re.match(r"^[\w\s()/.-]{2,80}\s+[-:]\s+", stripped) else 0.0
    compact_score = 0.3 if len(_tokens(stripped)) <= 80 else 0.0
    return min(1.0, colon_score + short_label_score + compact_score)


def _table_importance(evidence: RequirementEvidence, text: str) -> float:
    if evidence.chunk_type == "table":
        return 1.0
    separators = text.count("|") + text.count(";")
    return min(1.0, separators / 8.0)


def _cross_reference_score(text: str) -> float:
    refs = len(re.findall(r"\b(?:section|clause|table|figure|appendix|annex|abschnitt|tabelle|bild)\s+\d+", text, flags=re.IGNORECASE))
    return min(1.0, refs / 3.0)


def _citation_score(text: str) -> float:
    citations = len(re.findall(r"\b(?:[A-Z]{2,}|ISO|IEC|EN)\s*[-A-Z0-9]*\s*\d{2,}", text))
    return min(1.0, citations / 3.0)


def _requirement_confidence(text: str) -> float:
    return min(1.0, (_measurement_density(text) * 0.45) + (_normative_language(text) * 0.35) + (_constraint_likelihood(text) * 0.2))


def _evidence_uniqueness(tokens: set[str], document_frequency: Counter, document_count: int) -> float:
    if not tokens or document_count <= 0:
        return 0.0
    values = [math.log((document_count + 1) / (document_frequency[token] + 1)) for token in tokens]
    return min(1.0, (sum(values) / len(values)) / 3.0)


def _semantic_centrality(tokens: set[str], corpus_tokens: list[set[str]]) -> float:
    if not tokens or not corpus_tokens:
        return 0.0
    similarities = sorted((_jaccard(tokens, other) for other in corpus_tokens if other is not tokens), reverse=True)
    if not similarities:
        return 0.0
    return min(1.0, sum(similarities[:10]) / min(10, len(similarities)))


def _selection_reason(item: ScoredEvidence, phase: str) -> str:
    top = sorted(item.individual_scores.items(), key=lambda pair: pair[1], reverse=True)[:3]
    return f"{phase}:" + ",".join(f"{key}={value:.3f}" for key, value in top)


def _exclusion_reason(max_items: int) -> str:
    return f"not_in_top_{max_items}_generic_quality_scores"


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[^\W\d_]{3,}", text, flags=re.UNICODE)]


def _token_set(text: str) -> set[str]:
    return {token for token in _tokens(text) if token not in _STOPWORDS}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _evidence_batches(evidence_items: list[RequirementEvidence], batch_size: int = 2) -> Iterable[list[RequirementEvidence]]:
    for index in range(0, len(evidence_items), batch_size):
        yield evidence_items[index : index + batch_size]


def _resolve_evidence(item: dict, evidence_by_id: dict[str, RequirementEvidence]) -> RequirementEvidence | None:
    chunk_id = _normalize_evidence_id(str(item.get("evidence_chunk_id") or ""))
    if chunk_id in evidence_by_id:
        return evidence_by_id[chunk_id]
    source_text = _clean_text(str(item.get("source_text") or ""))
    if not source_text:
        return None
    needle = source_text.lower()
    for evidence in evidence_by_id.values():
        if needle in evidence.text.lower():
            return evidence
    return None


def _normalize_evidence_id(value: str) -> str:
    match = re.search(r"(p\d{3}-(?:text|table|image|page-context)-\d{3}|p\d{3}-page-context)", value)
    return match.group(1) if match else value.strip()


def write_extraction_audit_report(agent: RequirementExtractionAgent, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "agent": agent.name,
                "provider": agent.llm.name,
                "metrics": agent.metrics,
                "accepted_rule_count": len([event for event in agent.audit_events if event.get("status") == "accepted"]),
                "rejected_output_count": len([event for event in agent.audit_events if event.get("status") == "rejected"]),
                "retrieval": agent.retrieval_audit,
                "candidate_governance": agent.governance_audit,
                "raw_conflicts": _raw_conflict_summary(agent.audit_events),
                "events": agent.audit_events,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_candidate_selection_audit_report(agent: RequirementExtractionAgent, approved_rules: list[CanonicalRule], output_path: Path) -> None:
    before = None
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            if existing.get("audit_timing") == "before_implementation":
                before = existing
            else:
                before = existing.get("before_implementation")
        except json.JSONDecodeError:
            before = None
    governance = agent.governance_audit
    candidates = governance.get("candidates", [])
    by_candidate = {item.get("candidate_id"): item for item in candidates}
    records = []
    for rule in approved_rules:
        metadata = rule.extraction_metadata
        candidate_id = metadata.get("candidate_id")
        winner = by_candidate.get(candidate_id, {})
        rejected = [
            by_candidate[item]
            for item in metadata.get("rejected_competing_candidates", [])
            if item in by_candidate
        ]
        records.append(
            {
                "rule_id": rule.rule_id,
                "candidate_id": candidate_id,
                "source_chunk": metadata.get("evidence_chunk_id"),
                "page": rule.source_page,
                "confidence": rule.confidence,
                "selection_score": metadata.get("selection_score"),
                "selection_reason": metadata.get("selection_explanation"),
                "score_breakdown": metadata.get("selection_scores"),
                "evidence_breakdown": winner.get("evidence_breakdown", {}),
                "rejected_alternatives": rejected,
                "winner_loser_comparisons": [
                    item
                    for item in governance.get("pairwise_comparisons", [])
                    if item.get("rule_id") == rule.rule_id and item.get("winner_candidate_id") == candidate_id
                ],
            }
        )
    payload = {
        "phase": "candidate_selection_reliability",
        "audit_timing": "after_implementation",
        "before_implementation": before,
        "approved_rule_count": len(records),
        "records": records,
        "before_after_selection_comparison": _before_after_selection_comparison(before, records),
        "contradiction_inventory": governance.get("conflict_records", []),
        "duplicate_inventory": governance.get("duplicate_groups", []),
        "pairwise_comparisons": governance.get("pairwise_comparisons", []),
        "score_distribution": governance.get("score_distribution", {}),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _before_after_selection_comparison(before: dict | None, after_records: list[dict]) -> list[dict]:
    before_records = {
        item.get("rule_id"): item
        for item in (before or {}).get("records", [])
    }
    comparisons = []
    for after in after_records:
        previous = before_records.get(after.get("rule_id"), {})
        comparisons.append(
            {
                "rule_id": after.get("rule_id"),
                "before_candidate_id": previous.get("approved_candidate_id"),
                "after_candidate_id": after.get("candidate_id"),
                "before_source_chunk": previous.get("source_chunk"),
                "after_source_chunk": after.get("source_chunk"),
                "before_selection_score": previous.get("selection_score"),
                "after_selection_score": after.get("selection_score"),
                "selection_changed": previous.get("approved_candidate_id") not in {None, after.get("candidate_id")},
            }
        )
    return comparisons


def _raw_conflict_summary(events: list[dict]) -> list[dict]:
    by_rule: dict[str, dict[str, list[dict]]] = {}
    for index, event in enumerate(events, start=1):
        item = event.get("agent_output") or {}
        rule_id = str(item.get("rule_id") or "").strip()
        if not rule_id:
            continue
        value = _float_or_none(item.get("value"))
        unit = str(item.get("unit") or "")
        operator = str(item.get("operator") or "")
        key = f"{operator}:{value:g}:{unit}" if value is not None else f"{operator}:missing:{unit}"
        by_rule.setdefault(rule_id, {}).setdefault(key, []).append(
            {
                "event_id": f"E{index:04d}",
                "status": event.get("status"),
                "rejection_reason": event.get("rejection_reason"),
                "source_page": item.get("source_page"),
                "evidence_chunk_id": _normalize_evidence_id(str(item.get("evidence_chunk_id") or "")),
                "source_text": str(item.get("source_text") or "")[:300],
            }
        )
    conflicts = []
    for rule_id, values in sorted(by_rule.items()):
        if len(values) <= 1:
            continue
        conflicts.append(
            {
                "rule_id": rule_id,
                "conflict_type": "raw_agent_value_or_operator_conflict",
                "conflicting_values": values,
            }
        )
    return conflicts


def require_agent_generated_rules(rules: list[CanonicalRule]) -> list[CanonicalRule]:
    invalid = [
        rule.rule_id
        for rule in rules
        if rule.extraction_metadata.get("rule_source") != "agent_decision"
        or rule.extraction_metadata.get("agent_generated") is not True
        or not rule.extraction_metadata.get("candidate_id")
        or not rule.extraction_metadata.get("evidence_chunk_id")
        or rule.extraction_metadata.get("retrieval_rank") is None
        or rule.extraction_metadata.get("selection_score") is None
        or not rule.extraction_metadata.get("selection_explanation")
    ]
    if invalid:
        raise RuntimeError(f"Canonical rules failed agent-purity validation: {', '.join(invalid)}")
    return rules
