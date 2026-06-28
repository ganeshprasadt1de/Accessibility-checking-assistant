from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.document_intelligence import ProviderConfig, ingest_pdf
from backend.approved_rule_library import ApprovedRuleLibrary
from backend.generated_shacl import generate_accessibility_shacl
from backend.ontology_mapping import OntologyMappingAgent, apply_mapping_reviews
from backend.review_workflow import HumanReviewWorkflow, require_approved_rules
from backend.shacl_runner import run_shacl
from backend.requirement_extraction import RequirementExtractionAgent, require_agent_generated_rules


class ScriptedAgentProvider:
    name = "scripted_agent_provider"

    def generate_json(self, prompt: str, timeout: int = 180) -> dict:
        evidence_id = _first_evidence_id(prompt)
        return {
            "rules": [
                _agent_rule("door_width", "door", "clear_width", ">=", 0.9, "m", evidence_id),
                _agent_rule("corridor_width", "corridor", "clear_width", ">=", 1.5, "m", evidence_id),
                _agent_rule("turning_space", "movement_area", "turning_width", ">=", 1.5, "m", evidence_id),
                _agent_rule("ramp_slope", "ramp", "slope", "<=", 6.0, "%", evidence_id),
                _agent_rule("ramp_width", "ramp", "usable_width", ">=", 1.2, "m", evidence_id),
            ]
        }


def _make_image_pdf(pdf_path: Path, text: str) -> None:
    import fitz

    image_path = pdf_path.with_suffix(".png")
    source = fitz.open()
    source_page = source.new_page(width=1100, height=520)
    source_page.insert_textbox(fitz.Rect(30, 30, 1060, 490), text, fontsize=34)
    pixmap = source_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pixmap.save(image_path)
    source.close()

    doc = fitz.open()
    page = doc.new_page(width=1100, height=520)
    page.insert_image(fitz.Rect(0, 0, 1100, 520), filename=str(image_path))
    doc.save(pdf_path)
    doc.close()


