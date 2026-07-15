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

ACC = Namespace(NS["acc"])


def build_route_edges(ifc_path: Path, elements: list[Element], skipped_pairs: list[dict] | None = None) -> list[RouteEdge]:
    space_edges = _space_boundary_route_edges(ifc_path, elements, skipped_pairs)
    if space_edges:
        return space_edges
    raise RuntimeError("No usable IfcRelSpaceBoundary door-to-space route graph was found in the IFC model.")


def _space_boundary_route_edges(
    ifc_path: Path,
    elements: list[Element],
    skipped_pairs: list[dict] | None = None,
) -> list[RouteEdge]:
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
        grid = _build_occupancy_grid(space, obstacles, unique_doors)
        for door_a, door_b in combinations(unique_doors, 2):
            pair_key = tuple(sorted((door_a.guid, door_b.guid)) + [space_guid])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            try:
                path = _path_through_space(door_a, door_b, space, grid)
            except RuntimeError as exc:
                if skipped_pairs is not None:
                    skipped_pairs.append(
                        {
                            "startGuid": door_a.guid,
                            "endGuid": door_b.guid,
                            "spaceGuid": space.guid,
                            "spaceLabel": space.label,
                            "message": str(exc),
                        }
                    )
                continue
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
        height = _num(start.extra.get("derivedDoorHeightM"))
        if height is not None:
            measurements["routeDoorHeightMinM"] = height
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
    measurements["routeGridStepM"] = GRID_STEP_M
    measurements["routeVisualClearanceM"] = VISUAL_CHAIR_CLEARANCE_M
    measurements["routeIsOrthogonal"] = True
    width_a = door_a.extra.get("derivedDoorWidthM")
    width_b = door_b.extra.get("derivedDoorWidthM")
    widths = [float(width) for width in (width_a, width_b) if width is not None]
    if widths:
        measurements["routeDoorWidthMinM"] = min(widths)
    height_a = door_a.extra.get("derivedDoorHeightM")
    height_b = door_b.extra.get("derivedDoorHeightM")
    heights = [float(height) for height in (height_a, height_b) if height is not None]
    if heights:
        measurements["routeDoorHeightMinM"] = min(heights)
    if space:
        clear = _num(space.extra.get("derivedClearSpaceWidthM"))
        if clear is not None:
            measurements["routeClearWidthM"] = clear
        measurements["routeHasTurn"] = _path_has_turn(path)
    measurements["routeHitsStair"] = _route_hits_stair(path, obstacles, space)
    measurements.update(_ramp_measurements(obstacles, space, path))
    return measurements


def route_measurements(
    door_a: Element,
    door_b: Element,
    path: list[tuple[float, float, float]],
    obstacles: list[Element],
    space: Element | None,
) -> dict[str, float | str | bool | None]:
    return _route_measurements(door_a, door_b, path, obstacles, space)


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
        run_length = _num(ramp.extra.get("rampRunLengthM"))
        measurements["routeUsesRamp"] = True
        if slope is not None:
            measurements["routeRampSlopePercent"] = slope
        if width is not None:
            measurements["routeRampUsableWidthM"] = width
        if run_length is not None:
            measurements["routeRampRunLengthM"] = run_length
    return measurements


def _same_level(a: Element, b: Element) -> bool:
    if a.center and b.center and abs(a.center[2] - b.center[2]) <= 2.2:
        return True
    return bool(a.storey and b.storey and a.storey == b.storey)


def _path_through_space(
    door_a: Element,
    door_b: Element,
    space: Element,
    grid: dict,
) -> list[tuple[float, float, float]]:
    """Return a collision-free four-direction path between exact door centres."""
    a = door_a.center
    b = door_b.center
    z = _route_elevation(door_a, door_b, space)
    start = (a[0], a[1], z)
    end = (b[0], b[1], z)
    cells = _astar_grid(grid, _grid_cell(grid, start), _grid_cell(grid, end))
    if not cells:
        raise RuntimeError(
            f"No collision-free orthogonal route between doors {door_a.guid} and {door_b.guid} "
            f"through space {space.guid}."
        )
    grid_points = [_cell_point(grid, cell, z) for cell in cells]
    start_connector = _endpoint_connector(start, grid_points[0], grid)
    end_connector = list(reversed(_endpoint_connector(end, grid_points[-1], grid)))
    path = _dedupe_points(start_connector + grid_points[1:-1] + end_connector)
    path = [(point[0], point[1], z) for point in _simplify_plan_points(path)]
    dense = _densify_orthogonal(path)
    if not _path_clear(dense, grid):
        raise RuntimeError(
            f"Generated route failed its final obstacle check between doors {door_a.guid} and {door_b.guid}."
        )
    return dense


