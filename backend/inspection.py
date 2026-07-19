from __future__ import annotations

import hashlib
import heapq
import math
from collections import defaultdict

from .config import RULE_LIMITS
from .model import Element


CORRIDOR_BOUNDARY_STEP = 0.55
CORRIDOR_SKELETON_CLEARANCE = 0.12
CORRIDOR_MOVEMENT_STEP = 0.40


def build_inspection_checks(elements: list[Element]) -> list[dict]:
    """Build 2D Inspect-mode facts without changing validation or routing."""
    checks: list[dict] = []
    blockers = [
        element
        for element in elements
        if element.ifc_type in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"}
        and isinstance(element.extra.get("_inspectionFootprint"), dict)
    ]
    for element in elements:
        if element.ifc_type == "IfcDoor" and element.extra.get("isRouteRelevantDoor", True):
            checks.extend(_door_checks(element))
        elif element.ifc_type == "IfcSpace" and element.extra.get("isCorridorLike"):
            checks.extend(_corridor_checks(element, blockers))
        elif element.ifc_type in {"IfcRamp", "IfcRampFlight"}:
            checks.extend(_ramp_checks(element))
    return checks


def _door_checks(element: Element) -> list[dict]:
    width = _number(element.extra.get("inspectionDoorWidthM"))
    if width is None:
        width = _number(element.extra.get("derivedDoorWidthM"))
    height = _number(element.extra.get("inspectionDoorHeightM"))
    if height is None:
        height = _number(element.extra.get("derivedDoorHeightM"))
    checks = [
        _comparison_check(
            element,
            "door_width",
            "Door clear width",
            width,
            RULE_LIMITS.door_width_m,
            "minimum",
            "m",
            str(element.extra.get("doorWidthSource") or "IFC door geometry or declared width"),
        ),
        _comparison_check(
            element,
            "door_height",
            "Door clear height",
            height,
            RULE_LIMITS.door_height_m,
            "minimum",
            "m",
            str(element.extra.get("doorHeightSource") or "IFC door geometry or declared height"),
            unavailable_status="advisory",
            failure_status="advisory",
        ),
    ]
    note = element.extra.get("inspectionDoorDimensionNote")
    if note:
        for check in checks:
            check["note"] = str(note)
    return checks


def _corridor_checks(element: Element, blockers: list[Element]) -> list[dict]:
    width = _number(element.extra.get("derivedClearSpaceWidthM"))
    length = _number(element.extra.get("derivedCorridorLengthM"))
    slope = _number(element.extra.get("derivedCorridorSlopePercent"))
    turning = _number(element.extra.get("turningSpaceM"))
    slope_limit = (
        RULE_LIMITS.short_corridor_slope_percent
        if length is not None and length <= RULE_LIMITS.short_corridor_length_m
        else RULE_LIMITS.corridor_slope_percent
    )
    checks = [
        _comparison_check(
            element,
            "corridor_width",
            "Corridor clear width",
            width,
            RULE_LIMITS.corridor_width_m,
            "minimum",
            "m",
            "IFC space geometry",
        ),
        _comparison_check(
            element,
            "corridor_slope",
            "Corridor floor slope",
            slope,
            slope_limit,
            "maximum",
            "%",
            str(element.extra.get("corridorSlopeSource") or "IFC space floor faces"),
        ),
        _comparison_check(
            element,
            "turning_space",
            "Turning space",
            turning,
            RULE_LIMITS.turning_space_m,
            "minimum",
            "m",
            "IFC space geometry",
        ),
    ]
    checks.append(_corridor_passing_check(element, blockers))
    return checks


def _ramp_checks(element: Element) -> list[dict]:
    width = _number(element.extra.get("rampUsableWidthM"))
    slope = _number(element.extra.get("rampSlopePercent"))
    run = _number(element.extra.get("inspectionRampRunLengthM"))
    if run is None:
        run = _number(element.extra.get("rampRunLengthM"))
    return [
        _comparison_check(
            element,
            "ramp_width",
            "Ramp usable width",
            width,
            RULE_LIMITS.ramp_width_m,
            "minimum",
            "m",
            "IFC ramp geometry",
        ),
        _comparison_check(
            element,
            "ramp_slope",
            "Ramp slope",
            slope,
            RULE_LIMITS.ramp_slope_percent,
            "maximum",
            "%",
            "IFC ramp geometry",
        ),
        _comparison_check(
            element,
            "ramp_run_length",
            "Ramp flight length",
            run,
            RULE_LIMITS.ramp_run_length_m,
            "maximum",
            "m",
            str(element.extra.get("inspectionRampRunSource") or "IFC ramp plan geometry"),
        ),
    ]