class OperationalAgentTests(unittest.TestCase):
    def test_text_pdf_to_shacl_runs_complete_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text-rules.pdf"
            _make_text_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            shapes_path, report = _extract_approve_generate_validate(document, Path(tmp))
            self.assertTrue(shapes_path.exists())
            self.assertIn("candidateId", shapes_path.read_text(encoding="utf-8"))
            self.assertTrue(report["available"])
            self.assertGreaterEqual(report["resultCount"], 1)

    def test_scanned_pdf_runs_real_ocr_and_produces_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "scanned.pdf"
            _make_image_pdf(pdf_path, "Rollstuhl Durchgang mindestens 90 cm")
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
        ocr_text = " ".join(chunk.ocr_text for chunk in document.image_chunks)
        self.assertTrue(document.image_chunks)
        self.assertGreater(len(ocr_text.strip()), 0)

    def test_scanned_pdf_ocr_to_shacl_runs_complete_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "scanned-rules.pdf"
            _make_image_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            ocr_text = " ".join(chunk.ocr_text for chunk in document.image_chunks)
            shapes_path, report = _extract_approve_generate_validate(document, Path(tmp))
            self.assertGreater(len(ocr_text.strip()), 0)
            self.assertTrue(shapes_path.exists())
            self.assertTrue(report["available"])

    def test_diagram_pdf_runs_qwen_vl_and_produces_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "diagram.pdf"
            _make_image_pdf(pdf_path, "Wheelchair turning space 150 cm x 150 cm")
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=1),
            )
        vision_text = " ".join(chunk.vision_text for chunk in document.image_chunks)
        self.assertTrue(document.image_chunks)
        self.assertGreater(len(vision_text.strip()), 0)

    def test_diagram_pdf_vision_to_shacl_runs_complete_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "diagram-rules.pdf"
            _make_image_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=1),
            )
            vision_text = " ".join(chunk.vision_text for chunk in document.image_chunks)
            shapes_path, report = _extract_approve_generate_validate(document, Path(tmp))
            self.assertGreater(len(vision_text.strip()), 0)
            self.assertTrue(shapes_path.exists())
            self.assertTrue(report["available"])

    def test_human_rejection_blocks_shacl_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text.pdf"
            _make_image_pdf(pdf_path, "Rollstuhl Durchgang mindestens 90 cm")
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
            pending = HumanReviewWorkflow().create_pending(rules)
            with self.assertRaises(RuntimeError):
                generate_accessibility_shacl(require_approved_rules([]), Path(tmp) / "blocked.ttl")
            self.assertTrue(pending)

    def test_automatic_batch_approval_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text.pdf"
            _make_text_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
            with self.assertRaises(RuntimeError):
                HumanReviewWorkflow().apply_cli_decisions(rules, "preprocess-reviewer", "automatic approval")

    def test_invalid_shacl_blocks_approved_library_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text-rules.pdf"
            _make_text_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
            approved, review_audit, mappings = _review_rules_and_mappings(rules, Path(tmp))
            bad_shapes = Path(tmp) / "bad.shacl.ttl"
            bad_shapes.write_text("@prefix sh: <http://www.w3.org/ns/shacl#> .\nacc:Broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ApprovedRuleLibrary().publish(approved, bad_shapes, Path(tmp) / "library.json", human_review_audit=review_audit, mappings=mappings)

    def test_unresolved_conflict_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text-rules.pdf"
            _make_text_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            rules = require_agent_generated_rules(RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document))
            approved, review_audit, mappings = _review_rules_and_mappings(rules, Path(tmp))
            shapes_path = Path(tmp) / "generated.shacl.ttl"
            generate_accessibility_shacl(require_approved_rules(approved), shapes_path, mappings)
            governance_audit = {
                "conflict_records": [
                    {
                        "conflict_id": "conflict-test",
                        "rule_id": "test",
                        "conflicting_candidates": ["C1", "C2"],
                        "supporting_evidence": [],
                        "confidence_comparison": [],
                        "resolution_status": "unresolved",
                    }
                ]
            }
            with self.assertRaises(RuntimeError):
                ApprovedRuleLibrary().publish(approved, shapes_path, Path(tmp) / "library.json", governance_audit, human_review_audit=review_audit, mappings=mappings)

    def test_unresolved_duplicate_group_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "text-rules.pdf"
            _make_text_pdf(pdf_path, _all_rules_text())
            document = ingest_pdf(
                pdf_path,
                image_output_dir=Path(tmp) / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )
            rules = require_agent_generated_rules(RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document))
            approved, review_audit, mappings = _review_rules_and_mappings(rules, Path(tmp))
            shapes_path = Path(tmp) / "generated.shacl.ttl"
            generate_accessibility_shacl(require_approved_rules(approved), shapes_path, mappings)
            governance_audit = {
                "duplicate_groups": [
                    {
                        "duplicate_group_id": "dup-test",
                        "members": ["C1", "C2"],
                        "selected_candidate": "",
                        "merge_rationale": "",
                        "resolution_status": "unresolved",
                    }
                ]
            }
            with self.assertRaises(RuntimeError):
                ApprovedRuleLibrary().publish(approved, shapes_path, Path(tmp) / "library.json", governance_audit, human_review_audit=review_audit, mappings=mappings)


def _make_text_pdf(pdf_path: Path, text: str) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=900, height=400)
    page.insert_textbox(fitz.Rect(30, 30, 860, 360), text, fontsize=14)
    doc.save(pdf_path)
    doc.close()


def _all_rules_text() -> str:
    return (
        "Rollstuhl Durchgang lichte Breite mindestens 90 cm. "
        "Rollstuhl Verkehrsfläche nutzbare Breite mindestens 150 cm. "
        "Rollstuhl Wenden und Richtungswechsel Bewegungsfläche 150 cm mal 150 cm. "
        "Rampe Neigung maximal 6 %. "
        "Rampe nutzbare Laufbreite mindestens 120 cm."
    )


