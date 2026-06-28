from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from backend.config import ROOT, default_ifctolbd_zip
from backend.geometry import extract_elements
from backend.glb_export import export_box_glb
from backend.ifc_tools import (
    add_geometry_to_graph,
    element_uri,
    load_raw_graph,
    run_ifctolbd,
)
from backend.package_writer import write_json_package
from backend.routes import add_routes_to_graph, build_route_edges, save_route_binary
from backend.shacl_runner import issues_from_python_route_geometry, issues_from_shacl_report, run_shacl
from backend.audit import write_audit_report
from backend.approved_rule_library import ApprovedRuleLibrary
from backend.document_intelligence import ingest_pdf, save_structured_document
from backend.generated_shacl import generate_accessibility_shacl
from backend.governance import save_governance_audit, unresolved_conflicts
from backend.ontology_mapping import OntologyMappingAgent, apply_mapping_reviews, save_mapping_audit, save_mappings
from backend.processing_cache import ProcessingCache
from backend.requirement_extraction import RequirementExtractionAgent, require_agent_generated_rules, write_candidate_selection_audit_report, write_extraction_audit_report
from backend.review_workflow import HumanReviewWorkflow, require_approved_rules
from backend.rule_repository import JSONRuleRepository
from rdflib import Literal, Namespace, RDF
from rdflib.namespace import XSD

