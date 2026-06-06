from __future__ import annotations

import heapq
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
            path = _path_through_space(door_a, door_b, space)
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
                    source="IFC space boundaries and floor geometry",
                    via_space_guid=space.guid,
                    via_space_label=space.label,
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
    half = RULE_LIMITS.clearance_width_m / 2
    for point in _sample_path(path, step=0.35):
        x, y, z = point
        route_min = (x - half, y - half, z - 0.05)
        route_max = (x + half, y + half, z + RULE_LIMITS.clearance_height_m)
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


def _path_through_space(door_a: Element, door_b: Element, space: Element) -> list[tuple[float, float, float]]:
    a = door_a.center
    b = door_b.center
    c = space.center
    z = max(a[2], b[2], c[2])
    if not space.bbox_min or not space.bbox_max:
        return _orthogonal_between(a, b, z)

    min_x, min_y, _min_z = space.bbox_min
    max_x, max_y, _max_z = space.bbox_max
    margin = min(0.35, max(0.05, min(abs(max_x - min_x), abs(max_y - min_y)) * 0.08))

    entry_a = (
        _clamp(a[0], min_x + margin, max_x - margin),
        _clamp(a[1], min_y + margin, max_y - margin),
        z,
    )
    entry_b = (
        _clamp(b[0], min_x + margin, max_x - margin),
        _clamp(b[1], min_y + margin, max_y - margin),
        z,
    )

    width = abs(max_x - min_x)
    depth = abs(max_y - min_y)
    if width >= depth:
        lane_y = _clamp(c[1], min_y + margin, max_y - margin)
        points = [a, entry_a, (entry_a[0], lane_y, z), (entry_b[0], lane_y, z), entry_b, b]
    else:
        lane_x = _clamp(c[0], min_x + margin, max_x - margin)
        points = [a, entry_a, (lane_x, entry_a[1], z), (lane_x, entry_b[1], z), entry_b, b]
    return _dedupe_points(points)


def _orthogonal_between(a: tuple[float, float, float], b: tuple[float, float, float], z: float) -> list[tuple[float, float, float]]:
    mid = (b[0], a[1], z)
    return _dedupe_points([a, (a[0], a[1], z), mid, (b[0], b[1], z), b])


def _clamp(value: float, low: float, high: float) -> float:
    if low > high:
        return (low + high) / 2
    return max(low, min(high, value))


def _dedupe_points(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    result: list[tuple[float, float, float]] = []
    for point in points:
        if not result or distance(result[-1], point) > 0.03:
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
