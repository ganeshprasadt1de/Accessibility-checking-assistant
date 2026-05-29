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
) -> None:
    doors = [e.guid for e in elements if e.ifc_type == "IfcDoor"]
    route_index = {door: routes_from_start(edges, door) for door in doors}
    accessible_route_index = {door: routes_from_start(edges, door, pass_only=True) for door in doors}
    floors = _floor_summaries(elements, issues, edges)
    data = {
        "summary": {
            "elementCount": len(elements),
            "doorCount": len(doors),
            "routeEdgeCount": len(edges),
            "issueCount": len(issues),
            "missingGeometryCount": len(missing_geometry),
            "ifctolbd": ifctolbd_note,
            "shacl": shacl_summary,
            "ruleSource": "Indoor wheelchair rules: door width, route width, turning space, stair blockers, and ramp width/slope",
        },
        "rules": RULE_LIMITS.__dict__,
        "elements": [_element_dict(e) for e in elements],
        "issues": [issue.__dict__ for issue in issues],
        "routeEdges": [_edge_dict(e) for e in edges],
        "routesByDoor": route_index,
        "accessibleRoutesByDoor": accessible_route_index,
        "floors": floors,
        "missingGeometry": missing_geometry,
        "sources": {
            "measurements": "IfcOpenShell geometry or explicit IFC properties",
            "rules": "Indoor wheelchair route rules",
            "routes": "precomputed door graph from IFC geometry",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "app_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _element_dict(e: Element) -> dict:
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
        "extra": e.extra,
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
    }


def _floor_summaries(elements: list[Element], issues: list[Issue], edges: list[RouteEdge]) -> list[dict]:
    element_by_guid = {element.guid: element for element in elements}
    issue_count_by_guid: dict[str, int] = {}
    for issue in issues:
        issue_count_by_guid[issue.element_guid] = issue_count_by_guid.get(issue.element_guid, 0) + 1

    floors: dict[str, dict] = {}
    for element in elements:
        floor_name = element.storey or _floor_from_center(element.center)
        if not floor_name:
            continue
        floor = floors.setdefault(
            floor_name,
            {
                "name": floor_name,
                "elevation": element.center[2] if element.center else None,
                "elementGuids": [],
                "doorGuids": [],
                "spaceGuids": [],
                "stairGuids": [],
                "rampGuids": [],
                "issueCount": 0,
                "routeEdgeIds": [],
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
        elif element.ifc_type == "IfcStair":
            floor["stairGuids"].append(element.guid)
        elif element.ifc_type == "IfcRamp":
            floor["rampGuids"].append(element.guid)
        if floor["elevation"] is None and element.center:
            floor["elevation"] = element.center[2]

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

    return sorted(
        floors.values(),
        key=lambda floor: (float(floor["elevation"]) if floor["elevation"] is not None else 999999, floor["name"]),
    )


def _floor_from_center(center: tuple[float, float, float] | None) -> str | None:
    if not center:
        return None
    return f"z={center[2]:.2f}"