def _route_elevation(door_a: Element, door_b: Element, space: Element) -> float:
    bottoms = [item.bbox_min[2] for item in (door_a, door_b) if item.bbox_min]
    if bottoms:
        return max(bottoms) + 0.05
    if space.bbox_min:
        return space.bbox_min[2] + 0.05
    return min(door_a.center[2], door_b.center[2])


GRID_STEP_M = 0.10
VISUAL_CHAIR_CLEARANCE_M = 0.38


def _build_occupancy_grid(space: Element, obstacles: list[Element], doors: list[Element]) -> dict:
    if not space.bbox_min or not space.bbox_max:
        raise RuntimeError(f"Space {space.guid} has no bounding box for route generation.")
    step = GRID_STEP_M
    door_x = [door.center[0] for door in doors if door.center]
    door_y = [door.center[1] for door in doors if door.center]
    min_x = min([space.bbox_min[0], *door_x]) - 0.6
    min_y = min([space.bbox_min[1], *door_y]) - 0.6
    max_x = max([space.bbox_max[0], *door_x]) + 0.6
    max_y = max([space.bbox_max[1], *door_y]) + 0.6
    origin_x = math.floor(min_x / step) * step
    origin_y = math.floor(min_y / step) * step
    nx = math.ceil((max_x - origin_x) / step) + 1
    ny = math.ceil((max_y - origin_y) / step) + 1
    z = (space.bbox_min[2] + 0.05) if space.bbox_min else space.center[2]
    grid = {"step": step, "origin_x": origin_x, "origin_y": origin_y, "nx": nx, "ny": ny, "blocked": set()}
    wall_cells: set[tuple[int, int]] = set()
    hard_cells: set[tuple[int, int]] = set()
    for item in obstacles:
        if not item.bbox_min or not item.bbox_max:
            continue
        if item.bbox_max[2] < z or item.bbox_min[2] > z + RULE_LIMITS.clearance_height_m:
            continue
        if item.bbox_max[0] < min_x or item.bbox_min[0] > max_x or item.bbox_max[1] < min_y or item.bbox_min[1] > max_y:
            continue
        target = wall_cells if item.ifc_type == "IfcWall" else hard_cells
        _mark_box_cells(grid, item.bbox_min, item.bbox_max, VISUAL_CHAIR_CLEARANCE_M, target)
    for door in doors:
        if door.center and door.bbox_min and door.bbox_max:
            _carve_door_portal(grid, door, wall_cells)
    grid["blocked"] = wall_cells | hard_cells
    return grid


def _mark_box_cells(grid: dict, bbox_min, bbox_max, clearance: float, target: set) -> None:
    low = _grid_cell(grid, (bbox_min[0] - clearance, bbox_min[1] - clearance, 0))
    high = _grid_cell(grid, (bbox_max[0] + clearance, bbox_max[1] + clearance, 0))
    for ix in range(max(0, low[0]), min(grid["nx"] - 1, high[0]) + 1):
        for iy in range(max(0, low[1]), min(grid["ny"] - 1, high[1]) + 1):
            target.add((ix, iy))


def _carve_door_portal(grid: dict, door: Element, wall_cells: set) -> None:
    width_x = abs(door.bbox_max[0] - door.bbox_min[0])
    width_y = abs(door.bbox_max[1] - door.bbox_min[1])
    cx, cy, _ = door.center
    if width_x >= width_y:
        half_x = max(grid["step"] * 0.55, width_x / 2 - VISUAL_CHAIR_CLEARANCE_M)
        half_y = width_y / 2 + VISUAL_CHAIR_CLEARANCE_M + 0.25
    else:
        half_x = width_x / 2 + VISUAL_CHAIR_CLEARANCE_M + 0.25
        half_y = max(grid["step"] * 0.55, width_y / 2 - VISUAL_CHAIR_CLEARANCE_M)
    low = _grid_cell(grid, (cx - half_x, cy - half_y, 0))
    high = _grid_cell(grid, (cx + half_x, cy + half_y, 0))
    for ix in range(max(0, low[0]), min(grid["nx"] - 1, high[0]) + 1):
        for iy in range(max(0, low[1]), min(grid["ny"] - 1, high[1]) + 1):
            wall_cells.discard((ix, iy))


