from __future__ import annotations

import heapq
import math
import pickle
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF, RDFS
from rdflib.namespace import XSD

from .config import NS, RULE_LIMITS
from .geometry import distance, intersects_box, obstacle_elements
from .ifc_tools import element_uri
from .model import Element, RouteEdge
from .navigation import GEOMETRY_TOLERANCE_M, WHEELCHAIR_RADIUS_M

ACC = Namespace(NS["acc"])


def build_route_edges(ifc_path: Path, elements: list[Element]) -> list[RouteEdge]:
    space_edges = _space_boundary_route_edges(ifc_path, elements)
    if space_edges:
        return space_edges
    raise RuntimeError("No usable IfcRelSpaceBoundary door-to-space route graph was found in the IFC model.")


def _space_boundary_route_edges(ifc_path: Path, elements: list[Element]) -> list[RouteEdge]:
    import ifcopenshell

    model = ifcopenshell.open(str(ifc_path))
    by_guid = {element.guid: element for element in elements}
    spaces_by_door = _door_to_spaces(model)
    doors_by_space: dict[str, list[Element]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        door = by_guid.get(door_guid)
        if not door or not door.center:
            continue
        for space_guid in space_guids:
            space = by_guid.get(space_guid)
            if space and space.center:
                doors_by_space[space_guid].append(door)

    obstacles = obstacle_elements(elements)
    edges: list[RouteEdge] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for space_guid, doors in sorted(doors_by_space.items()):
        space = by_guid.get(space_guid)
        if not space or not space.center or len(doors) < 2:
            continue
        unique_doors = sorted({door.guid: door for door in doors}.values(), key=lambda item: item.guid)
        for door_a, door_b in combinations(unique_doors, 2):
            pair_key = tuple(sorted((door_a.guid, door_b.guid)) + [space_guid])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            path = _provisional_route_candidate(door_a, door_b, space)
            measurements = _route_measurements(door_a, door_b, path, obstacles, space)
            edge_id = f"E{len(edges) + 1:05d}"
            dist = sum(distance(path[i], path[i + 1]) for i in range(len(path) - 1))
            edges.append(
                RouteEdge(
                    edge_id=edge_id,
                    start_guid=door_a.guid,
                    end_guid=door_b.guid,
                    distance_m=dist,
                    status="unchecked",
                    reasons=[],
                    path=path,
                    source="IFC space-boundary candidate awaiting strict 0.01 m tiled navigation",
                    via_space_guid=space.guid,
                    via_space_label=space.label,
                    measurements=measurements,
                )
            )
    edges.extend(_stair_approach_route_edges(elements, len(edges)))
    return edges


def _stair_approach_route_edges(elements: list[Element], offset: int) -> list[RouteEdge]:
    doors = [item for item in elements if item.ifc_type == "IfcDoor" and item.center]
    stairs = [item for item in elements if item.ifc_type in {"IfcStair", "IfcStairFlight"} and item.center]
    edges: list[RouteEdge] = []
    for stair in sorted(stairs, key=lambda item: item.guid):
        same_level_doors = [door for door in doors if _same_level(door, stair)]
        if not same_level_doors:
            continue
        start = min(same_level_doors, key=lambda door: distance(door.center, stair.center))
        route_z = (start.bbox_min[2] + 0.05) if start.bbox_min else min(start.center[2], stair.center[2])
        route_start = (start.center[0], start.center[1], route_z)
        route_end = (stair.center[0], stair.center[1], route_z)
        path = _densify_orthogonal(_orthogonal_between(route_start, route_end, route_z))
        measurements: dict[str, float | str | bool | None] = {
            "routeApproachType": "stair",
            "routeHitsStair": _route_intersects_any(path, [stair]),
            "routeHasTurn": False,
        }
        width = _num(start.extra.get("derivedDoorWidthM"))
        if width is not None:
            measurements["routeDoorWidthMinM"] = width
        edge_id = f"E{offset + len(edges) + 1:05d}"
        edges.append(
            RouteEdge(
                edge_id=edge_id,
                start_guid=start.guid,
                end_guid=stair.guid,
                distance_m=sum(distance(path[i], path[i + 1]) for i in range(len(path) - 1)),
                status="unchecked",
                reasons=[],
                path=path,
                source="IFC stair approach geometry",
                via_space_guid=stair.guid,
                via_space_label=stair.label,
                measurements=measurements,
            )
        )
    return edges


def _door_to_spaces(model) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = defaultdict(list)
    for relation in model.by_type("IfcRelSpaceBoundary"):
        space = getattr(relation, "RelatingSpace", None)
        element = getattr(relation, "RelatedBuildingElement", None)
        if space is None or element is None or not element.is_a("IfcDoor"):
            continue
        door_guid = getattr(element, "GlobalId", None)
        space_guid = getattr(space, "GlobalId", None)
        if not door_guid or not space_guid:
            continue
        if space_guid not in mapping[door_guid]:
            mapping[door_guid].append(space_guid)
    return mapping


def _route_measurements(
    door_a: Element,
    door_b: Element,
    path: list[tuple[float, float, float]],
    obstacles: list[Element],
    space: Element | None,
) -> dict[str, float | str | bool | None]:
    measurements: dict[str, float | str | bool | None] = {}
    measurements["routeIsOrthogonal"] = True
    width_a = door_a.extra.get("derivedDoorWidthM")
    width_b = door_b.extra.get("derivedDoorWidthM")
    widths = [float(width) for width in (width_a, width_b) if width is not None]
    if widths:
        measurements["routeDoorWidthMinM"] = min(widths)
    if space:
        clear = _num(space.extra.get("derivedClearSpaceWidthM"))
        turn = _num(space.extra.get("turningSpaceM"))
        if clear is not None:
            measurements["routeClearWidthM"] = clear
        if turn is not None:
            measurements["routeTurningSpaceM"] = turn
        measurements["routeHasTurn"] = _path_has_turn(path)
    measurements["routeHitsStair"] = _route_hits_stair(path, obstacles, space)
    measurements.update(_ramp_measurements(obstacles, space, path))
    return measurements


def apply_navigation_result_to_edge(
    edge: RouteEdge,
    elements: list[Element],
    result: dict,
    display_path: list[list[float]],
) -> None:
    """Replace provisional route facts with one audited 0.01 m result."""
    by_guid = {element.guid: element for element in elements}
    start = by_guid.get(edge.start_guid)
    end = by_guid.get(edge.end_guid)
    if not start or not end:
        raise RuntimeError(f"Route {edge.edge_id} lost an IFC endpoint before strict navigation.")
    path = [tuple(float(value) for value in point[:3]) for point in display_path]
    obstacles = obstacle_elements(elements)
    via = by_guid.get(edge.via_space_guid) if edge.via_space_guid else None
    route_context = via if via and via.ifc_type == "IfcSpace" else start
    route_z = _route_elevation(start, end, route_context)
    path = [(point[0], point[1], route_z) for point in path]
    edge.path = path
    edge.distance_m = sum(distance(a, b) for a, b in zip(path, path[1:]))
    edge.status = "unchecked"
    edge.reasons = []
    edge.source = "audited four-direction A* route on precomputed 0.01 m navigation tiles"
    measurements = _route_measurements(start, end, path, obstacles, via)
    audit = result.get("audit") or {}
    endpoint_exact = bool(
        len(path) >= 2
        and start.center
        and end.center
        and math.hypot(path[0][0] - start.center[0], path[0][1] - start.center[1]) <= 1e-8
        and math.hypot(path[-1][0] - end.center[0], path[-1][1] - end.center[1]) <= 1e-8
    )
    measurements.update(
        {
            "routeGridStepM": float(result.get("resolutionM", 0.01)),
            "routeNavigationClearanceM": float(result.get("clearanceM", 0.90)),
            "routeRequiredWidthM": float(result.get("routeWidthM", RULE_LIMITS.corridor_width_m)),
            "routeNavigationStatus": str(result.get("status", "blocked")),
            "routeNavigationBlocked": result.get("status") != "pass",
            "routeNavigationReason": str(result.get("reason") or "navigation_unavailable"),
            "routeNavigationSafeDistanceM": float(result.get("distanceM") or 0.0),
            "routeNavigationCollisionFree": bool(audit.get("collisionFree")),
            "routeNavigationEndpointsExact": bool(audit.get("endpointsExact")),
            "routeDisplayEndpointsExact": endpoint_exact,
            "routeIsOrthogonal": all(
                abs(a[0] - b[0]) <= 1e-8 or abs(a[1] - b[1]) <= 1e-8
                for a, b in zip(path, path[1:])
            ),
        }
    )
    edge.measurements = measurements


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _path_has_turn(path: list[tuple[float, float, float]]) -> bool:
    if len(path) < 3:
        return False
    for index in range(1, len(path) - 1):
        prev_point = path[index - 1]
        point = path[index]
        next_point = path[index + 1]
        a = (point[0] - prev_point[0], point[1] - prev_point[1])
        b = (next_point[0] - point[0], next_point[1] - point[1])
        if abs(a[0] * b[1] - a[1] * b[0]) > 0.05:
            return True
    return False


def _route_hits_stair(path: list[tuple[float, float, float]], obstacles: list[Element], space: Element | None) -> bool:
    stairs = [item for item in obstacles if item.ifc_type in {"IfcStair", "IfcStairFlight"}]
    if not stairs:
        return False
    return _route_intersects_any(path, stairs)


def _route_intersects_any(path: list[tuple[float, float, float]], obstacles: list[Element]) -> bool:
    half = WHEELCHAIR_RADIUS_M - GEOMETRY_TOLERANCE_M
    for point in _sample_path(path, step=0.35):
        x, y, z = point
        floor_z = z - 0.05
        route_min = (x - half, y - half, floor_z)
        route_max = (x + half, y + half, floor_z + RULE_LIMITS.clearance_height_m)
        if any(intersects_box(route_min, route_max, obstacle.bbox_min, obstacle.bbox_max) for obstacle in obstacles):
            return True
    return False


def _sample_path(path: list[tuple[float, float, float]], step: float) -> list[tuple[float, float, float]]:
    if len(path) < 2:
        return path
    points: list[tuple[float, float, float]] = []
    for start, end in zip(path, path[1:]):
        segment_length = distance(start, end)
        count = max(1, int(segment_length / step))
        for index in range(count + 1):
            t = index / count
            points.append(
                (
                    start[0] + (end[0] - start[0]) * t,
                    start[1] + (end[1] - start[1]) * t,
                    start[2] + (end[2] - start[2]) * t,
                )
            )
    return points


def _ramp_measurements(obstacles: list[Element], space: Element | None, path: list[tuple[float, float, float]]) -> dict[str, float | str | bool | None]:
    measurements: dict[str, float | str | bool | None] = {}
    for ramp in obstacles:
        if ramp.ifc_type not in {"IfcRamp", "IfcRampFlight"}:
            continue
        if space and space.bbox_min and space.bbox_max:
            if not intersects_box(space.bbox_min, space.bbox_max, ramp.bbox_min, ramp.bbox_max):
                continue
        elif not _route_intersects_any(path, [ramp]):
            continue
        slope = _num(ramp.extra.get("rampSlopePercent"))
        width = _num(ramp.extra.get("rampUsableWidthM"))
        measurements["routeUsesRamp"] = True
        if slope is not None:
            measurements["routeRampSlopePercent"] = slope
        if width is not None:
            measurements["routeRampUsableWidthM"] = width
    return measurements


def _same_level(a: Element, b: Element) -> bool:
    if a.center and b.center and abs(a.center[2] - b.center[2]) <= 2.2:
        return True
    return bool(a.storey and b.storey and a.storey == b.storey)


def _provisional_route_candidate(
    door_a: Element,
    door_b: Element,
    space: Element,
) -> list[tuple[float, float, float]]:
    """Create endpoint geometry that strict tiled navigation replaces before export."""
    z = _route_elevation(door_a, door_b, space)
    start = (door_a.center[0], door_a.center[1], z)
    end = (door_b.center[0], door_b.center[1], z)
    return _densify_orthogonal(_orthogonal_between(start, end, z))


def _route_elevation(door_a: Element, door_b: Element, space: Element) -> float:
    bottoms = [item.bbox_min[2] for item in (door_a, door_b) if item.bbox_min]
    if bottoms:
        return max(bottoms) + 0.05
    if space.bbox_min:
        return space.bbox_min[2] + 0.05
    return min(door_a.center[2], door_b.center[2])


def _densify_orthogonal(points, step: float = 0.12):
    dense = []
    for start, end in zip(points, points[1:]):
        if abs(start[0] - end[0]) > 1e-8 and abs(start[1] - end[1]) > 1e-8:
            raise ValueError("Route contains a non-orthogonal segment")
        length = distance(start, end)
        count = max(1, math.ceil(length / step))
        for index in range(count):
            t = index / count
            point = tuple(start[i] + (end[i] - start[i]) * t for i in range(3))
            if not dense or distance(dense[-1], point) > 1e-8:
                dense.append(point)
    if points:
        dense.append(points[-1])
    return dense


def _orthogonal_between(a: tuple[float, float, float], b: tuple[float, float, float], z: float) -> list[tuple[float, float, float]]:
    mid = (b[0], a[1], z)
    return _dedupe_points([a, (a[0], a[1], z), mid, (b[0], b[1], z), b])


def _dedupe_points(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    result: list[tuple[float, float, float]] = []
    for point in points:
        if not result or distance(result[-1], point) > 1e-8:
            result.append(point)
    return result


def add_routes_to_graph(g: Graph, edges: list[RouteEdge]) -> None:
    for edge in edges:
        uri = ACC[f"route/{edge.edge_id}"]
        g.add((uri, RDF.type, ACC.RouteEdge))
        g.add((uri, ACC.routeStartDoor, element_uri(edge.start_guid)))
        g.add((uri, ACC.routeEndDoor, element_uri(edge.end_guid)))
        g.add((uri, ACC.routeDistanceM, Literal(round(edge.distance_m, 4), datatype=XSD.decimal)))
        g.add((uri, ACC.routeStatus, Literal(edge.status)))
        g.add((uri, ACC.routeSource, Literal(edge.source)))
        if edge.via_space_guid:
            g.add((uri, ACC.viaSpace, element_uri(edge.via_space_guid)))
        if edge.via_space_label:
            g.add((uri, RDFS.label, Literal(f"{edge.start_guid} to {edge.end_guid} through {edge.via_space_label}")))
        for reason in edge.reasons:
            g.add((uri, ACC.routeFailureReason, Literal(reason)))
        for key, value in edge.measurements.items():
            if isinstance(value, bool):
                g.add((uri, ACC[key], Literal(value, datatype=XSD.boolean)))
            elif isinstance(value, (int, float)):
                g.add((uri, ACC[key], Literal(round(float(value), 4), datatype=XSD.decimal)))
            elif value is not None:
                g.add((uri, ACC[key], Literal(str(value))))


def save_route_binary(edges: list[RouteEdge], path) -> None:
    graph = defaultdict(list)
    by_id = {}
    for edge in edges:
        item = {
            "edge_id": edge.edge_id,
            "start_guid": edge.start_guid,
            "end_guid": edge.end_guid,
            "distance_m": edge.distance_m,
            "status": edge.status,
            "reasons": edge.reasons,
            "path": edge.path,
            "via_space_guid": edge.via_space_guid,
            "via_space_label": edge.via_space_label,
        }
        by_id[edge.edge_id] = item
        graph[edge.start_guid].append(item)
        reverse = dict(item)
        reverse["start_guid"], reverse["end_guid"] = edge.end_guid, edge.start_guid
        reverse["path"] = list(reversed(edge.path))
        graph[edge.end_guid].append(reverse)
    with open(path, "wb") as handle:
        pickle.dump({"graph": dict(graph), "edges": by_id}, handle)


def routes_from_start(edges: list[RouteEdge], start_guid: str, pass_only: bool = False) -> list[dict]:
    graph = defaultdict(list)
    for edge in edges:
        if pass_only and edge.status != "pass":
            continue
        graph[edge.start_guid].append((edge.end_guid, edge))
        graph[edge.end_guid].append((edge.start_guid, edge))
    counter = 0
    queue = [(0.0, counter, start_guid, [])]
    seen: dict[str, float] = {}
    result = []
    while queue:
        dist, _counter_value, guid, path_edges = heapq.heappop(queue)
        if guid in seen:
            continue
        seen[guid] = dist
        if guid != start_guid:
            result.append({"target_guid": guid, "distance_m": dist, "edge_ids": [e.edge_id for e in path_edges]})
        for nxt, edge in graph.get(guid, []):
            if nxt not in seen:
                counter += 1
                heapq.heappush(queue, (dist + edge.distance_m, counter, nxt, path_edges + [edge]))
    return result
