from __future__ import annotations

import hashlib
import heapq
import math
from collections import defaultdict
from dataclasses import replace
from itertools import combinations
from pathlib import Path

from .config import RULE_LIMITS
from .geometry import distance, obstacle_elements
from .model import Element, RouteEdge
from .routes import _route_measurements

PLAN_GRID_STEP = 0.20
PLAN_ROUTE_HALF_WIDTH = 0.04
CORRIDOR_BOUNDARY_STEP = 0.55
CORRIDOR_SKELETON_CLEARANCE = 0.12


def prepare_plan_geometry(
    ifc_path: Path,
    elements: list[Element],
) -> tuple[dict[str, object], dict[str, list[str]]]:
    import ifcopenshell

    model = ifcopenshell.open(str(ifc_path))
    footprints = _element_footprints(model, elements)
    floor_refs = _floor_refs(elements)
    spaces_by_door = _door_to_spaces(model)
    _add_geometric_door_spaces(spaces_by_door, elements, footprints, floor_refs)
    _filter_door_spaces(spaces_by_door, elements, footprints, _door_normals(model, elements))
    _set_space_clearance_regions(elements, footprints, spaces_by_door)
    return footprints, spaces_by_door


def build_plan_route_edges(
    ifc_path: Path,
    elements: list[Element],
    route_edges: list[RouteEdge],
    prepared: tuple[dict[str, object], dict[str, list[str]]] | None = None,
) -> list[RouteEdge]:
    footprints, spaces_by_door = prepared or prepare_plan_geometry(ifc_path, elements)
    candidates = _plan_candidates(elements, footprints, spaces_by_door, route_edges)
    return build_plan_network(elements, candidates, route_edges)


def build_plan_network(
    elements: list[Element],
    candidates: list[RouteEdge],
    route_edges: list[RouteEdge] | None = None,
) -> list[RouteEdge]:
    roles: dict[str, set[str]] = defaultdict(set)
    by_id = {edge.edge_id: edge for edge in candidates}
    for edge in _select_forest(candidates, pass_only=False):
        roles[edge.edge_id].add("physical")
    for edge in _select_forest(candidates, pass_only=True):
        roles[edge.edge_id].add("accessible")
    for edge in _issue_witnesses(candidates, elements):
        roles[edge.edge_id].add("issue")

    result = []
    for edge_id, edge_roles in roles.items():
        edge = by_id[edge_id]
        measurements = dict(edge.measurements)
        measurements["planNetworkRole"] = " ".join(sorted(edge_roles))
        result.append(replace(edge, measurements=measurements))

    result.extend(_stair_markers(route_edges or []))
    connected = {guid for edge in candidates for guid in (edge.start_guid, edge.end_guid)}
    result.extend(_unreachable_markers(elements, connected))
    return sorted(result, key=lambda edge: edge.edge_id)