def _grid_cell(grid: dict, point) -> tuple[int, int]:
    return (
        math.floor((point[0] - grid["origin_x"]) / grid["step"] + 0.5),
        math.floor((point[1] - grid["origin_y"]) / grid["step"] + 0.5),
    )


def _cell_point(grid: dict, cell: tuple[int, int], z: float) -> tuple[float, float, float]:
    return (
        grid["origin_x"] + cell[0] * grid["step"],
        grid["origin_y"] + cell[1] * grid["step"],
        z,
    )


def _astar_grid(grid: dict, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]] | None:
    blocked = grid["blocked"]
    if start in blocked or end in blocked:
        return None
    queue = [(abs(start[0] - end[0]) + abs(start[1] - end[1]), 0, start)]
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    cost = {start: 0}
    while queue:
        _score, current_cost, current = heapq.heappop(queue)
        if current == end:
            path = [current]
            while current in parent:
                current = parent[current]
                path.append(current)
            return list(reversed(path))
        if current_cost != cost.get(current):
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbour = (current[0] + dx, current[1] + dy)
            if neighbour[0] < 0 or neighbour[0] >= grid["nx"] or neighbour[1] < 0 or neighbour[1] >= grid["ny"] or neighbour in blocked:
                continue
            next_cost = current_cost + 1
            if next_cost >= cost.get(neighbour, math.inf):
                continue
            cost[neighbour] = next_cost
            parent[neighbour] = current
            heuristic = abs(neighbour[0] - end[0]) + abs(neighbour[1] - end[1])
            heapq.heappush(queue, (next_cost + heuristic, next_cost, neighbour))
    return None


def _endpoint_connector(endpoint, grid_point, grid: dict):
    endpoint_cell = _grid_cell(grid, endpoint)
    grid_cell = _grid_cell(grid, grid_point)
    if endpoint_cell != grid_cell or endpoint_cell in grid["blocked"]:
        raise RuntimeError(f"Door endpoint at {endpoint[:2]} is not in its free portal cell.")
    candidates = [
        [endpoint, (grid_point[0], endpoint[1], endpoint[2]), grid_point],
        [endpoint, (endpoint[0], grid_point[1], endpoint[2]), grid_point],
    ]
    return min((_dedupe_points(candidate) for candidate in candidates), key=lambda points: sum(distance(a, b) for a, b in zip(points, points[1:])))


def _path_clear(path, grid: dict) -> bool:
    for start, end in zip(path, path[1:]):
        if abs(start[0] - end[0]) > 1e-8 and abs(start[1] - end[1]) > 1e-8:
            return False
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        count = max(1, math.ceil(length / (grid["step"] / 2)))
        for index in range(count + 1):
            t = index / count
            point = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t, 0)
            if _grid_cell(grid, point) in grid["blocked"]:
                return False
    return True


def _simplify_plan_points(points):
    result = []
    for point in points:
        if len(result) >= 2:
            a, b = result[-2], result[-1]
            if (a[0] == b[0] == point[0]) or (a[1] == b[1] == point[1]):
                result[-1] = point
                continue
        result.append(point)
    return result


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


def _clamp(value: float, low: float, high: float) -> float:
    if low > high:
        return (low + high) / 2
    return max(low, min(high, value))


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


def routes_from_start(
    edges: list[RouteEdge],
    start_guid: str,
    pass_only: bool = False,
    target_guids: set[str] | None = None,
) -> list[dict]:
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
        if guid != start_guid and (target_guids is None or guid in target_guids):
            result.append({"target_guid": guid, "distance_m": dist, "edge_ids": [e.edge_id for e in path_edges]})
        for nxt, edge in graph.get(guid, []):
            if nxt not in seen:
                counter += 1
                heapq.heappush(queue, (dist + edge.distance_m, counter, nxt, path_edges + [edge]))
    return result