def _comparison_check(
    element: Element,
    rule_id: str,
    label: str,
    measured: float | None,
    required: float,
    comparison: str,
    unit: str,
    source: str,
    unavailable_status: str = "unavailable",
    failure_status: str = "fail",
) -> dict:
    if measured is None:
        status = unavailable_status
    elif comparison == "minimum":
        status = "pass" if measured + 1e-6 >= required else failure_status
    else:
        status = "pass" if measured <= required + 1e-6 else failure_status
    return {
        "checkId": _check_id(element.guid, rule_id),
        "elementGuid": element.guid,
        "ruleId": rule_id,
        "label": label,
        "status": status,
        "measured": round(measured, 4) if measured is not None else None,
        "required": required,
        "comparison": comparison,
        "unit": unit,
        "source": source,
    }


def _corridor_passing_check(element: Element, blockers: list[Element]) -> dict:
    result = _corridor_movement_measurement(element, blockers)
    measured = result.get("measured")
    check = _comparison_check(
        element,
        "corridor_movement_area",
        "1.80 x 1.80 m passing-area spacing",
        measured,
        RULE_LIMITS.corridor_movement_interval_m,
        "maximum",
        "m",
        str(result.get("source") or "IFC corridor footprint"),
    )
    check["movementSpaceM"] = RULE_LIMITS.corridor_movement_space_m
    check["corridorLengthM"] = result.get("length")
    check["evidence"] = result.get("evidence")
    if result.get("reason"):
        check["note"] = result["reason"]
    return check


def _corridor_movement_measurement(element: Element, blockers: list[Element]) -> dict:
    from shapely.geometry import shape
    from shapely.ops import unary_union

    mapping = element.extra.get("_inspectionFootprint")
    if not isinstance(mapping, dict):
        return {"measured": None, "source": "unavailable", "reason": "Corridor footprint is not available."}
    try:
        area = shape(mapping).buffer(0)
    except Exception:
        return {"measured": None, "source": "unavailable", "reason": "Corridor footprint could not be read."}
    if area.is_empty:
        return {"measured": None, "source": "unavailable", "reason": "Corridor footprint is empty."}
    obstacle_shapes = []
    for blocker in blockers:
        if not _z_ranges_overlap(element, blocker):
            continue
        try:
            obstacle = shape(blocker.extra["_inspectionFootprint"]).buffer(0)
        except Exception:
            continue
        if not obstacle.is_empty and obstacle.intersects(area):
            obstacle_shapes.append(obstacle)
    if obstacle_shapes:
        area = area.difference(unary_union(obstacle_shapes)).buffer(0)
    if area.is_empty:
        return {
            "measured": None,
            "source": "IFC corridor and obstacle footprints",
            "reason": "Walls, columns, or stairs leave no usable corridor footprint.",
        }
    segments = _space_skeleton_segments(area)
    if not segments:
        return {
            "measured": None,
            "source": "IFC corridor footprint",
            "reason": "A stable corridor centre line could not be derived from this footprint.",
        }
    result = _corridor_movement_area_region(element, area, segments)
    if obstacle_shapes:
        result["source"] = "IFC corridor footprint with overlapping walls, columns, and stairs removed"
    return result


def _z_ranges_overlap(first: Element, second: Element) -> bool:
    if not first.bbox_min or not first.bbox_max or not second.bbox_min or not second.bbox_max:
        return False
    return max(first.bbox_min[2], second.bbox_min[2]) <= min(first.bbox_max[2], second.bbox_max[2]) + 0.02


def _corridor_movement_area_region(element: Element, area, segments) -> dict:
    from shapely.geometry import LineString, mapping
    from shapely.ops import unary_union

    graph: dict[tuple[float, float], dict[tuple[float, float], float]] = defaultdict(dict)
    for first, second in segments:
        segment_length = math.dist(first, second)
        if segment_length <= 1e-6:
            continue
        count = max(1, math.ceil(segment_length / CORRIDOR_MOVEMENT_STEP))
        points = [
            (
                round(first[0] + (second[0] - first[0]) * index / count, 3),
                round(first[1] + (second[1] - first[1]) * index / count, 3),
            )
            for index in range(count + 1)
        ]
        for first_point, second_point in zip(points, points[1:]):
            distance = math.dist(first_point, second_point)
            graph[first_point][second_point] = min(distance, graph[first_point].get(second_point, math.inf))
            graph[second_point][first_point] = min(distance, graph[second_point].get(first_point, math.inf))
    if not graph:
        return {"measured": None, "source": "IFC corridor footprint", "reason": "No corridor centre-line graph was available."}

    length, _start, _end = _graph_diameter(graph, set(graph))
    side = RULE_LIMITS.corridor_movement_space_m
    boundary = area.boundary
    wide = {node for node in graph if _corridor_movement_square_fits(area, node, side, boundary)}
    narrow = set(graph) - wide
    gaps = []
    remaining = set(narrow)
    while remaining:
        start = min(remaining)
        component = _graph_component(graph, start, narrow)
        remaining.difference_update(component)
        gap, first, second = _graph_diameter(graph, component)
        if first is not None:
            gap += _corridor_movement_boundary_distance(area, graph, component, wide, first, side)
        if second is not None and second != first:
            gap += _corridor_movement_boundary_distance(area, graph, component, wide, second, side)
        lines = [
            LineString([first_node, second_node])
            for first_node in component
            for second_node in graph[first_node]
            if second_node in component and first_node < second_node
        ]
        if not lines:
            continue
        geometry = unary_union(lines).buffer(0.20, cap_style=2, join_style=2).intersection(area).buffer(0)
        if geometry.is_empty:
            continue
        gaps.append({"measured": round(gap, 4), "geometry": mapping(geometry)})
    worst = max(gaps, key=lambda item: item["measured"], default=None)
    return {
        "measured": worst["measured"] if worst else 0.0,
        "length": round(length, 4),
        "evidence": worst["geometry"] if worst else None,
        "source": "IFC corridor footprint and centre-line analysis",
    }


