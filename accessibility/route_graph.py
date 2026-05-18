from __future__ import annotations

from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell
from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef
from rdflib.namespace import XSD

from accessibility.model import GeometryFinding


ACC = Namespace("http://example.org/accessibility#")
PROPS = Namespace("http://lbd.arch.rwth-aachen.de/props#")
ROUTE = Namespace("http://example.org/accessibility-route#")

MIN_DOOR_WIDTH_M = 0.90
MAX_LEVEL_CHANGE_M = 0.02


def build_accessible_route_graph(uploaded_file, graph: Graph) -> tuple[list[dict[str, str]], list[GeometryFinding]]:
    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)

    subjects = _subjects_by_global_id(graph)
    spaces = list(model.by_type("IfcSpace"))
    doors = list(model.by_type("IfcDoor"))
    relations = list(model.by_type("IfcRelSpaceBoundary"))

    rows: list[dict[str, str]] = []
    findings: list[GeometryFinding] = []

    if not spaces:
        findings.append(_finding("Model data", "IFC model", "Accessible route graph", "not checked", "No IfcSpace entities were found.", "Export rooms or spaces as IfcSpace."))
        return rows, findings
    if not doors:
        findings.append(_finding("Model data", "IFC model", "Accessible route graph", "not checked", "No IfcDoor entities were found.", "Export doors as IfcDoor."))
        return rows, findings
    if not relations:
        findings.append(_finding("Model data", "IFC model", "Accessible route graph", "not checked", "No IfcRelSpaceBoundary entities were found.", "Export space boundaries."))
        return rows, findings

    door_to_spaces = _door_to_spaces(relations)
    adjacency: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    route_edge_count = 0
    invalid_edge_count = 0

    for door in doors:
        linked_spaces = door_to_spaces.get(door.id(), [])
        if len(linked_spaces) < 2:
            continue

        for space_a, space_b in combinations(linked_spaces, 2):
            edge = _route_edge(graph, subjects, door, space_a, space_b)
            if edge is None:
                continue
            route_edge_count += 1
            if edge["Pass"] != "true":
                invalid_edge_count += 1
            adjacency[space_a.GlobalId].append((space_b.GlobalId, edge["Route edge"], edge["Pass"] == "true"))
            adjacency[space_b.GlobalId].append((space_a.GlobalId, edge["Route edge"], edge["Pass"] == "true"))
            rows.append(edge)

    if not rows:
        findings.append(_finding("Model data", "IFC model", "Accessible route graph", "not checked", "Space boundaries exist, but no door was linked to two spaces.", "Export second-level space boundaries or door-space relations."))
        return rows, findings

    entrances = _entrance_spaces(spaces, adjacency)
    targets = _target_spaces(spaces)
    if not entrances:
        findings.append(_finding("Model data", "IFC model", "Entrance-to-room path finding", "not checked", "No entrance, lobby, foyer, or reception space label was found.", "Name the entrance space clearly or add an entrance property."))
    else:
        reachable = _reachable_spaces(adjacency, entrances)
        unreachable_targets = [space for space in targets if space.GlobalId not in reachable]
        findings.append(
            _finding(
                "Mobility",
                "IFC model",
                "Entrance-to-room path finding",
                "checked",
                f"{len(reachable)} spaces are reachable from the detected entrance spaces through valid accessible route edges.",
                "Review unreachable target rooms and invalid route edges.",
            )
        )
        for target in unreachable_targets[:25]:
            findings.append(
                _finding(
                    "Mobility",
                    _label(target),
                    "Shortest accessible path",
                    "failed",
                    "No valid accessible path was found from a detected entrance space to this target space.",
                    "Check door width, level changes, and missing door-space boundaries along the route.",
                )
            )

    findings.append(
        _finding(
            "Mobility",
            "IFC model",
            "Door-to-door route continuity",
            "checked",
            f"Built {route_edge_count} room-door-room route edges. {invalid_edge_count} route edges failed the current accessible route checks.",
            "Fix failed route edges, then rerun the check.",
        )
    )

    return rows, findings