ACC = Namespace("https://example.org/wheelchair-accessibility#")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preprocess an IFC model and validate it with generated SHACL rules.")
    parser.add_argument("--ifc", type=Path, default=ROOT / "AC20-Institute-Var-2.ifc", help="Path to one IFC file.")
    parser.add_argument("--ifctolbd-zip", type=Path, default=default_ifctolbd_zip(), help="Path to IFCtoLBD-master.zip.")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "app_package", help="Output app package folder.")
    parser.add_argument("--rules-pdf", type=Path, default=ROOT / "planungsgrundlagen_barrierefreies_bauen.pdf", help="Technical standard PDF used to derive canonical rules.")
    parser.add_argument("--save-bin", action="store_true", help="Save route_graph.bin for fast route loading.")
    parser.add_argument("--approve-rules", action="store_true", help="Explicitly approve extracted canonical rules for SHACL generation.")
    parser.add_argument("--reviewer", default="", help="Human reviewer name used when --approve-rules is set.")
    parser.add_argument("--review-rationale", default="", help="Human review rationale used when --approve-rules is set.")
    parser.add_argument("--reject-rules", default="", help="Comma-separated rule IDs to reject during explicit review.")
    parser.add_argument("--escalate-rules", default="", help="Comma-separated rule IDs to escalate during explicit review.")
    parser.add_argument("--no-cache", action="store_true", help="Disable PDF document and rule extraction cache.")
    args = parser.parse_args()

    output = args.output.resolve()
    work = output / "_work"
    work.mkdir(parents=True, exist_ok=True)
    ifc_path = args.ifc.resolve()

    print(f"Reading IFC: {ifc_path}")
    elements, missing_geometry = extract_elements(ifc_path)
    print(f"Extracted elements: {len(elements)}")

    raw_ttl = output / "raw_lbd_graph.ttl"
    ifctolbd_note = run_ifctolbd(ifc_path, args.ifctolbd_zip.resolve(), raw_ttl, work)
    print(ifctolbd_note)

    graph = load_raw_graph(raw_ttl)
    add_geometry_to_graph(graph, elements)
    edges = build_route_edges(ifc_path, elements)
    add_routes_to_graph(graph, edges)
    lbd_ttl = output / "lbd_graph.ttl"
    graph.serialize(destination=lbd_ttl, format="turtle")

    rules_pdf = args.rules_pdf.resolve()
    processing_cache = ProcessingCache(output / "_cache")
    cache_report = {"enabled": not args.no_cache, "document": {}, "extraction": {}}
    document_images = output / "document_images"
    document_cache_key = processing_cache.document_key(rules_pdf)
    structured_document = None
    if not args.no_cache:
        structured_document = processing_cache.load_document(document_cache_key, rules_pdf, document_images)
    if structured_document is None:
        cache_report["document"] = {"status": "miss", "cache_key": document_cache_key}
        print("PDF document cache: miss")
        _reset_generated_directory(document_images, output)
        structured_document = ingest_pdf(rules_pdf, image_output_dir=document_images)
        if not args.no_cache:
            processing_cache.save_document(document_cache_key, rules_pdf, structured_document, document_images)
    else:
        cache_report["document"] = {"status": "hit", "cache_key": document_cache_key}
        print("PDF document cache: hit")
    save_structured_document(structured_document, output / "document_chunks.json")
    extraction_agent = RequirementExtractionAgent()
    extraction_cache_key = processing_cache.extraction_key(
        document_cache_key,
        extraction_agent.llm,
        ROOT / "rules" / "canonical_rule_schema.json",
    )
    canonical_rules = None
    if not args.no_cache:
        canonical_rules = processing_cache.load_extraction(extraction_cache_key, extraction_agent)
    if canonical_rules is None:
        cache_report["extraction"] = {"status": "miss", "cache_key": extraction_cache_key}
        print("Rule extraction cache: miss")
        canonical_rules = require_agent_generated_rules(extraction_agent.extract(structured_document))
        if not args.no_cache:
            processing_cache.save_extraction(extraction_cache_key, extraction_agent, canonical_rules)
    else:
        cache_report["extraction"] = {"status": "hit", "cache_key": extraction_cache_key}
        print("Rule extraction cache: hit")
    extraction_audit_path = output / "rule_extraction_audit.json"
    write_extraction_audit_report(extraction_agent, extraction_audit_path)
    extraction_audit = json.loads(extraction_audit_path.read_text(encoding="utf-8"))
    (output / "processing_cache_report.json").write_text(json.dumps(cache_report, indent=2, ensure_ascii=False), encoding="utf-8")
    canonical_rules_path = output / "canonical_rules.json"
    generated_shapes = output / "generated_accessibility_rules.shacl.ttl"
    JSONRuleRepository().save(
        canonical_rules,
        rules_pdf,
        {
            "document_chunks": str(output / "document_chunks.json"),
            "document_chunk_counts": structured_document.to_dict()["chunk_counts"],
            "document_intelligence": structured_document.metadata,
            "rule_extraction_audit": str(extraction_audit_path),
        },
        canonical_rules_path,
    )
    mapping_agent = OntologyMappingAgent()
    ontology_mappings = mapping_agent.map_rules(canonical_rules, graph)
    review = HumanReviewWorkflow()
    review.create_queue(canonical_rules, ontology_mappings, extraction_agent.governance_audit, mapping_agent.audit)
    if not args.approve_rules:
        raise RuntimeError("Explicit human review is required. Re-run with --approve-rules, --reviewer, and --review-rationale after reviewing canonical_rules.json, rule_extraction_audit.json, and ifc_mapping_audit.json.")
    approved_rules, rejected_rules, escalated_rules = review.apply_cli_decisions(
        canonical_rules,
        reviewer=args.reviewer,
        rationale=args.review_rationale,
        reject_rule_ids=_csv_set(args.reject_rules),
        escalate_rule_ids=_csv_set(args.escalate_rules),
    )
    review_path = output / "rule_review.json"
    human_review_audit_path = output / "human_review_audit.json"
    review_audit = review.save(approved_rules, rejected_rules, escalated_rules, review_path)
    human_review_audit_path.write_text(json.dumps(review_audit, indent=2, ensure_ascii=False), encoding="utf-8")
    event_log_path = output / "governance_event_log.json"
    review.save_event_log(event_log_path)
    approved_canonical_rules = require_approved_rules(approved_rules)
    approved_mapping_decisions = [decision for decision in review.decisions if decision.review_id.startswith("RV-MAP-")]
    reviewed_mappings = apply_mapping_reviews(ontology_mappings, approved_mapping_decisions)
    mapped_rule_ids = {item.rule_id for item in reviewed_mappings}
    approved_rules_for_publication = [item for item in approved_rules if item.rule.rule_id in mapped_rule_ids]
    approved_canonical_rules_for_publication = require_approved_rules(approved_rules_for_publication)
    candidate_selection_audit_path = output / "candidate_selection_audit.json"
    write_candidate_selection_audit_report(extraction_agent, approved_canonical_rules, candidate_selection_audit_path)
    mapping_path = output / "ontology_mappings.json"
    save_mappings(reviewed_mappings, mapping_path)
    mapping_audit_path = output / "ifc_mapping_audit.json"
    mapping_agent.audit["review_decisions"] = [
        decision
        for decision in review_audit.get("decision_records", [])
        if decision.get("review_id", "").startswith("RV-MAP-")
    ]
    mapping_agent.audit["selected_mappings"] = [item.__dict__ for item in reviewed_mappings]
    save_mapping_audit(mapping_agent.audit, mapping_audit_path)
    generate_accessibility_shacl(approved_canonical_rules_for_publication, generated_shapes, reviewed_mappings)
    approved_library_path = output / "approved_rule_library.json"
    published_library = ApprovedRuleLibrary().publish(
        approved_rules_for_publication,
        generated_shapes,
        approved_library_path,
        extraction_agent.governance_audit,
        human_review_audit=review_audit,
        mappings=reviewed_mappings,
    )
    review_audit["publication_decisions"] = [published_library.publication_inventory]
    review_audit["publication_failures"] = [
        check
        for check in published_library.publication_inventory.get("checks", [])
        if not check.get("passed")
    ]
    human_review_audit_path.write_text(json.dumps(review_audit, indent=2, ensure_ascii=False), encoding="utf-8")
    review_path.write_text(json.dumps(review_audit, indent=2, ensure_ascii=False), encoding="utf-8")
    review.record_event(args.reviewer, "publication_published", published_library.publication_inventory)
    review.save_event_log(event_log_path)
    governance_audit_path = output / "governance_audit.json"
    save_governance_audit(governance_audit_path, extraction_audit, review_audit, published_library.publication_inventory)
    shacl_summary = run_shacl(lbd_ttl, generated_shapes, output / "shacl_report.ttl")
    shacl_summary["source"] = "Generated SHACL from approved canonical PDF rules"
    shacl_summary["documentChunks"] = str(output / "document_chunks.json")
    shacl_summary["canonicalRules"] = str(canonical_rules_path)
    shacl_summary["ruleExtractionAudit"] = str(extraction_audit_path)
    shacl_summary["candidateSelectionAudit"] = str(candidate_selection_audit_path)
    shacl_summary["ruleReview"] = str(review_path)
    shacl_summary["humanReviewAudit"] = str(human_review_audit_path)
    shacl_summary["governanceEventLog"] = str(event_log_path)
    shacl_summary["ontologyMappings"] = str(mapping_path)
    shacl_summary["ifcMappingAudit"] = str(mapping_audit_path)
    shacl_summary["generatedShapes"] = str(generated_shapes)
    shacl_summary["approvedRuleLibrary"] = str(approved_library_path)
    shacl_summary["governanceAudit"] = str(governance_audit_path)
    shacl_summary["processingCacheReport"] = str(output / "processing_cache_report.json")
    shacl_summary["processingCache"] = cache_report
    issues = issues_from_shacl_report(output / "shacl_report.ttl", lbd_ttl, elements, edges)
    python_route_issues = issues_from_python_route_geometry(elements, edges, issues)
    issues.extend(python_route_issues)
    shacl_summary["pythonRouteGeometryIssues"] = {
        "count": len(python_route_issues),
        "source": "Route geometry",
        "note": "These issues are counted in the app issue list, but they do not change the pySHACL conformance result.",
    }
    for issue in issues:
        issue_uri = ACC[f"issue/{issue.issue_id}"]
        graph.add((issue_uri, RDF.type, ACC.AccessibilityIssue))
        graph.add((issue_uri, ACC.issueElement, element_uri(issue.element_guid)))
        graph.add((issue_uri, ACC.ruleId, Literal(issue.rule_id)))
        graph.add((issue_uri, ACC.severity, Literal(issue.severity)))
        graph.add((issue_uri, ACC.shortExplanation, Literal(issue.short_text)))
        graph.add((issue_uri, ACC.issueSource, Literal(issue.source)))
        if issue.measured is not None:
            graph.add((issue_uri, ACC.measuredValue, Literal(round(issue.measured, 4), datatype=XSD.decimal)))
        if issue.required is not None:
            graph.add((issue_uri, ACC.requiredValue, Literal(round(issue.required, 4), datatype=XSD.decimal)))
    for edge in edges:
        route_uri = ACC[f"route/{edge.edge_id}"]
        graph.set((route_uri, ACC.routeStatus, Literal(edge.status)))
        for reason in edge.reasons:
            graph.add((route_uri, ACC.routeFailureReason, Literal(reason)))
    graph.serialize(destination=lbd_ttl, format="turtle")

    write_json_package(output, elements, issues, edges, missing_geometry, shacl_summary, ifctolbd_note)
    write_audit_report(ifc_path, output, {"routeEdges": [
        {
            "startGuid": edge.start_guid,
            "endGuid": edge.end_guid,
            "status": edge.status,
            "reasons": edge.reasons,
        }
        for edge in edges
    ]})
    export_box_glb(elements, edges, output / "route_model.glb")
    if args.save_bin:
        save_route_binary(edges, output / "route_graph.bin")
    print(f"Wrote package: {output}")
    print(f"Routes: {len(edges)}, issues: {len(issues)}, missing geometry: {len(missing_geometry)}")
    return 0


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _reset_generated_directory(path: Path, output_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = output_root.resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise RuntimeError(f"Refusing to clean directory outside output package: {resolved_path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)
    resolved_path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Preprocess failed: {exc}", file=sys.stderr)
        raise
