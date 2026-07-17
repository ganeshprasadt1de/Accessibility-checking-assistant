from __future__ import annotations

import argparse
import json
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
    run_ifctolbd_exe,
)
from backend.package_writer import write_json_package
from backend.navigation import build_navigation_package
from backend.simulation_routes import add_floor_check_routes
from backend.routes import add_routes_to_graph, build_route_edges, save_route_binary
from backend.shacl_runner import issues_from_shacl_report, run_shacl
from backend.audit import write_audit_report
from rdflib import Literal, Namespace, RDF
from rdflib.namespace import XSD

ACC = Namespace("https://example.org/wheelchair-accessibility#")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preprocess an IFC model for wheelchair route checking.")
    parser.add_argument("--ifc", type=Path, default=ROOT / "AC20-Institute-Var-2.ifc", help="Path to one IFC file.")
    parser.add_argument("--ifctolbd-exe", type=Path, default=ROOT / "IFCtoLBDConverter_CLI.exe", help="Path to IFCtoLBDConverter_CLI.exe.")
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
    ifctolbd_note = "raw graph created by IFCtoLBD"
    if args.ifctolbd_exe.exists():
        ifctolbd_note = run_ifctolbd_exe(ifc_path, args.ifctolbd_exe.resolve(), raw_ttl)
    else:
        ifctolbd_note = run_ifctolbd(ifc_path, args.ifctolbd_zip.resolve(), raw_ttl, work)
    graph = load_raw_graph(raw_ttl)
    print(ifctolbd_note)
    add_geometry_to_graph(graph, elements)
    edges = build_route_edges(ifc_path, elements)
    add_routes_to_graph(graph, edges)
    lbd_ttl = output / "lbd_graph.ttl"
    graph.serialize(destination=lbd_ttl, format="turtle")

    shacl_summary = run_shacl(lbd_ttl, ROOT / "rules" / "accessibility_rules.shacl.ttl", output / "shacl_report.ttl")
    issues = issues_from_shacl_report(output / "shacl_report.ttl", lbd_ttl, elements, edges)
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
    print("Building tiled point-navigation package at 0.01 m resolution")
    navigation_index = build_navigation_package(output / "app_data.json", output)
    app_data_path = output / "app_data.json"
    app_data = json.loads(app_data_path.read_text(encoding="utf-8"))
    app_data["summary"]["pointNavigation"] = {
        "formatVersion": navigation_index["formatVersion"],
        "resolutionM": navigation_index["resolutionM"],
        "tileSizeM": navigation_index["tileSizeM"],
        "wheelchairClearanceM": navigation_index["wheelchairClearanceM"],
        "accessibleRouteWidthM": navigation_index["accessibleRouteWidthM"],
        "floorCount": len(navigation_index["floors"]),
    }
    app_data["sources"]["pointNavigation"] = "precomputed tiled occupancy and component graph from IFC floor geometry"
    app_data_path.write_text(json.dumps(app_data, indent=2), encoding="utf-8")
    print("Building strict routes for the 2.5D floor-check simulation")
    floor_check_summary = add_floor_check_routes(app_data_path, output)
    print(
        "Floor-check routes: "
        f"{floor_check_summary['directionalRouteCount']} directional, "
        f"{floor_check_summary['unavailableEdgeCount']} unavailable edges"
    )
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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Preprocess failed: {exc}", file=sys.stderr)
        raise
