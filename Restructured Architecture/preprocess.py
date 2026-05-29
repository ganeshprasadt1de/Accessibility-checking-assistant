from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.config import ROOT, default_ifctolbd_zip
from backend.geometry import extract_elements
from backend.glb_export import export_box_glb
from backend.ifc_tools import (
    add_geometry_to_graph,
    create_raw_lbd_fallback,
    element_uri,
    load_raw_graph,
    try_ifctolbd,
)
from backend.package_writer import write_json_package
from backend.routes import add_routes_to_graph, build_route_edges, save_route_binary
from backend.rules import evaluate_value_rules
from backend.shacl_runner import run_shacl
from backend.audit import write_audit_report
from backend.model import Issue
from backend.short_explainer import fallback
from rdflib import Literal, Namespace, RDF
from rdflib.namespace import XSD

ACC = Namespace("https://example.org/wheelchair-accessibility#")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preprocess an IFC model for wheelchair route checking.")
    parser.add_argument("--ifc", type=Path, default=ROOT / "AC20-Institute-Var-2.ifc", help="Path to one IFC file.")
    parser.add_argument("--ifctolbd-zip", type=Path, default=default_ifctolbd_zip(), help="Path to IFCtoLBD-master.zip.")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "app_package", help="Output app package folder.")
    parser.add_argument("--save-bin", action="store_true", help="Save route_graph.bin for fast route loading.")
    args = parser.parse_args()

    output = args.output.resolve()
    work = output / "_work"
    work.mkdir(parents=True, exist_ok=True)
    ifc_path = args.ifc.resolve()

    print(f"Reading IFC: {ifc_path}")
    elements, missing_geometry = extract_elements(ifc_path)
    print(f"Extracted elements: {len(elements)}")

    raw_ttl = output / "raw_lbd_graph.ttl"
    ok, ifctolbd_note = try_ifctolbd(ifc_path, args.ifctolbd_zip.resolve(), raw_ttl, work)
    if not ok:
        create_raw_lbd_fallback(elements, raw_ttl)
    print(ifctolbd_note)

    graph = load_raw_graph(raw_ttl)
    add_geometry_to_graph(graph, elements)
    edges = build_route_edges(ifc_path, elements)
    add_routes_to_graph(graph, edges)
    issues = evaluate_value_rules(elements)
    _add_route_issues(issues, elements, edges)
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
    lbd_ttl = output / "lbd_graph.ttl"
    graph.serialize(destination=lbd_ttl, format="turtle")

    shacl_summary = run_shacl(lbd_ttl, ROOT / "rules" / "accessibility_rules.shacl.ttl", output / "shacl_report.ttl")
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


def _add_route_issues(issues: list[Issue], elements, edges) -> None:
    by_guid = {element.guid: element for element in elements}
    for edge in edges:
        if edge.status != "fail":
            continue
        element = by_guid.get(edge.via_space_guid) or by_guid.get(edge.start_guid)
        if not element:
            continue
        rule_id = edge.reasons[0] if edge.reasons else "unreachable"
        issues.append(
            Issue(
                issue_id=f"I{len(issues) + 1:04d}",
                element_guid=element.guid,
                element_label=element.label,
                element_type=element.ifc_type,
                rule_id=rule_id,
                severity="fail",
                measured=edge.distance_m,
                required=None,
                unit="m",
                source="Indoor route path check",
                short_text=fallback(rule_id),
                details=f"Route edge {edge.edge_id} failed: {', '.join(edge.reasons)}.",
            )
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Preprocess failed: {exc}", file=sys.stderr)
        raise
