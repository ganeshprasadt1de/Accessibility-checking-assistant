from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import Graph

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.document_intelligence import (
    ImageChunk,
    PageMetadata,
    PaddleOCRProvider,
    ProviderConfig,
    ProviderRegistry,
    OllamaVisionProvider,
    StructuredDocument,
    TableChunk,
    TextChunk,
    ingest_pdf,
    rank_image_chunks,
)
from backend.generated_shacl import generate_accessibility_shacl
from backend.ontology_mapping import OntologyMappingAgent
from backend.processing_cache import ProcessingCache
from backend.requirement_extraction import RequirementExtractionAgent, _best_rules, require_agent_generated_rules
from backend.rule_repository import JSONRuleRepository
from backend.shacl_runner import issues_from_python_route_geometry


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


class DocumentRulePipelineTests(unittest.TestCase):
    def test_ingest_pdf_extracts_page_text_image_and_metadata(self) -> None:
        try:
            import fitz
        except Exception as exc:
            self.skipTest(f"PyMuPDF is not available: {exc}")

        one_pixel_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            image_path = tmp_path / "pixel.png"
            image_path.write_bytes(one_pixel_png)

            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Rollstuhl Durchgang mindestens 90 cm.")
            page.insert_image(fitz.Rect(72, 100, 92, 120), filename=str(image_path))
            doc.save(pdf_path)
            doc.close()

            structured = ingest_pdf(
                pdf_path,
                image_output_dir=tmp_path / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )

            self.assertEqual(len(structured.pages), 1)
            self.assertGreaterEqual(len(structured.text_chunks), 1)
            self.assertGreaterEqual(len(structured.image_chunks), 1)
            self.assertEqual(structured.pages[0].image_count, len(structured.image_chunks))
            self.assertIn("Rollstuhl", structured.text_chunks[0].text)
            self.assertTrue(Path(structured.image_chunks[0].image_path or "").exists())
            self.assertTrue(any(chunk.metadata.get("image_source") == "page_render" for chunk in structured.image_chunks))

    def test_ingest_pdf_renders_vector_page_as_image_evidence(self) -> None:
        try:
            import fitz
        except Exception as exc:
            self.skipTest(f"PyMuPDF is not available: {exc}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "vector-page.pdf"
            doc = fitz.open()
            page = doc.new_page(width=500, height=300)
            page.insert_text((40, 60), "Minimum clearance 120 cm", fontsize=24)
            page.draw_rect(fitz.Rect(40, 90, 420, 230), width=2)
            page.draw_line(fitz.Point(40, 250), fitz.Point(420, 250), width=2)
            doc.save(pdf_path)
            doc.close()

            structured = ingest_pdf(
                pdf_path,
                image_output_dir=tmp_path / "images",
                provider_config=ProviderConfig(max_vision_images=0),
            )

            rendered = [chunk for chunk in structured.image_chunks if chunk.metadata.get("image_source") == "page_render"]
            self.assertTrue(rendered)
            self.assertTrue(Path(rendered[0].image_path or "").exists())
            self.assertIn("120", rendered[0].ocr_text)

    def test_processing_cache_reuses_document_images_and_extraction_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "rules.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% cache identity test\n")
            first_images = tmp_path / "first_images"
            first_images.mkdir()
            image_path = first_images / "page-001-page_render-001.png"
            image_path.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
            ))
            document = StructuredDocument(
                source_pdf=str(pdf_path),
                metadata={"ocr_provider": "paddleocr", "vision_provider": "ollama_qwen3-vl:8b"},
                pages=[PageMetadata(1, 100, 100, 0, 20, 1, 0)],
                text_chunks=[TextChunk("p001-text-001", 1, "Minimum clear width 90 cm.")],
                image_chunks=[
                    ImageChunk(
                        "p001-page_render-001",
                        1,
                        1,
                        1,
                        1,
                        "png",
                        str(image_path),
                        ocr_text="Minimum clear width 90 cm.",
                        vision_text="Door usable width 90 cm.",
                    )
                ],
                table_chunks=[],
            )
            cache = ProcessingCache(tmp_path / "cache")
            document_key = cache.document_key(pdf_path, ProviderConfig(max_vision_images=3))
            cache.save_document(document_key, pdf_path, document, first_images)

            loaded_images = tmp_path / "loaded_images"
            loaded_document = cache.load_document(document_key, pdf_path, loaded_images)

            self.assertIsNotNone(loaded_document)
            self.assertTrue((loaded_images / "page-001-page_render-001.png").exists())
            self.assertEqual(loaded_document.image_chunks[0].ocr_text, "Minimum clear width 90 cm.")
            self.assertEqual(Path(loaded_document.image_chunks[0].image_path or "").parent, loaded_images)

            agent = RequirementExtractionAgent(llm=ScriptedAgentProvider())
            rules = [_canonical_candidate("C0001", 0.8, "p001-text-001", "Minimum clear width 90 cm.", "Minimum clear width 90 cm.", ">=")]
            agent.metrics = {"llm_calls": 1, "rules_from_llm": 1, "rules_rejected": 0}
            agent.audit_events = [{"status": "accepted", "candidate_id": "C0001"}]
            agent.retrieval_audit = {"selected_chunks": [{"chunk_id": "p001-text-001"}]}
            agent.governance_audit = {"candidate_count": 1}
            agent._candidate_counter = 1
            extraction_key = cache.extraction_key(document_key, agent.llm, ROOT / "rules" / "canonical_rule_schema.json")
            cache.save_extraction(extraction_key, agent, rules)

            restored_agent = RequirementExtractionAgent(llm=ScriptedAgentProvider())
            restored_rules = cache.load_extraction(extraction_key, restored_agent)

            self.assertEqual([rule.rule_id for rule in restored_rules or []], ["door_width"])
            self.assertEqual(restored_agent.metrics["llm_calls"], 1)
            self.assertEqual(restored_agent.audit_events[0]["candidate_id"], "C0001")

    def test_python_route_geometry_stair_block_creates_visible_issue_without_shacl_failure(self) -> None:
        from backend.model import Element, RouteEdge

        door = Element(
            guid="door-1",
            ifc_type="IfcDoor",
            name="Door 1",
            label="Door 1",
            center=(0, 0, 0),
        )
        stair = Element(
            guid="stair-1",
            ifc_type="IfcStair",
            name="Stair 1",
            label="Stair 1",
            center=(1, 0, 0),
        )
        edge = RouteEdge(
            edge_id="E00001",
            start_guid=door.guid,
            end_guid=stair.guid,
            distance_m=1.0,
            status="pass",
            reasons=[],
            path=[door.center, stair.center],
            via_space_guid=stair.guid,
            via_space_label=stair.label,
            measurements={"routeHitsStair": True},
        )

        issues = issues_from_python_route_geometry([door, stair], [edge], [])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].rule_id, "stair_block")
        self.assertEqual(issues[0].source, "Route geometry")
        self.assertEqual(edge.status, "fail")
        self.assertIn("stair_block", edge.reasons)

    def test_requirement_agent_reads_text_table_and_image_evidence(self) -> None:
        document = StructuredDocument(
            source_pdf="sample.pdf",
            metadata={},
            pages=[PageMetadata(1, 100, 100, 0, 0, 1, 1)],
            text_chunks=[
                TextChunk("p001-text-001", 7, "Rollstuhl Durchgang lichte Breite mindestens 90 cm."),
                TextChunk("p011-text-001", 11, "Verkehrsfläche nutzbare Breite mindestens 150 cm."),
            ],
            table_chunks=[
                TableChunk(
                    "p017-table-001",
                    17,
                    "Rampe | Neigung 6 % | nutzbare Laufbreite 120 cm",
                    row_count=1,
                    column_count=3,
                    extraction_method="unit_test",
                )
            ],
            image_chunks=[
                ImageChunk(
                    "p007-image-001",
                    7,
                    1,
                    100,
                    80,
                    "png",
                    None,
                    vision_text="Rollstuhl wenden Richtungswechsel Bewegungsfläche 150 cm mal 150 cm",
                )
            ],
        )

        rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
        by_id = {rule.rule_id: rule for rule in rules}

        self.assertEqual(set(by_id), {"corridor_width", "door_width", "ramp_slope", "ramp_width", "turning_space"})
        self.assertTrue(all(rule.extraction_metadata["agent_generated"] for rule in rules))
        self.assertTrue(all(rule.extraction_metadata["rule_source"] == "agent_decision" for rule in rules))
        self.assertEqual(by_id["door_width"].value, 0.9)
        self.assertEqual(by_id["ramp_slope"].operator, "<=")

    def test_generated_shacl_from_agent_rules_is_parseable(self) -> None:
        document = StructuredDocument(
            source_pdf="sample.pdf",
            metadata={},
            pages=[],
            text_chunks=[
                TextChunk("p001-text-001", 7, "Rollstuhl Durchgang mindestens 90 cm. Wenden Richtungswechsel 150 cm."),
                TextChunk("p011-text-001", 11, "Verkehrsfläche nutzbare Breite mindestens 150 cm."),
                TextChunk("p017-text-001", 17, "Rampe Neigung 6 % und nutzbare Laufbreite 120 cm."),
            ],
            table_chunks=[],
            image_chunks=[],
        )
        rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
        with tempfile.TemporaryDirectory() as tmp:
            shapes_path = Path(tmp) / "generated.shacl.ttl"
            data_graph = _mapping_data_graph()
            mappings = OntologyMappingAgent().map_rules(require_agent_generated_rules(rules), data_graph)
            generate_accessibility_shacl(require_agent_generated_rules(rules), shapes_path, mappings)
            graph = Graph().parse(shapes_path, format="turtle")
            shape_text = shapes_path.read_text(encoding="utf-8")

        self.assertGreater(len(graph), 0)
        self.assertIn("mappingCandidateId", shape_text)

    def test_provider_registry_uses_final_providers(self) -> None:
        registry = ProviderRegistry(ProviderConfig(max_vision_images=0))

        self.assertIsInstance(registry.ocr(), PaddleOCRProvider)
        self.assertIsInstance(registry.vision(), OllamaVisionProvider)

    def test_image_ranker_prefers_high_quality_evidence(self) -> None:
        images = [
            ImageChunk("p001-image-001", 1, 1, 20, 20, "png", None, ocr_text="logo"),
            ImageChunk("p002-image-001", 2, 1, 500, 400, "png", None, ocr_text="minimum required clearance 1.20 m and maximum slope 6 %"),
        ]
        text_chunks = [
            TextChunk("p001-text-001", 1, "General introduction."),
            TextChunk("p002-text-001", 2, "The drawing states a mandatory minimum dimension and limit."),
        ]
        pages = [PageMetadata(1, 600, 800, 0, 20, 1, 0), PageMetadata(2, 600, 800, 0, 80, 1, 0)]

        ranked = rank_image_chunks(images, text_chunks, [], pages, max_selected=1)

        self.assertEqual(ranked[0]["chunk_id"], "p002-image-001")
        self.assertTrue(ranked[0]["selected_for_vision"])
        self.assertFalse(ranked[1]["selected_for_vision"])
        self.assertIn("selection_reason", ranked[0])
        self.assertIn("exclusion_reason", ranked[1])

    def test_rule_repository_saves_and_loads_canonical_rules(self) -> None:
        document = StructuredDocument(
            source_pdf="sample.pdf",
            metadata={},
            pages=[],
            text_chunks=[
                TextChunk("p001-text-001", 7, "Rollstuhl Durchgang mindestens 90 cm. Wenden Richtungswechsel 150 cm."),
                TextChunk("p011-text-001", 11, "Verkehrsfläche nutzbare Breite mindestens 150 cm."),
                TextChunk("p017-text-001", 17, "Rampe Neigung 6 % und nutzbare Laufbreite 120 cm."),
            ],
            table_chunks=[],
            image_chunks=[],
        )
        rules = RequirementExtractionAgent(llm=ScriptedAgentProvider()).extract(document)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            repo = JSONRuleRepository()
            repo.save(rules, Path("sample.pdf"), {"test": True}, path)
            loaded = repo.load(path)

        self.assertEqual([rule.rule_id for rule in loaded], [rule.rule_id for rule in rules])

    def test_non_agent_rule_is_rejected_by_purity_gate(self) -> None:
        from backend.canonical_rules import CanonicalRule

        rule = CanonicalRule(
            rule_id="door_width",
            entity="door",
            property="clear_width",
            operator=">=",
            value=0.9,
            unit="m",
            source_text="Durchgang mindestens 90 cm",
            source_page=1,
            confidence=0.9,
            extraction_metadata={"repair_path": True},
        )

        with self.assertRaises(RuntimeError):
            require_agent_generated_rules([rule])

    def test_candidate_governance_prefers_stronger_evidence_over_confidence(self) -> None:
        from backend.canonical_rules import CanonicalRule

        weak = _canonical_candidate(
            "C0001",
            confidence=1.0,
            evidence_chunk_id="p001-text-001",
            evidence_text="The service counter has a height of 90 cm.",
            source_text="[text p001-text-001 page 1]",
            raw_operator="<=",
        )
        strong = _canonical_candidate(
            "C0002",
            confidence=0.92,
            evidence_chunk_id="p001-table-001",
            evidence_text="Component | Geometry | Measure cm | Passage | clear width | >= 90",
            source_text="Passage clear width >= 90 cm",
            raw_operator=">=",
        )
        retrieval_audit = {
            "selected_chunks": [
                {"chunk_id": "p001-text-001", "rank": 2, "final_score": 0.8},
                {"chunk_id": "p001-table-001", "rank": 8, "final_score": 0.7},
            ],
            "excluded_chunks": [],
        }

        selected, governance = _best_rules([weak, strong], retrieval_audit)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].extraction_metadata["candidate_id"], "C0002")
        self.assertGreater(selected[0].extraction_metadata["selection_score"], 0)
        self.assertIn("duplicate_groups", governance)
        self.assertEqual(governance["duplicate_groups"][0]["selected_candidate"], "C0002")


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


