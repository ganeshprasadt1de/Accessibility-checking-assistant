from __future__ import annotations

import json
from pathlib import Path

from .config import RULE_LIMITS
from .model import Element, Issue, RouteEdge
from .routes import routes_from_start


def write_json_package(
    output_dir: Path,
    elements: list[Element],
    issues: list[Issue],
    edges: list[RouteEdge],
    missing_geometry: list[str],
    shacl_summary: dict,
    ifctolbd_note: str,
    inspection_checks: list[dict] | None = None,
    plan_edges: list[RouteEdge] | None = None,
    plan_elements: list[Element] | None = None,
) -> None:
    plan_edges = plan_edges or []
    plan_elements = plan_elements or elements
    doors = [e.guid for e in elements if e.ifc_type == "IfcDoor"]
    route_index = {door: routes_from_start(edges, door) for door in doors}
    accessible_route_index = {door: routes_from_start(edges, door, pass_only=True) for door in doors}
    floors = _floor_summaries(elements, plan_elements, issues, edges, plan_edges)
    ifctolbd_failed = "ifctolbd failed" in ifctolbd_note.lower()
    data = {
        "summary": {
            "elementCount": len(elements),
            "doorCount": len(doors),
            "routeEdgeCount": len(edges),
            "planRouteEdgeCount": len(plan_edges),
            "issueCount": len(issues),
            "missingGeometryCount": len(missing_geometry),
            "ifctolbd": ifctolbd_note,
            "shacl": shacl_summary,
            "ruleSource": "SHACL rules over IFC-derived geometry measurements" if ifctolbd_failed else "SHACL rules over IFCtoLBD RDF plus IFC-derived geometry measurements",
        },
        "rules": RULE_LIMITS.__dict__,
        "elements": [_element_dict(e) for e in elements],
        "planElements": [_element_dict(e) for e in plan_elements],
        "issues": [issue.__dict__ for issue in issues],
        "inspectionChecks": inspection_checks or [],
        "issueRegions": _inspection_issue_regions(inspection_checks or [], issues),
        "routeEdges": [_edge_dict(e) for e in edges],
        "planRouteEdges": [_edge_dict(e) for e in plan_edges],
        "routesByDoor": route_index,
        "accessibleRoutesByDoor": accessible_route_index,
        "floors": floors,
        "missingGeometry": missing_geometry,
        "sources": {
            "measurements": "IfcOpenShell geometry or explicit IFC properties",
            "rules": "SHACL validation report",
            "routes": "precomputed door graph from IFC space boundaries",
            "planRoutes": "sparse 2D route network from Shapely walkable areas",
            "ifctolbd": "failed; raw LBD RDF is not included" if ifctolbd_failed else "raw graph created by IFCtoLBD",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "app_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _element_dict(e: Element) -> dict:
    public_extra = {key: value for key, value in e.extra.items() if key != "_inspectionFootprint"}
    return {
        "guid": e.guid,
        "ifcType": e.ifc_type,
        "name": e.name,
        "label": e.label,
        "source": e.source,
        "width": e.width,
        "depth": e.depth,
        "height": e.height,
        "center": e.center,
        "bboxMin": e.bbox_min,
        "bboxMax": e.bbox_max,
        "storey": e.storey,
        "extra": public_extra,
    }


def _edge_dict(e: RouteEdge) -> dict:
    return {
        "edgeId": e.edge_id,
        "startGuid": e.start_guid,
        "endGuid": e.end_guid,
        "distanceM": e.distance_m,
        "status": e.status,
        "reasons": e.reasons,
        "path": e.path,
        "source": e.source,
        "viaSpaceGuid": e.via_space_guid,
        "viaSpaceLabel": e.via_space_label,
        "measurements": e.measurements,
    }


def _inspection_issue_regions(checks: list[dict], issues: list[Issue]) -> list[dict]:
    issue_ids = {(issue.element_guid, issue.rule_id): issue.issue_id for issue in issues}
    result = []
    for check in checks:
        region = check.get("region")
        if check.get("status") != "fail" or not isinstance(region, dict):
            continue
        issue_id = issue_ids.get((check.get("elementGuid"), check.get("ruleId")), check.get("checkId"))
        result.append({**region, "issue_id": issue_id})
    return result


def _floor_summaries(
    elements: list[Element],
    plan_elements: list[Element],
    issues: list[Issue],
    edges: list[RouteEdge],
    plan_edges: list[RouteEdge],
) -> list[dict]:
    element_by_guid = {element.guid: element for element in elements}
    floor_refs = _floor_refs(elements)
    issue_count_by_guid: dict[str, int] = {}
    for issue in issues:
        issue_count_by_guid[issue.element_guid] = issue_count_by_guid.get(issue.element_guid, 0) + 1

    floors: dict[str, dict] = {}
    for element in elements:
        floor_name = _floor_name_for_element(element, floor_refs)
        if not floor_name:
            continue
        floor = floors.setdefault(
            floor_name,
            {
                "name": floor_name,
                "elevation": element.center[2] if element.center else None,
                "elementGuids": [],
                "planElementGuids": [],
                "doorGuids": [],
                "spaceGuids": [],
                "stairGuids": [],
                "rampGuids": [],
                "planRampGuids": [],
                "issueCount": 0,
                "routeEdgeIds": [],
                "planRouteEdgeIds": [],
                "routeStatusCounts": {},
                "failureReasonCounts": {},
            },
        )
        floor["elementGuids"].append(element.guid)
        floor["issueCount"] += issue_count_by_guid.get(element.guid, 0)
        if element.ifc_type == "IfcDoor":
            floor["doorGuids"].append(element.guid)
        elif element.ifc_type == "IfcSpace":
            floor["spaceGuids"].append(element.guid)
        elif element.ifc_type in {"IfcStair", "IfcStairFlight"}:
            floor["stairGuids"].append(element.guid)
        elif element.ifc_type in {"IfcRamp", "IfcRampFlight"}:
            floor["rampGuids"].append(element.guid)
        if floor["elevation"] is None and element.center:
            floor["elevation"] = element.center[2]

    for element in plan_elements:
        floor_name = _floor_name_for_plan_element(element, floor_refs)
        if not floor_name or floor_name not in floors:
            continue
        floors[floor_name]["planElementGuids"].append(element.guid)
        if element.ifc_type in {"IfcRamp", "IfcRampFlight"}:
            floors[floor_name]["planRampGuids"].append(element.guid)

    for edge in edges:
        start = element_by_guid.get(edge.start_guid)
        floor_name = start.storey if start else None
        if not floor_name and start:
            floor_name = _floor_from_center(start.center)
        if not floor_name or floor_name not in floors:
            continue
        floor = floors[floor_name]
        floor["routeEdgeIds"].append(edge.edge_id)
        floor["routeStatusCounts"][edge.status] = floor["routeStatusCounts"].get(edge.status, 0) + 1
        for reason in edge.reasons:
            floor["failureReasonCounts"][reason] = floor["failureReasonCounts"].get(reason, 0) + 1

    plan_element_by_guid = {element.guid: element for element in plan_elements}
    for edge in plan_edges:
        floor_name = _floor_name_for_plan_edge(edge, plan_element_by_guid, floor_refs)
        if not floor_name or floor_name not in floors:
            continue
        floors[floor_name]["planRouteEdgeIds"].append(edge.edge_id)

    visible_floors = [
        floor
        for floor in floors.values()
        if floor["doorGuids"] or floor["routeEdgeIds"] or floor["planRouteEdgeIds"] or floor["issueCount"]
    ]
    return sorted(
        visible_floors,
        key=lambda floor: (float(floor["elevation"]) if floor["elevation"] is not None else 999999, floor["name"]),
    )


def _floor_from_center(center: tuple[float, float, float] | None) -> str | None:
    if not center:
        return None
    return f"z={center[2]:.2f}"


def _floor_refs(elements: list[Element]) -> list[tuple[str, float]]:
    grouped: dict[str, list[float]] = {}
    source = [element for element in elements if element.ifc_type == "IfcDoor" and element.storey and element.center]
    if not source:
        source = [element for element in elements if element.storey and element.center]
    for element in source:
        grouped.setdefault(element.storey, []).append(float(element.center[2]))
    refs = []
    for name, elevations in grouped.items():
        refs.append((name, sum(elevations) / len(elevations)))
    return refs


def _floor_name_for_element(element: Element, floor_refs: list[tuple[str, float]]) -> str | None:
    if element.storey:
        return element.storey
    if not element.center:
        return None
    z = float(element.center[2])
    if floor_refs:
        name, ref_z = min(floor_refs, key=lambda item: abs(item[1] - z))
        if abs(ref_z - z) <= 1.8:
            return name
    return _floor_from_center(element.center)


def _floor_name_for_plan_edge(
    edge: RouteEdge,
    element_by_guid: dict[str, Element],
    floor_refs: list[tuple[str, float]],
) -> str | None:
    for guid in (edge.start_guid, edge.end_guid, edge.via_space_guid):
        element = element_by_guid.get(guid) if guid else None
        if element:
            floor_name = _floor_name_for_plan_element(element, floor_refs)
            if floor_name:
                return floor_name
    points = [point for point in edge.path if len(point) >= 3]
    if not points:
        return None
    z = sum(float(point[2]) for point in points) / len(points)
    if floor_refs:
        floor_name, floor_z = min(floor_refs, key=lambda item: abs(item[1] - z))
        if abs(floor_z - z) <= 1.8:
            return floor_name
    return _floor_from_center((0.0, 0.0, z))


def _floor_name_for_plan_element(element: Element, floor_refs: list[tuple[str, float]]) -> str | None:
    if element.ifc_type in {"IfcRamp", "IfcRampFlight"} and element.extra.get("rampDisplayStorey"):
        return str(element.extra["rampDisplayStorey"])
    return _floor_name_for_element(element, floor_refs)