def _corridor_movement_square_fits(area, node, side: float, boundary=None) -> bool:
    from shapely.geometry import Point

    point = Point(node[0], node[1])
    boundary = boundary if boundary is not None else area.boundary
    if point.distance(boundary) < side / 2 - 0.01:
        return False
    return _turning_square_side(area, point, side) >= side - 0.005


def _corridor_movement_boundary_distance(area, graph, component, wide, node, side: float) -> float:
    neighbours = [neighbour for neighbour in graph[node] if neighbour in wide and neighbour not in component]
    values = []
    for neighbour in neighbours:
        low = node
        high = neighbour
        for _ in range(8):
            middle = ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)
            if _corridor_movement_square_fits(area, middle, side):
                high = middle
            else:
                low = middle
        values.append(math.dist(node, high))
    return min(values, default=0.0)


def _graph_component(graph, start, allowed) -> set:
    result = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for neighbour in graph[current]:
            if neighbour in allowed and neighbour not in result:
                result.add(neighbour)
                queue.append(neighbour)
    return result


def _graph_diameter(graph, nodes: set) -> tuple[float, tuple | None, tuple | None]:
    best = 0.0, None, None
    for start in nodes:
        distances = {start: 0.0}
        queue = [(0.0, start)]
        while queue:
            current_distance, current = heapq.heappop(queue)
            if current_distance != distances.get(current):
                continue
            if current_distance > best[0]:
                best = current_distance, start, current
            for neighbour, length in graph[current].items():
                if neighbour not in nodes:
                    continue
                value = current_distance + length
                if value >= distances.get(neighbour, math.inf):
                    continue
                distances[neighbour] = value
                heapq.heappush(queue, (value, neighbour))
    return best


def _space_skeleton_segments(area) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    from shapely import STRtree, union_all, voronoi_polygons
    from shapely.geometry import MultiPoint

    samples = _space_boundary_samples(area)
    if len(samples) < 4:
        return []
    sample_points = [item[0] for item in samples]
    safe = area.buffer(-CORRIDOR_SKELETON_CLEARANCE)
    if safe.is_empty:
        return []
    try:
        tree = STRtree(sample_points)
        edges = voronoi_polygons(MultiPoint(sample_points), extend_to=area.envelope, only_edges=True)
    except Exception:
        return []
    tolerance = max(0.03, CORRIDOR_BOUNDARY_STEP * 0.08)
    parts = []
    for part in _line_parts(edges.intersection(safe)):
        if part.length < 0.08:
            continue
        midpoint = part.interpolate(0.5, normalized=True)
        _nearest, nearest_distances = tree.query_nearest(midpoint, all_matches=True, return_distance=True)
        if not len(nearest_distances):
            continue
        nearby = tree.query(midpoint, predicate="dwithin", distance=float(min(nearest_distances)) + tolerance)
        ranked = sorted((samples[int(index)] for index in nearby), key=lambda item: midpoint.distance(item[0]))
        if len(ranked) < 2 or midpoint.distance(ranked[1][0]) - midpoint.distance(ranked[0][0]) > tolerance:
            continue
        first, second = ranked[:2]
        if first[1] == second[1]:
            difference = abs(first[2] - second[2])
            difference = min(difference, first[3] - difference)
            if difference <= 3:
                continue
        parts.append(part)
    if not parts:
        return []
    geometry = union_all(parts, grid_size=0.001)
    result = {}
    for part in _line_parts(geometry):
        for first, second in zip(part.coords, part.coords[1:]):
            if math.dist(first, second) < 0.08:
                continue
            first_point = float(first[0]), float(first[1])
            second_point = float(second[0]), float(second[1])
            key = tuple(sorted((tuple(round(value, 3) for value in first_point), tuple(round(value, 3) for value in second_point))))
            result[key] = first_point, second_point
    return list(result.values())


