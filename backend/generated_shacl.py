from __future__ import annotations

import re
from pathlib import Path

from .canonical_rules import CanonicalRule
from .config import NS
from .ontology_mapping import OntologyMapping


ACC = NS["acc"]


def generate_accessibility_shacl(
    rules: list[CanonicalRule],
    output_path: Path,
    mappings: list[OntologyMapping],
) -> Path:
    mapping_by_rule = {item.rule_id: item for item in mappings}
    mapped_rules = [rule for rule in rules if rule.rule_id in mapping_by_rule]
    if not mapped_rules:
        raise RuntimeError("Cannot generate SHACL because no reviewed IFC mappings are available.")

    text = "\n".join(
        [_prefixes()]
        + [_rule_shape(rule, mapping_by_rule[rule.rule_id]) for rule in mapped_rules]
        + [""]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def _prefixes() -> str:
    return f"""@prefix acc: <{ACC}> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def _rule_shape(rule: CanonicalRule, mapping: OntologyMapping) -> str:
    shape_name = _shape_name(rule.rule_id)
    comparator = _failure_comparator(rule.operator)
    if comparator is None:
        raise RuntimeError(f"Unsupported canonical operator for SHACL generation: {rule.operator}")
    candidate_rule_id = str(rule.extraction_metadata.get("candidate_id", ""))
    evidence_chunk = str(rule.extraction_metadata.get("evidence_chunk_id", ""))
    review = mapping.review_decision
    return f"""
acc:{shape_name}
    a sh:NodeShape ;
    acc:generatedFromRule "{_escape(rule.rule_id)}" ;
    acc:candidateRuleId "{_escape(candidate_rule_id)}" ;
    acc:mappingCandidateId "{_escape(mapping.mapping_candidate_id)}" ;
    acc:selectedIfcTarget <{mapping.ifc_target}> ;
    acc:selectedOntologyProperty <{mapping.ontology_property}> ;
    acc:mappingReviewDecision "{_escape(str(review.get("status", "")))}" ;
    acc:mappingReviewId "{_escape(str(review.get("review_id", "")))}" ;
    acc:mappingDecisionId "{_escape(str(review.get("decision_id", "")))}" ;
    acc:mappingReviewedBy "{_escape(str(review.get("reviewer", "")))}" ;
    acc:mappingReviewTimestamp "{_escape(str(review.get("timestamp", "")))}" ;
    acc:mappingConfidence {mapping.confidence:.6f} ;
    acc:sourcePage {rule.source_page} ;
    acc:evidenceChunk "{_escape(evidence_chunk)}" ;
    sh:targetClass <{mapping.ontology_class}> ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "{_escape(rule.rule_id)}|{_escape(rule.property)} violates {_escape(rule.operator)} {_fmt(rule.value)} {_escape(rule.unit)}. Source page {rule.source_page}." ;
        sh:select \"\"\"
            PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
            SELECT $this WHERE {{
                $this <{mapping.ontology_property}> ?value .
                FILTER(xsd:decimal(?value) {comparator} {_fmt(rule.value)})
            }}
        \"\"\" ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "missing_{_escape(rule.rule_id)}|{_escape(rule.property)} is missing. Source page {rule.source_page}." ;
        sh:select \"\"\"
            SELECT $this WHERE {{
                FILTER NOT EXISTS {{ $this <{mapping.ontology_property}> ?value . }}
            }}
        \"\"\" ;
    ] .
"""


def _failure_comparator(operator: str) -> str | None:
    return {
        ">=": "<",
        ">": "<=",
        "<=": ">",
        "<": ">=",
        "=": "!=",
        "==": "!=",
    }.get(operator.strip())


def _shape_name(rule_id: str) -> str:
    words = [item for item in re.split(r"[^A-Za-z0-9]+", rule_id) if item]
    if not words:
        return "GeneratedRuleShape"
    return "".join(word[:1].upper() + word[1:] for word in words) + "Shape"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