def _canonical_candidate(
    candidate_id: str,
    confidence: float,
    evidence_chunk_id: str,
    evidence_text: str,
    source_text: str,
    raw_operator: str,
):
    from backend.canonical_rules import CanonicalRule

    return CanonicalRule(
        rule_id="door_width",
        entity="door",
        property="clear_width",
        operator=">=",
        value=0.9,
        unit="m",
        source_text=source_text,
        source_page=1,
        confidence=confidence,
        extraction_metadata={
            "extractor": "unit_test",
            "llm_used": True,
            "agent_generated": True,
            "rule_source": "agent_decision",
            "candidate_id": candidate_id,
            "evidence_chunk_id": evidence_chunk_id,
            "evidence_chunk_type": "table" if "table" in evidence_chunk_id else "text",
            "evidence_text": evidence_text,
            "agent_batch_index": 1,
            "raw_agent_rule": {
                "rule_id": "door_width",
                "entity": "door",
                "property": "clear_width",
                "operator": raw_operator,
                "value": 90,
                "unit": "cm",
                "source_text": source_text,
                "source_page": 1,
            },
        },
    )


def _mapping_data_graph() -> Graph:
    graph = Graph()
    graph.parse(
        data="""@prefix acc: <https://example.org/wheelchair-accessibility#> .
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
        format="turtle",
    )
    return graph


if __name__ == "__main__":
    unittest.main()