def _route_edge(graph: Graph, subjects: dict[str, URIRef], door, space_a, space_b) -> dict[str, str] | None:
    door_subject = subjects.get(door.GlobalId)
    space_a_subject = subjects.get(space_a.GlobalId)
    space_b_subject = subjects.get(space_b.GlobalId)
    if door_subject is None or space_a_subject is None or space_b_subject is None:
        return None

    door_width = _door_width(graph, door_subject, door)
    door_center = _center_xyz(graph, door_subject)
    level_change = abs(_center_z(graph, space_a_subject) - _center_z(graph, space_b_subject))
    step_free = level_change <= MAX_LEVEL_CHANGE_M
    door_ok = door_width is not None and door_width >= MIN_DOOR_WIDTH_M
    route_pass = door_ok and step_free

    edge_subject = ROUTE[f"edge_{door.GlobalId}_{space_a.GlobalId}_{space_b.GlobalId}"]
    label = f"{_label(space_a)} -> {_label(space_b)} through {_label(door)}"
    graph.add((edge_subject, RDF.type, ACC.RouteEdge))
    graph.add((edge_subject, RDFS.label, Literal(label)))
    graph.add((edge_subject, ACC.fromSpace, space_a_subject))
    graph.add((edge_subject, ACC.toSpace, space_b_subject))
    graph.add((edge_subject, ACC.routeDoor, door_subject))
    graph.add((edge_subject, ACC.routeDoorWidthM, Literal(door_width if door_width is not None else -1, datatype=XSD.double)))
    if door_center is not None:
        graph.add((edge_subject, ACC.doorCenterX, Literal(door_center[0], datatype=XSD.double)))
        graph.add((edge_subject, ACC.doorCenterY, Literal(door_center[1], datatype=XSD.double)))
        graph.add((edge_subject, ACC.doorCenterZ, Literal(door_center[2], datatype=XSD.double)))
    graph.add((edge_subject, ACC.levelChangeM, Literal(round(level_change, 4), datatype=XSD.double)))
    graph.add((edge_subject, ACC.stepFree, Literal(step_free, datatype=XSD.boolean)))
    graph.add((edge_subject, ACC.routePass, Literal(route_pass, datatype=XSD.boolean)))

    return {
        "Route edge": label,
        "Route edge node": str(edge_subject),
        "From space": _label(space_a),
        "From space node": str(space_a_subject),
        "To space": _label(space_b),
        "To space node": str(space_b_subject),
        "Door": _label(door),
        "Door node": str(door_subject),
        "Door width m": "missing" if door_width is None else f"{door_width:.3f}",
        "Level change m": f"{level_change:.3f}",
        "Step-free": str(step_free).lower(),
        "Pass": str(route_pass).lower(),
    }


def _door_to_spaces(relations) -> dict[int, list]:
    mapping: dict[int, list] = defaultdict(list)
    for relation in relations:
        space = getattr(relation, "RelatingSpace", None)
        element = getattr(relation, "RelatedBuildingElement", None)
        if space is None or element is None or not element.is_a("IfcDoor"):
            continue
        if all(existing.id() != space.id() for existing in mapping[element.id()]):
            mapping[element.id()].append(space)
    return mapping


def _subjects_by_global_id(graph: Graph) -> dict[str, URIRef]:
    return {str(value): subject for subject, value in graph.subject_objects(PROPS.globalIdIfcRoot_attribute_simple)}


def _door_width(graph: Graph, door_subject, door) -> float | None:
    value = graph.value(door_subject, ACC.derivedDoorWidthM)
    if value is not None:
        return float(value)
    value = getattr(door, "OverallWidth", None)
    if value is not None:
        return float(value)
    return None


def _center_z(graph: Graph, subject) -> float:
    value = graph.value(subject, ACC.centerZ)
    return float(value) if value is not None else 0.0


def _center_xyz(graph: Graph, subject) -> tuple[float, float, float] | None:
    x = graph.value(subject, ACC.centerX)
    y = graph.value(subject, ACC.centerY)
    z = graph.value(subject, ACC.centerZ)
    if x is None or y is None or z is None:
        return None
    return float(x), float(y), float(z)


def _entrance_spaces(spaces, adjacency: dict[str, list[tuple[str, str, bool]]]) -> list:
    words = ["entrance", "eingang", "lobby", "foyer", "reception", "aufnahme", "halle"]
    return [space for space in spaces if space.GlobalId in adjacency and any(word in _label(space).lower() for word in words)]


def _target_spaces(spaces) -> list:
    return [space for space in spaces if not any(word in _label(space).lower() for word in ["entrance", "eingang", "lobby", "foyer", "reception", "aufnahme", "halle"])]


def _reachable_spaces(adjacency: dict[str, list[tuple[str, str, bool]]], starts) -> set[str]:
    visited = set()
    queue = deque(space.GlobalId for space in starts)
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for neighbor, _edge_label, is_valid in adjacency.get(current, []):
            if is_valid and neighbor not in visited:
                queue.append(neighbor)
    return visited


def _label(element) -> str:
    return str(getattr(element, "LongName", None) or getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))


def _finding(category: str, element: str, check: str, result: str, reason: str, fix: str) -> GeometryFinding:
    return GeometryFinding(category=category, element=element, check=check, result=result, reason=reason, fix=fix)

