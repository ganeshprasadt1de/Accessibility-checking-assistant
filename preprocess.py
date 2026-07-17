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
    bind_graph,
    element_uri,
    load_raw_graph,
    passing_area_gap_uri,
    run_ifctolbd,
    run_ifctolbd_exe,
)
from backend.package_writer import write_json_package
from backend.navigation import build_navigation_package
from backend.plan_routes import build_plan_route_edges, prepare_plan_geometry
from backend.routes import add_plan_routes_to_graph, add_routes_to_graph, build_route_edges, save_route_binary
from backend.simulation_routes import add_floor_check_routes, apply_strict_navigation_to_edges
from backend.shacl_runner import issues_from_shacl_report, run_shacl
from backend.audit import write_audit_report
from rdflib import Graph, Literal, Namespace, RDF
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
    try:
        if args.ifctolbd_exe.exists():
            ifctolbd_note = run_ifctolbd_exe(ifc_path, args.ifctolbd_exe.resolve(), raw_ttl)
        else:
            ifctolbd_note = run_ifctolbd(ifc_path, args.ifctolbd_zip.resolve(), raw_ttl, work)
        graph = load_raw_graph(raw_ttl)
    except Exception as exc:
        ifctolbd_note = f"IFCtoLBD failed; continued with IFC geometry only: {exc}"
        graph = Graph()
        bind_graph(graph)
    print(ifctolbd_note)
    skipped_route_pairs = []
    edges = build_route_edges(ifc_path, elements, skipped_route_pairs)
    try:
        plan_geometry = prepare_plan_geometry(ifc_path, elements)
    except Exception as exc:
        print(f"2D geometry preparation failed: {exc}", file=sys.stderr)
        plan_geometry = None
    plan_routes_available = True
    try:
        plan_edges = build_plan_route_edges(ifc_path, elements, edges, plan_geometry)
    except Exception as exc:
        print(f"2D route generation failed: {exc}", file=sys.stderr)
        plan_edges = []
        plan_routes_available = False
    rdf_plan_edges = [edge for edge in plan_edges if not edge.measurements.get("planMarkerOnly")]
    add_geometry_to_graph(graph, elements)
    provisional_shacl = {
        "available": False,
        "conforms": None,
        "source": "pending strict 0.01 m route generation",
        "resultCount": 0,
        "message": "SHACL runs after strict tiled route generation.",
    }
    write_json_package(output, elements, [], edges, missing_geometry, provisional_shacl, ifctolbd_note)
    print("Building tiled navigation package at 0.01 m resolution")
    navigation_index = build_navigation_package(output / "app_data.json", output)
    print("Replacing provisional route candidates with audited 0.01 m routes")
    strict_summary, strict_records = apply_strict_navigation_to_edges(
        output / "app_data.json",
        output,
        elements,
        edges,
    )
    print(
        "Strict route edges: "
        f"{strict_summary['passCount']} connected, "
        f"{strict_summary['blockedCount']} blocked, "
        f"{strict_summary['unavailableCount']} without a drawable candidate"
    )

    add_routes_to_graph(graph, edges)
    if plan_routes_available:
        add_plan_routes_to_graph(graph, plan_edges, elements)
    lbd_ttl = output / "lbd_graph.ttl"
    graph.serialize(destination=lbd_ttl, format="turtle")
    shacl_summary = run_shacl(lbd_ttl, ROOT / "rules" / "accessibility_rules.shacl.ttl", output / "shacl_report.ttl")
    issues = issues_from_shacl_report(
        output / "shacl_report.ttl",
        lbd_ttl,
        elements,
        edges,
        rdf_plan_edges,
    )
    unexplained_navigation_failures = [
        edge.edge_id
        for edge in edges
        if edge.measurements.get("routeNavigationBlocked") is True and edge.status != "fail"
    ]
    if unexplained_navigation_failures:
        raise RuntimeError(
            "SHACL did not reject strict navigation failures: "
            + ", ".join(unexplained_navigation_failures)
        )
    for issue in issues:
        issue_uri = ACC[f"issue/{issue.issue_id}"]
        graph.add((issue_uri, RDF.type, ACC.AccessibilityIssue))
        graph.add((issue_uri, ACC.issueElement, element_uri(issue.element_guid)))
        graph.add((issue_uri, ACC.ruleId, Literal(issue.rule_id)))
        graph.add((issue_uri, ACC.severity, Literal(issue.severity)))
        graph.add((issue_uri, ACC.shortExplanation, Literal(issue.short_text)))
        graph.add((issue_uri, ACC.issueSource, Literal(issue.source)))
        if issue.evidence_id:
            graph.add((issue_uri, ACC.issueEvidence, passing_area_gap_uri(issue.evidence_id)))
        if issue.measured is not None:
            graph.add((issue_uri, ACC.measuredValue, Literal(round(issue.measured, 4), datatype=XSD.decimal)))
        if issue.required is not None:
            graph.add((issue_uri, ACC.requiredValue, Literal(round(issue.required, 4), datatype=XSD.decimal)))
    for edge in [*edges, *rdf_plan_edges]:
        route_uri = ACC[f"route/{edge.edge_id}"]
        graph.set((route_uri, ACC.routeStatus, Literal(edge.status)))
        graph.remove((route_uri, ACC.routeFailureReason, None))
        for reason in edge.reasons:
            graph.add((route_uri, ACC.routeFailureReason, Literal(reason)))
    graph.serialize(destination=lbd_ttl, format="turtle")

    write_json_package(output, elements, issues, edges, missing_geometry, shacl_summary, ifctolbd_note, plan_edges)
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
    app_data["summary"]["routeNavigation"] = strict_summary
    app_data["sources"]["pointNavigation"] = "precomputed tiled occupancy and component graph from IFC floor geometry"
    app_data["sources"]["routes"] = "audited four-direction A* routes from the precomputed 0.01 m navigation tiles"
    app_data_path.write_text(json.dumps(app_data, indent=2), encoding="utf-8")
    print("Building strict routes for the 2.5D floor-check simulation")
    floor_check_precomputed = None if rdf_plan_edges else strict_records
    floor_check_summary = add_floor_check_routes(app_data_path, output, floor_check_precomputed)
    print(
        "Floor-check routes: "
        f"{floor_check_summary['directionalRouteCount']} directional, "
        f"{floor_check_summary['unavailableEdgeCount']} unavailable edges"
    )
    write_audit_report(
        ifc_path,
        output,
        {
            "routeEdges": [
                {
                    "startGuid": edge.start_guid,
                    "endGuid": edge.end_guid,
                    "status": edge.status,
                    "reasons": edge.reasons,
                }
                for edge in edges
            ],
            "skippedRoutePairs": skipped_route_pairs,
        },
    )
    export_box_glb(elements, edges, output / "route_model.glb")
    if args.save_bin:
        save_route_binary(edges, output / "route_graph.bin")
    print(f"Wrote package: {output}")
    print(
        f"Routes: {len(edges)}, plan routes: {len(plan_edges)}, issues: {len(issues)}, "
        f"missing geometry: {len(missing_geometry)}, skipped route pairs: {len(skipped_route_pairs)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Preprocess failed: {exc}", file=sys.stderr)
        raise