def _space_boundary_samples(area) -> list[tuple[object, int, int, int]]:
    from shapely.geometry import LineString

    result = []
    ring_index = 0
    for polygon in _polygon_parts(area):
        for ring in [polygon.exterior, *polygon.interiors]:
            line = LineString(ring.coords)
            count = max(4, math.ceil(line.length / CORRIDOR_BOUNDARY_STEP))
            for index in range(count):
                result.append((line.interpolate(index / count, normalized=True), ring_index, index, count))
            ring_index += 1
    return result


def _turning_square_side(area, point, limit: float) -> float:
    from shapely.affinity import rotate

    best = 0.0
    for angle in _turning_square_angles(area):
        value = rotate(area, -angle, origin=(point.x, point.y)) if angle else area
        best = max(best, _axis_aligned_turning_square_side(value, point, limit))
        if best >= limit - 1e-6:
            return limit
    return best


def _turning_square_angles(area) -> list[float]:
    rectangle = area.minimum_rotated_rectangle
    coordinates = list(rectangle.exterior.coords)
    angles = {0.0}
    for first, second in zip(coordinates, coordinates[1:]):
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        if math.hypot(dx, dy) <= 0.03:
            continue
        angle = math.degrees(math.atan2(dy, dx)) % 90.0
        if angle > 45.0:
            angle -= 90.0
        if abs(angle) > 0.01:
            angles.add(round(angle, 4))
    return sorted(angles)


def _axis_aligned_turning_square_side(area, point, limit: float) -> float:
    safe = area.buffer(1e-7)
    coordinates = _turning_boundary_coordinates(area)
    if _axis_aligned_turning_square_fits(safe, point, limit, coordinates):
        return limit
    low = 0.0
    high = limit
    for _ in range(9):
        value = (low + high) / 2
        if _axis_aligned_turning_square_fits(safe, point, value, coordinates):
            low = value
        else:
            high = value
    return low


def _axis_aligned_turning_square_fits(area, point, side: float, coordinates) -> bool:
    from shapely.geometry import box
    from shapely.prepared import prep

    if side <= 1e-6:
        return True
    half = side / 2
    min_x, min_y, max_x, max_y = area.bounds
    low_x = max(point.x - half, min_x + half)
    high_x = min(point.x + half, max_x - half)
    low_y = max(point.y - half, min_y + half)
    high_y = min(point.y + half, max_y - half)
    if low_x > high_x + 1e-7 or low_y > high_y + 1e-7:
        return False
    x_values = _turning_axis_candidates([value[0] for value in coordinates], low_x, high_x, point.x, half)
    y_values = _turning_axis_candidates([value[1] for value in coordinates], low_y, high_y, point.y, half)
    prepared = prep(area)
    for x in x_values:
        for y in y_values:
            if prepared.covers(box(x - half, y - half, x + half, y + half)):
                return True
    return False


def _turning_axis_candidates(boundaries, low: float, high: float, origin: float, half: float) -> list[float]:
    values = {low, high, (low + high) / 2}
    if low <= origin <= high:
        values.add(origin)
    for boundary in boundaries:
        for value in [boundary - half, boundary, boundary + half]:
            if low - 1e-7 <= value <= high + 1e-7:
                values.add(min(high, max(low, value)))
    value = math.ceil(low * 10 - 1e-7) / 10
    while value <= high + 1e-7:
        values.add(min(high, max(low, value)))
        value += 0.1
    return sorted(values, key=lambda item: (abs(item - origin), item))


def _turning_boundary_coordinates(area) -> list[tuple[float, float]]:
    coordinates = []
    for polygon in _polygon_parts(area.simplify(0.01, preserve_topology=True)):
        for ring in [polygon.exterior, *polygon.interiors]:
            coordinates.extend((float(x), float(y)) for x, y in ring.coords)
    return coordinates


def _polygon_parts(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
        return
    if hasattr(geometry, "geoms"):
        for item in geometry.geoms:
            yield from _polygon_parts(item)


def _line_parts(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type in {"LineString", "LinearRing"}:
        yield geometry
        return
    if hasattr(geometry, "geoms"):
        for item in geometry.geoms:
            yield from _line_parts(item)


def _check_id(guid: str, rule_id: str) -> str:
    return "I" + hashlib.sha1(f"{guid}:{rule_id}".encode("utf-8")).hexdigest()[:11].upper()


def _number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