def _plan_candidates(
    elements: list[Element],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
    route_edges: list[RouteEdge],
) -> list[RouteEdge]:
    by_guid = {element.guid: element for element in elements}
    doors_by_space: dict[str, list[Element]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        door = by_guid.get(door_guid)
        if not _route_door(door):
            continue
        for space_guid in space_guids:
            space = by_guid.get(space_guid)
            if space and space.ifc_type == "IfcSpace" and not space.extra.get("isExcludedSpace"):
                doors_by_space[space_guid].append(door)

    current: dict[tuple[str, str, str], list[RouteEdge]] = defaultdict(list)
    for edge in route_edges:
        if not edge.via_space_guid:
            continue
        key = _pair_key(edge.start_guid, edge.end_guid, edge.via_space_guid)
        current[key].append(edge)

    obstacles = obstacle_elements(elements)
    candidates = []
    for space_guid, doors in sorted(doors_by_space.items()):
        space = by_guid.get(space_guid)
        unique_doors = sorted({door.guid: door for door in doors}.values(), key=lambda door: door.guid)
        if space is None or len(unique_doors) < 2:
            continue
        area = _space_walkable_area(space, unique_doors, obstacles, footprints)
        grid = _area_grid(area) if area is not None and not area.is_empty else None
        for first, second in combinations(unique_doors, 2):
            key = _pair_key(first.guid, second.guid, space_guid)
            matched = min(current.get(key, []), key=lambda edge: edge.distance_m, default=None)
            path = None
            if matched and area is not None and _path_inside_area(matched.path, area):
                path = _compact_path(matched.path)
            elif grid:
                path = _path_in_area(first, second, space, area, grid)
            elif matched:
                path = _compact_path(matched.path)
            if not path or len(path) < 2:
                continue
            measurements = dict(matched.measurements) if matched else _route_measurements(first, second, path, obstacles, space)
            reasons = list(matched.reasons) if matched else _measurement_reasons(measurements)
            if area is not None and not area.is_empty:
                _set_route_clear_width(measurements, path, area, [first, second], footprints)
                reasons = [reason for reason in reasons if reason != "route_width"]
                clear = _number(measurements.get("routeClearWidthM"))
                if clear is not None and clear < RULE_LIMITS.corridor_width_m:
                    reasons.append("route_width")
            candidates.append(
                RouteEdge(
                    edge_id=_plan_edge_id(first.guid, second.guid, space_guid),
                    start_guid=first.guid,
                    end_guid=second.guid,
                    distance_m=_path_length(path),
                    status="fail" if reasons else "pass",
                    reasons=reasons,
                    path=path,
                    source="2D walkable area route",
                    via_space_guid=space.guid,
                    via_space_label=space.label,
                    measurements=measurements,
                )
            )
    return candidates


def _element_footprints(model, elements: list[Element]) -> dict[str, object]:
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    wanted = {
        element.guid
        for element in elements
        if element.ifc_type in {"IfcSpace", "IfcDoor", "IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"}
    }
    footprints = {}
    for ifc_type in ["IfcSpace", "IfcDoor", "IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"]:
        for obj in model.by_type(ifc_type):
            guid = getattr(obj, "GlobalId", None)
            if guid not in wanted:
                continue
            try:
                shape = ifcopenshell.geom.create_shape(settings, obj)
            except Exception:
                continue
            footprint = _shape_footprint(shape)
            if footprint is not None and not footprint.is_empty:
                footprints[guid] = footprint
    return footprints


def _shape_footprint(shape):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    verts = getattr(shape.geometry, "verts", None)
    faces = getattr(shape.geometry, "faces", None)
    if verts is None or faces is None or not len(verts) or not len(faces):
        return None
    polygons = []
    for index in range(0, len(faces), 3):
        points = []
        for vertex_index in faces[index : index + 3]:
            offset = int(vertex_index) * 3
            points.append((float(verts[offset]), float(verts[offset + 1])))
        polygon = Polygon(points)
        if polygon.is_valid and polygon.area > 1e-6:
            polygons.append(polygon)
    return unary_union(polygons).buffer(0) if polygons else None


def _element_polygon(element: Element, footprints: dict[str, object]):
    from shapely.geometry import box

    polygon = footprints.get(element.guid)
    if polygon is not None and not polygon.is_empty:
        return polygon
    if not element.bbox_min or not element.bbox_max:
        return None
    return box(element.bbox_min[0], element.bbox_min[1], element.bbox_max[0], element.bbox_max[1])


def _door_to_spaces(model) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = defaultdict(list)
    for relation in model.by_type("IfcRelSpaceBoundary"):
        space = getattr(relation, "RelatingSpace", None)
        element = getattr(relation, "RelatedBuildingElement", None)
        if space is None or element is None or not element.is_a("IfcDoor"):
            continue
        door_guid = getattr(element, "GlobalId", None)
        space_guid = getattr(space, "GlobalId", None)
        if door_guid and space_guid and space_guid not in mapping[door_guid]:
            mapping[door_guid].append(space_guid)
    return mapping


def _add_geometric_door_spaces(
    spaces_by_door: dict[str, list[str]],
    elements: list[Element],
    footprints: dict[str, object],
    floor_refs: list[tuple[str, float]],
) -> None:
    spaces = [
        element
        for element in elements
        if element.ifc_type == "IfcSpace" and not element.extra.get("isExcludedSpace") and element.center
    ]
    for door in elements:
        if not _route_door(door) or spaces_by_door.get(door.guid):
            continue
        door_polygon = _element_polygon(door, footprints)
        door_floor = _floor_name(door, floor_refs)
        if door_polygon is None or door_polygon.is_empty or not door_floor:
            continue
        candidates = []
        for space in spaces:
            if _floor_name(space, floor_refs) != door_floor:
                continue
            space_polygon = _element_polygon(space, footprints)
            if space_polygon is None or space_polygon.is_empty:
                continue
            gap = door_polygon.distance(space_polygon)
            if gap <= 0.35:
                candidates.append((gap, space.guid))
        if candidates:
            candidates.sort()
            best = candidates[0][0]
            spaces_by_door[door.guid] = [guid for gap, guid in candidates if gap <= best + 0.20][:2]


def _door_normals(model, elements: list[Element]) -> dict[str, tuple[float, float]]:
    from ifcopenshell.util.placement import get_local_placement

    result = {}
    for element in elements:
        if element.ifc_type != "IfcDoor":
            continue
        nx = _number(element.extra.get("doorDepthAxisX"))
        ny = _number(element.extra.get("doorDepthAxisY"))
        length = math.hypot(nx or 0.0, ny or 0.0)
        if nx is not None and ny is not None and length > 0.5:
            result[element.guid] = nx / length, ny / length
    for door in model.by_type("IfcDoor"):
        guid = getattr(door, "GlobalId", None)
        placement = getattr(door, "ObjectPlacement", None)
        if not guid or guid in result or placement is None:
            continue
        try:
            matrix = get_local_placement(placement)
            nx = float(matrix[0][1])
            ny = float(matrix[1][1])
        except Exception:
            continue
        length = math.hypot(nx, ny)
        if length > 0.5:
            result[guid] = nx / length, ny / length
    return result


def _filter_door_spaces(
    spaces_by_door: dict[str, list[str]],
    elements: list[Element],
    footprints: dict[str, object],
    door_normals: dict[str, tuple[float, float]],
) -> None:
    from shapely.geometry import LineString, Point

    by_guid = {element.guid: element for element in elements}
    for door_guid, space_guids in list(spaces_by_door.items()):
        door = by_guid.get(door_guid)
        normal = door_normals.get(door_guid)
        if door is None or not door.center or normal is None:
            continue
        point = Point(door.center[0], door.center[1])
        line = LineString(
            [
                (point.x - normal[0] * 1.5, point.y - normal[1] * 1.5),
                (point.x + normal[0] * 1.5, point.y + normal[1] * 1.5),
            ]
        )
        accepted = []
        for space_guid in space_guids:
            space = by_guid.get(space_guid)
            polygon = _element_polygon(space, footprints) if space else None
            if polygon is None or polygon.is_empty or not line.intersection(polygon.buffer(0.02)).is_empty:
                accepted.append(space_guid)
        if accepted:
            spaces_by_door[door_guid] = accepted


def _space_walkable_area(
    space: Element,
    doors: list[Element],
    obstacles: list[Element],
    footprints: dict[str, object],
):
    from shapely.ops import unary_union

    polygon = _element_polygon(space, footprints)
    if polygon is None or polygon.is_empty:
        return None
    blockers = []
    for obstacle in obstacles:
        if obstacle.ifc_type not in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"}:
            continue
        if not _z_overlap(space, obstacle):
            continue
        obstacle_polygon = _element_polygon(obstacle, footprints)
        if obstacle_polygon is not None and not obstacle_polygon.is_empty and obstacle_polygon.distance(polygon) <= 0.05:
            blockers.append(obstacle_polygon)
    area = polygon.buffer(0)
    if blockers:
        area = area.difference(unary_union(blockers)).buffer(0)
    openings = []
    for door in doors:
        door_polygon = _element_polygon(door, footprints)
        if door_polygon is not None and not door_polygon.is_empty:
            openings.append(door_polygon.buffer(0.22, cap_style=2, join_style=2))
    if openings:
        opening_area = unary_union(openings).intersection(polygon.buffer(0.75))
        area = unary_union([area, opening_area]).buffer(0)
    return area if not area.is_empty else None


def _set_space_clearance_regions(
    elements: list[Element],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
) -> None:
    by_guid = {element.guid: element for element in elements}
    doors_by_space: dict[str, list[Element]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        door = by_guid.get(door_guid)
        if not _route_door(door):
            continue
        for space_guid in space_guids:
            doors_by_space[space_guid].append(door)
    obstacles = obstacle_elements(elements)
    for space in elements:
        if space.ifc_type != "IfcSpace" or not space.extra.get("isCorridorLike"):
            continue
        space.issue_regions = [region for region in space.issue_regions if region.get("rule_id") != "corridor_width"]
        doors = doors_by_space.get(space.guid, [])
        area = _space_walkable_area(space, doors, obstacles, footprints)
        if area is None or area.is_empty:
            continue
        region = _space_clearance_region(space, area, doors, footprints)
        if region is None:
            continue
        space.extra["derivedClearSpaceWidthM"] = region["measured"]
        space.issue_regions.append(region)


def _space_clearance_region(
    space: Element,
    area,
    doors: list[Element],
    footprints: dict[str, object],
) -> dict | None:
    from shapely import union_all
    from shapely.geometry import LineString, Point, mapping

    door_zones = []
    for door in doors:
        polygon = _element_polygon(door, footprints)
        if polygon is not None and not polygon.is_empty and polygon.distance(area) <= 0.50:
            door_zones.append(polygon.buffer(0.35, cap_style=2, join_style=2))
    door_zone = union_all(door_zones, grid_size=0.001) if door_zones else None
    min_x, min_y, max_x, max_y = area.bounds
    span = math.hypot(max_x - min_x, max_y - min_y) + 2.0
    samples = []
    for start, end in _space_skeleton_segments(area):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 0.03:
            continue
        nx = -dy / length
        ny = dx / length
        count = max(1, math.ceil(length / 0.22))
        for index in range(count):
            value = (index + 0.5) / count
            x = start[0] + dx * value
            y = start[1] + dy * value
            point = Point(x, y)
            if door_zone is not None and door_zone.covers(point):
                continue
            cross = LineString([(x - nx * span, y - ny * span), (x + nx * span, y + ny * span)]).intersection(area)
            parts = [part for part in _line_parts(cross) if part.distance(point) <= 0.03]
            if not parts:
                continue
            line = max(parts, key=lambda part: part.length)
            if line.length >= RULE_LIMITS.corridor_width_m:
                continue
            samples.append(
                {
                    "width": line.length,
                    "point": point,
                    "line": line,
                    "shape": line.buffer(0.14, cap_style=2).intersection(area),
                }
            )
    if not samples:
        return None

    merged = union_all([sample["shape"] for sample in samples], grid_size=0.001).buffer(0.04).buffer(-0.04).intersection(area)
    polygons = []
    selected = []
    area_samples = []
    for polygon in _polygon_parts(merged):
        min_x, min_y, max_x, max_y = polygon.bounds
        if max(max_x - min_x, max_y - min_y) < RULE_LIMITS.turning_space_m:
            continue
        values = [sample for sample in samples if polygon.buffer(0.03).covers(sample["point"])]
        if len(values) < 2:
            continue
        polygons.append(polygon)
        selected.extend(values)
        area_samples.append(values)
    if not polygons:
        return None

    geometry = union_all(polygons, grid_size=0.001).buffer(0)
    worst = min(selected, key=lambda sample: sample["width"])
    anchor = max(polygons, key=lambda polygon: polygon.area).representative_point()
    line = list(worst["line"].coords)
    z = space.center[2] if space.center else 0.0
    region_key = hashlib.sha1(f"corridor_width:{space.guid}".encode("utf-8")).hexdigest()[:11].upper()
    areas = []
    for polygon, values in zip(polygons, area_samples):
        area_worst = min(values, key=lambda sample: sample["width"])
        area_anchor = polygon.representative_point()
        area_line = list(area_worst["line"].coords)
        bounds_key = ":".join(f"{value:.3f}" for value in polygon.bounds)
        area_key = hashlib.sha1(f"{space.guid}:{bounds_key}".encode("utf-8")).hexdigest()[:9].upper()
        areas.append(
            {
                "area_id": f"A{area_key}",
                "measured": round(area_worst["width"], 4),
                "geometry": mapping(polygon),
                "anchor": [round(area_anchor.x, 4), round(area_anchor.y, 4), round(z, 4)],
                "measurement_line": [
                    [round(area_line[0][0], 4), round(area_line[0][1], 4), round(z, 4)],
                    [round(area_line[-1][0], 4), round(area_line[-1][1], 4), round(z, 4)],
                ],
            }
        )
    return {
        "region_id": f"R{region_key}",
        "rule_id": "corridor_width",
        "element_guid": space.guid,
        "measured": round(worst["width"], 4),
        "required": RULE_LIMITS.corridor_width_m,
        "unit": "m",
        "geometry": mapping(geometry),
        "anchor": [round(anchor.x, 4), round(anchor.y, 4), round(z, 4)],
        "measurement_line": [
            [round(line[0][0], 4), round(line[0][1], 4), round(z, 4)],
            [round(line[-1][0], 4), round(line[-1][1], 4), round(z, 4)],
        ],
        "area_count": len(polygons),
        "areas": areas,
    }


def _space_skeleton_segments(area) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    from shapely import STRtree, union_all, voronoi_polygons
    from shapely.geometry import LineString, MultiPoint

    samples = _space_boundary_samples(area)
    if len(samples) < 4:
        return []
    sample_points = [item[0] for item in samples]
    safe = area.buffer(-CORRIDOR_SKELETON_CLEARANCE)
    if safe.is_empty:
        safe = area.buffer(-PLAN_ROUTE_HALF_WIDTH)
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


def _set_route_clear_width(
    measurements: dict,
    path: list[tuple[float, float, float]],
    area,
    route_doors: list[Element],
    footprints: dict[str, object],
) -> None:
    for key in ["routeClearWidthM", "routeClearWidthPointX", "routeClearWidthPointY", "routeClearWidthPointZ"]:
        measurements.pop(key, None)
    value = _route_clear_width_measurement(path, area, route_doors, footprints)
    if value is None:
        return
    width, point = value
    measurements["routeClearWidthM"] = width
    measurements["routeClearWidthPointX"] = round(point[0], 4)
    measurements["routeClearWidthPointY"] = round(point[1], 4)
    measurements["routeClearWidthPointZ"] = round(point[2], 4)


def _route_clear_width_measurement(
    path: list[tuple[float, float, float]],
    area,
    route_doors: list[Element],
    footprints: dict[str, object],
) -> tuple[float, tuple[float, float, float]] | None:
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    door_zones = []
    for door in route_doors:
        polygon = _element_polygon(door, footprints)
        if polygon is not None and not polygon.is_empty:
            door_zones.append(polygon.buffer(0.35, cap_style=2, join_style=2))
        elif door.center:
            door_zones.append(Point(door.center[0], door.center[1]).buffer(0.45))
    door_zone = unary_union(door_zones) if door_zones else None
    min_x, min_y, max_x, max_y = area.bounds
    span = math.hypot(max_x - min_x, max_y - min_y) + 2.0
    widths = []
    for start, end in zip(path, path[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 0.03:
            continue
        nx = -dy / length
        ny = dx / length
        count = max(1, math.ceil(length / 0.35))
        for index in range(count):
            value = (index + 0.5) / count
            x = start[0] + dx * value
            y = start[1] + dy * value
            z = start[2] + (end[2] - start[2]) * value
            point = Point(x, y)
            if door_zone is not None and door_zone.covers(point):
                continue
            cross = LineString([(x - nx * span, y - ny * span), (x + nx * span, y + ny * span)]).intersection(area)
            values = [part.length for part in _line_parts(cross) if part.distance(point) <= 0.03]
            if values:
                widths.append((max(values), (x, y, z)))
    if not widths:
        return None
    width, point = min(widths, key=lambda value: value[0])
    return round(width, 4), point


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


def _area_grid(area) -> dict | None:
    from shapely.geometry import Point
    from shapely.prepared import prep

    safe = area.buffer(-PLAN_ROUTE_HALF_WIDTH)
    if safe.is_empty:
        safe = area
    min_x, min_y, max_x, max_y = safe.bounds
    step = PLAN_GRID_STEP
    origin_x = math.floor(min_x / step) * step
    origin_y = math.floor(min_y / step) * step
    nx = math.ceil((max_x - origin_x) / step) + 1
    ny = math.ceil((max_y - origin_y) / step) + 1
    prepared = prep(safe)
    allowed = set()
    clearance = {}
    for ix in range(nx):
        x = origin_x + ix * step
        for iy in range(ny):
            y = origin_y + iy * step
            point = Point(x, y)
            if prepared.covers(point):
                cell = ix, iy
                allowed.add(cell)
                clearance[cell] = point.distance(area.boundary)
    if not allowed:
        return None
    width = max_x - min_x
    depth = max_y - min_y
    target = min(0.75, max(0.35, min(width, depth) / 4))
    return {
        "step": step,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "allowed": allowed,
        "clearance": clearance,
        "target": target,
    }


def _path_in_area(first: Element, second: Element, space: Element, area, grid: dict):
    z = _route_z(first, second, space)
    start = first.center[0], first.center[1], z
    end = second.center[0], second.center[1], z
    start_cell = _nearest_cell(grid, start)
    end_cell = _nearest_cell(grid, end)
    if start_cell is None or end_cell is None:
        return None
    cells = _astar(grid, start_cell, end_cell)
    if not cells:
        return None
    points = [_cell_point(grid, cell, z) for cell in cells]
    start_path = _endpoint_path(start, points[0], area)
    end_path = _endpoint_path(end, points[-1], area)
    if not start_path or not end_path:
        return None
    path = _compact_path(start_path + points[1:-1] + list(reversed(end_path)))
    return path if _path_inside_area(path, area) else None


def _astar(grid: dict, start: tuple[int, int], end: tuple[int, int]):
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    start_state = start, -1
    queue = [(_grid_heuristic(start, end, grid["step"]), 0.0, start, -1)]
    costs = {start_state: 0.0}
    parents = {}
    final = None
    while queue:
        _score, cost, cell, direction = heapq.heappop(queue)
        state = cell, direction
        if cost != costs.get(state):
            continue
        if cell == end:
            final = state
            break
        for next_direction, (dx, dy) in enumerate(directions):
            neighbour = cell[0] + dx, cell[1] + dy
            if neighbour not in grid["allowed"]:
                continue
            turn_cost = 0.22 if direction >= 0 and direction != next_direction else 0.0
            shortfall = max(0.0, grid["target"] - grid["clearance"].get(neighbour, 0.0)) / grid["target"]
            next_cost = cost + grid["step"] * (1.0 + shortfall * shortfall * 2.4) + turn_cost
            next_state = neighbour, next_direction
            if next_cost >= costs.get(next_state, math.inf):
                continue
            costs[next_state] = next_cost
            parents[next_state] = state
            score = next_cost + _grid_heuristic(neighbour, end, grid["step"])
            heapq.heappush(queue, (score, next_cost, neighbour, next_direction))
    if final is None:
        return None
    cells = []
    state = final
    while True:
        cells.append(state[0])
        if state == start_state:
            break
        state = parents[state]
    return list(reversed(cells))


def _nearest_cell(grid: dict, point) -> tuple[int, int] | None:
    cell = (
        round((point[0] - grid["origin_x"]) / grid["step"]),
        round((point[1] - grid["origin_y"]) / grid["step"]),
    )
    if cell in grid["allowed"]:
        return cell
    candidates = sorted(
        grid["allowed"],
        key=lambda value: (value[0] - cell[0]) ** 2 + (value[1] - cell[1]) ** 2,
    )
    return candidates[0] if candidates and math.dist(candidates[0], cell) * grid["step"] <= 1.5 else None


def _endpoint_path(endpoint, grid_point, area):
    candidates = [
        [endpoint, (grid_point[0], endpoint[1], endpoint[2]), grid_point],
        [endpoint, (endpoint[0], grid_point[1], endpoint[2]), grid_point],
        [endpoint, grid_point],
    ]
    valid = [_compact_path(candidate) for candidate in candidates if _path_inside_area(candidate, area)]
    return min(valid, key=_path_length) if valid else None


def _path_inside_area(path, area) -> bool:
    from shapely.geometry import LineString

    if not path or len(path) < 2:
        return False
    line = LineString([(point[0], point[1]) for point in path])
    outside = line.buffer(PLAN_ROUTE_HALF_WIDTH, cap_style=2, join_style=2).difference(area.buffer(0.02))
    return outside.is_empty or outside.area <= 0.002


def _compact_path(path):
    result = []
    for point in path:
        value = tuple(float(item) for item in point)
        if result and distance(result[-1], value) <= 1e-8:
            continue
        if len(result) >= 2:
            first = result[-2]
            second = result[-1]
            cross = (second[0] - first[0]) * (value[1] - second[1]) - (second[1] - first[1]) * (value[0] - second[0])
            if abs(cross) <= 1e-8:
                result[-1] = value
                continue
        result.append(value)
    return result


def _select_forest(edges: list[RouteEdge], pass_only: bool) -> list[RouteEdge]:
    parent = {}

    def find(value):
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    selected = []
    candidates = [edge for edge in edges if not pass_only or edge.status == "pass"]
    candidates.sort(key=lambda edge: (edge.status != "pass", edge.distance_m, edge.edge_id))
    for edge in candidates:
        first = find(edge.start_guid)
        second = find(edge.end_guid)
        if first == second:
            continue
        parent[second] = first
        selected.append(edge)
    return selected


def _issue_witnesses(edges: list[RouteEdge], elements: list[Element]) -> list[RouteEdge]:
    by_guid = {element.guid: element for element in elements}
    selected = {}
    for edge in edges:
        for key in _edge_issue_keys(edge, by_guid):
            current = selected.get(key)
            if current is None or edge.distance_m < current.distance_m:
                selected[key] = edge
    return list({edge.edge_id: edge for edge in selected.values()}.values())


def _edge_issue_keys(edge: RouteEdge, elements: dict[str, Element]) -> list[tuple[str, str]]:
    keys = []
    for reason in edge.reasons:
        if reason == "door_width":
            narrow = []
            for guid in (edge.start_guid, edge.end_guid):
                width = _number(elements.get(guid).extra.get("derivedDoorWidthM")) if elements.get(guid) else None
                if width is not None and width < RULE_LIMITS.route_door_width_m:
                    narrow.append((reason, guid))
            keys.extend(narrow or [(reason, edge.edge_id)])
        elif edge.via_space_guid:
            keys.append((reason, edge.via_space_guid))
        else:
            keys.append((reason, edge.edge_id))
    return keys


def _stair_markers(route_edges: list[RouteEdge]) -> list[RouteEdge]:
    result = []
    for edge in route_edges:
        if "stair_block" not in edge.reasons:
            continue
        measurements = dict(edge.measurements)
        measurements["planNetworkRole"] = "issue"
        measurements["planMarkerOnly"] = True
        result.append(
            replace(
                edge,
                edge_id=_plan_edge_id(edge.edge_id, edge.start_guid, "stair"),
                path=_compact_path(edge.path),
                measurements=measurements,
            )
        )
    return result


def _unreachable_markers(elements: list[Element], connected: set[str]) -> list[RouteEdge]:
    result = []
    for door in elements:
        if not _route_door(door) or door.guid in connected:
            continue
        x, y, z = door.center
        path = [(x - 0.03, y, z), (x + 0.03, y, z)]
        result.append(
            RouteEdge(
                edge_id=_plan_edge_id(door.guid, door.guid, "unreachable"),
                start_guid=door.guid,
                end_guid=door.guid,
                distance_m=0.0,
                status="fail",
                reasons=["unreachable"],
                path=path,
                source="2D route connectivity",
                measurements={"planNetworkRole": "issue", "planMarkerOnly": True},
            )
        )
    return result


def _measurement_reasons(measurements: dict) -> list[str]:
    reasons = []
    width = _number(measurements.get("routeDoorWidthMinM"))
    clear = _number(measurements.get("routeClearWidthM"))
    turn = _number(measurements.get("routeTurningSpaceM"))
    slope = _number(measurements.get("routeRampSlopePercent"))
    ramp_width = _number(measurements.get("routeRampUsableWidthM"))
    if width is not None and width < RULE_LIMITS.route_door_width_m:
        reasons.append("door_width")
    if clear is not None and clear < RULE_LIMITS.corridor_width_m:
        reasons.append("route_width")
    if measurements.get("routeHasTurn") and turn is not None and turn < RULE_LIMITS.turning_space_m:
        reasons.append("turning_space")
    if measurements.get("routeHitsStair"):
        reasons.append("stair_block")
    if slope is not None and slope > RULE_LIMITS.ramp_slope_percent:
        reasons.append("ramp_slope")
    if ramp_width is not None and ramp_width < RULE_LIMITS.ramp_width_m:
        reasons.append("ramp_width")
    return reasons


def _route_door(element: Element | None) -> bool:
    return bool(
        element
        and element.ifc_type == "IfcDoor"
        and element.center
        and not element.extra.get("isExcludedRouteDoor")
    )


def _floor_refs(elements: list[Element]) -> list[tuple[str, float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    source = [element for element in elements if element.ifc_type == "IfcDoor" and element.storey and element.center]
    if not source:
        source = [element for element in elements if element.storey and element.center]
    for element in source:
        grouped[element.storey].append(float(element.center[2]))
    if grouped:
        return [(name, sum(values) / len(values)) for name, values in grouped.items()]
    centers = sorted(float(element.center[2]) for element in elements if element.ifc_type == "IfcDoor" and element.center)
    refs = []
    for z in centers:
        if not refs or abs(refs[-1][1] - z) > 1.8:
            refs.append((f"z={z:.2f}", z))
        else:
            name, current = refs[-1]
            refs[-1] = name, (current + z) / 2
    return refs


def _floor_name(element: Element, floor_refs: list[tuple[str, float]]) -> str | None:
    if element.storey:
        return element.storey
    if not element.center:
        return None
    z = float(element.center[2])
    if floor_refs:
        name, ref_z = min(floor_refs, key=lambda item: abs(item[1] - z))
        if abs(ref_z - z) <= 1.8:
            return name
    return f"z={z:.2f}"


def _z_overlap(first: Element, second: Element) -> bool:
    if first.bbox_min and first.bbox_max and second.bbox_min and second.bbox_max:
        return first.bbox_min[2] <= second.bbox_max[2] and first.bbox_max[2] >= second.bbox_min[2]
    return bool(first.center and second.center and abs(first.center[2] - second.center[2]) <= 2.2)


def _route_z(first: Element, second: Element, space: Element) -> float:
    bottoms = [element.bbox_min[2] for element in (first, second) if element.bbox_min]
    if bottoms:
        return max(bottoms) + 0.05
    if space.bbox_min:
        return space.bbox_min[2] + 0.05
    return min(first.center[2], second.center[2])


def _cell_point(grid: dict, cell: tuple[int, int], z: float) -> tuple[float, float, float]:
    return (
        grid["origin_x"] + cell[0] * grid["step"],
        grid["origin_y"] + cell[1] * grid["step"],
        z,
    )


def _grid_heuristic(first: tuple[int, int], second: tuple[int, int], step: float) -> float:
    return (abs(first[0] - second[0]) + abs(first[1] - second[1])) * step


def _path_length(path) -> float:
    return sum(distance(first, second) for first, second in zip(path, path[1:]))


def _pair_key(first: str, second: str, space_guid: str) -> tuple[str, str, str]:
    low, high = sorted((first, second))
    return low, high, space_guid


def _plan_edge_id(first: str, second: str, context: str) -> str:
    value = "|".join(sorted((first, second)) + [context])
    return "P" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:11].upper()


def _number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
