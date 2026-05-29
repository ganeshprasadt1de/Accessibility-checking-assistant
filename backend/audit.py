from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path


def write_audit_report(ifc_path: Path, output_dir: Path, app_summary: dict) -> None:
    import ifcopenshell

    model = ifcopenshell.open(str(ifc_path))
    rels = model.by_type("IfcRelSpaceBoundary")
    door_to_spaces: dict[str, set[str]] = defaultdict(set)
    space_to_doors: dict[str, set[str]] = defaultdict(set)
    boundary_types: Counter[str] = Counter()
    for rel in rels:
        space = getattr(rel, "RelatingSpace", None)
        element = getattr(rel, "RelatedBuildingElement", None)
        if not space or not element:
            continue
        boundary_types[element.is_a()] += 1
        if element.is_a("IfcDoor"):
            door_to_spaces[element.GlobalId].add(space.GlobalId)
            space_to_doors[space.GlobalId].add(element.GlobalId)

    all_doors = {door.GlobalId: _label(door) for door in model.by_type("IfcDoor")}
    all_spaces = {space.GlobalId: _label(space) for space in model.by_type("IfcSpace")}
    route_edges = app_summary.get("routeEdges", [])
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in route_edges:
        adjacency[edge["startGuid"]].add(edge["endGuid"])
        adjacency[edge["endGuid"]].add(edge["startGuid"])

    components = _components(adjacency)
    data = {
        "ifc_counts": {
            name: len(model.by_type(name))
            for name in [
                "IfcSpace",
                "IfcDoor",
                "IfcWall",
                "IfcSlab",
                "IfcRamp",
                "IfcStair",
                "IfcRelSpaceBoundary",
                "IfcRelConnectsPathElements",
            ]
        },
        "door_boundary_relation_count": sum(len(v) for v in door_to_spaces.values()),
        "doors_total": len(all_doors),
        "doors_with_space_boundary": len(door_to_spaces),
        "doors_without_space_boundary": [
            {"guid": guid, "name": name}
            for guid, name in all_doors.items()
            if guid not in door_to_spaces
        ],
        "door_space_count_histogram": dict(Counter(len(v) for v in door_to_spaces.values())),
        "spaces_total": len(all_spaces),
        "spaces_with_route_doors": len(space_to_doors),
        "space_route_door_count_histogram": dict(Counter(len(v) for v in space_to_doors.values())),
        "boundary_element_types": dict(boundary_types.most_common(20)),
        "route_graph": {
            "route_edges": len(route_edges),
            "doors_with_route_edges": len(adjacency),
            "doors_without_route_edges": len(all_doors) - len(adjacency),
            "connected_component_sizes": sorted([len(comp) for comp in components], reverse=True),
            "status_counts": dict(Counter(edge["status"] for edge in route_edges)),
            "failure_reason_counts": dict(Counter(reason for edge in route_edges for reason in edge.get("reasons", []))),
        },
        "shacl_route_rule_note": (
            "The current SHACL route rule checks acc:routeStatus = 'fail'. "
            "The dependency facts are calculated before SHACL by the backend. "
            "This is useful for reporting but it is not a full route-planning proof inside SHACL."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ifc_route_audit.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (output_dir / "ifc_route_audit.md").write_text(_markdown(data), encoding="utf-8")


def _components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen = set()
    components = []
    for node in adjacency:
        if node in seen:
            continue
        queue = deque([node])
        seen.add(node)
        comp = []
        while queue:
            current = queue.popleft()
            comp.append(current)
            for nxt in adjacency[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        components.append(comp)
    return components


def _markdown(data: dict) -> str:
    rg = data["route_graph"]
    lines = [
        "# IFC Route Audit",
        "",
        "## IFC Data",
        "",
        f"- Spaces: {data['ifc_counts']['IfcSpace']}",
        f"- Doors: {data['ifc_counts']['IfcDoor']}",
        f"- Space boundaries: {data['ifc_counts']['IfcRelSpaceBoundary']}",
        f"- Door-space boundary relations: {data['door_boundary_relation_count']}",
        f"- Doors with space boundary: {data['doors_with_space_boundary']}",
        f"- Doors without space boundary: {len(data['doors_without_space_boundary'])}",
        f"- Door boundary space-count histogram: {data['door_space_count_histogram']}",
        "",
        "## Route Graph",
        "",
        f"- Route edges: {rg['route_edges']}",
        f"- Doors with route edges: {rg['doors_with_route_edges']}",
        f"- Doors without route edges: {rg['doors_without_route_edges']}",
        f"- Connected component sizes: {rg['connected_component_sizes']}",
        f"- Route status counts: {rg['status_counts']}",
        f"- Failure reason counts: {rg['failure_reason_counts']}",
        "",
        "## SHACL Route Rule",
        "",
        data["shacl_route_rule_note"],
    ]
    if data["doors_without_space_boundary"]:
        lines.extend(["", "## Doors Without Space Boundary", ""])
        for item in data["doors_without_space_boundary"][:60]:
            lines.append(f"- {item['name']} ({item['guid']})")
    return "\n".join(lines) + "\n"


def _label(element) -> str:
    return str(getattr(element, "LongName", None) or getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))