def _wrap_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        if sum(len(item) + 1 for item in current) + len(word) > max_chars and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def _extract_approve_generate_validate(document, tmp_path: Path):
    rules = require_agent_generated_rules(RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document))
    approved, review_audit, mappings = _review_rules_and_mappings(rules, tmp_path)
    shapes_path = tmp_path / "generated.shacl.ttl"
    data_graph = tmp_path / "data.ttl"
    data_graph.write_text(
        """@prefix acc: <https://example.org/wheelchair-accessibility#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

acc:door1 a acc:IfcDoor ;
    acc:derivedDoorWidthM "0.80"^^xsd:decimal .
acc:space1 a acc:IfcSpace ;
    acc:derivedClearSpaceWidthM "1.20"^^xsd:decimal ;
    acc:turningSpaceM "1.20"^^xsd:decimal .
acc:ramp1 a acc:IfcRamp ;
    acc:rampSlopePercent "8.0"^^xsd:decimal ;
    acc:rampUsableWidthM "1.00"^^xsd:decimal .
acc:route1 a acc:RouteEdge ;
    acc:routeDoorWidthMinM "0.80"^^xsd:decimal ;
    acc:routeClearWidthM "1.20"^^xsd:decimal ;
    acc:routeHasTurn true ;
    acc:routeTurningSpaceM "1.20"^^xsd:decimal ;
    acc:routeRampSlopePercent "8.0"^^xsd:decimal ;
    acc:routeRampUsableWidthM "1.00"^^xsd:decimal .
""",
        encoding="utf-8",
    )
    generate_accessibility_shacl(require_approved_rules(approved), shapes_path, mappings)
    ApprovedRuleLibrary().publish(approved, shapes_path, tmp_path / "approved_rule_library.json", human_review_audit=review_audit, mappings=mappings)
    report = run_shacl(data_graph, shapes_path, tmp_path / "report.ttl")
    return shapes_path, report


def _review_rules_and_mappings(rules, tmp_path: Path):
    mapping_agent = OntologyMappingAgent()
    mappings = mapping_agent.map_rules(rules, _test_data_graph(tmp_path))
    review = HumanReviewWorkflow()
    review.create_queue(rules, mappings, {"duplicate_groups": []}, mapping_agent.audit)
    approved, rejected, escalated = review.apply_cli_decisions(rules, "unit-reviewer", "accepted for test")
    review_audit = review.save(approved, rejected, escalated, tmp_path / "human_review_audit.json")
    mapping_decisions = [decision for decision in review.decisions if decision.review_id.startswith("RV-MAP-")]
    reviewed_mappings = apply_mapping_reviews(mappings, mapping_decisions)
    return approved, review_audit, reviewed_mappings


def _test_data_graph(tmp_path: Path):
    from rdflib import Graph

    data_graph = tmp_path / "data.ttl"
    if not data_graph.exists():
        data_graph.write_text(
            """@prefix acc: <https://example.org/wheelchair-accessibility#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

acc:door1 a acc:IfcDoor ;
    rdfs:label "Door opening" ;
    acc:derivedDoorWidthM "0.80"^^xsd:decimal .
acc:space1 a acc:IfcSpace ;
    rdfs:label "Corridor movement area" ;
    acc:derivedClearSpaceWidthM "1.20"^^xsd:decimal ;
    acc:turningSpaceM "1.20"^^xsd:decimal .
acc:ramp1 a acc:IfcRamp ;
    rdfs:label "Ramp" ;
    acc:rampSlopePercent "8.0"^^xsd:decimal ;
    acc:rampUsableWidthM "1.00"^^xsd:decimal .
""",
            encoding="utf-8",
        )
    graph = Graph()
    graph.parse(data_graph, format="turtle")
    return graph


def _first_evidence_id(prompt: str) -> str:
    import re

    match = re.search(r"\[(?:text|table|image|page_context) ([^ ]+) page", prompt)
    return match.group(1) if match else "p001-page-context"


def _agent_rule(rule_id: str, entity: str, prop: str, operator: str, value: float, unit: str, evidence_id: str) -> dict:
    return {
        "rule_id": rule_id,
        "entity": entity,
        "property": prop,
        "operator": operator,
        "value": value,
        "unit": unit,
        "source_text": f"Agent selected explicit evidence for {rule_id} {value} {unit}",
        "source_page": 1,
        "confidence": 0.9,
        "evidence_chunk_id": evidence_id,
        "evidence_chunk_type": "page_context",
    }


if __name__ == "__main__":
    unittest.main()
