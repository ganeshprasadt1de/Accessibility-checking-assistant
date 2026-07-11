from __future__ import annotations

import hashlib
import heapq
import math
import pickle
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF, RDFS
from rdflib.namespace import XSD

from .config import NS, RULE_LIMITS
from .geometry import distance, intersects_box, obstacle_elements
from .ifc_tools import element_uri
from .model import Element, RouteEdge

ACC = Namespace(NS["acc"])
SPINE_ROUTE_DOOR_LIMIT = 4
ROUTE_Z_TOLERANCE = 0.20
ROUTE_GRID_STEP = 0.55
ROUTE_NAVIGATION_HALF = 0.05
NON_TRANSIT_ROUTE_COST = 4.0
SPINE_ROUTE_COST = 0.30
SKELETON_CLEARANCE = 0.12


def build_route_edges(ifc_path: Path, elements: list[Element]) -> list[RouteEdge]:
    edges = _walkable_route_edges(ifc_path, elements)
    if not edges:
        edges = _space_boundary_route_edges(ifc_path, elements)
    return _add_unreachable_door_edges(edges, elements)


def _walkable_route_edges(ifc_path: Path, elements: list[Element]) -> list[RouteEdge]:
    import ifcopenshell
    from shapely.ops import unary_union

    model = ifcopenshell.open(str(ifc_path))
    spaces_by_door = _door_to_spaces(model)
    footprints = _element_footprints(model, elements)
    door_normals = _door_normals(model, elements)
    floor_refs = _route_floor_refs(elements)
    _add_geometric_door_spaces(spaces_by_door, elements, footprints, floor_refs)
    _filter_door_spaces(spaces_by_door, elements, footprints, door_normals)
    floors: dict[str, dict[str, list[Element]]] = {}
    for element in elements:
        floor_name = _route_floor_name(element, floor_refs)
        if not floor_name:
            continue
        floor = floors.setdefault(floor_name, defaultdict(list))
        floor[element.ifc_type].append(element)

    edges: list[RouteEdge] = []
    for floor_name, groups in floors.items():
        spaces = [space for space in groups.get("IfcSpace", []) if not space.extra.get("isExcludedSpace")]
        doors = [
            door
            for door in groups.get("IfcDoor", [])
            if door.guid in spaces_by_door and not door.extra.get("isExcludedRouteDoor") and door.center
        ]
        if not spaces:
            continue
        space_areas = _space_walkable_areas(spaces, doors, groups, footprints, spaces_by_door)
        if not space_areas:
            continue
        _set_space_clearance_regions(spaces, doors, footprints, space_areas)
        if len(doors) < 2:
            continue
        walkable = unary_union(list(space_areas.values())).buffer(0)
        if not walkable or walkable.is_empty:
            continue
        floor_edges = _walkable_floor_edges(
            floor_name,
            spaces,
            doors,
            groups,
            footprints,
            spaces_by_door,
            space_areas,
            walkable,
            door_normals,
        )
        edges.extend(floor_edges)
    return edges


def _element_footprints(model, elements: list[Element]) -> dict[str, object]:
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    wanted = {element.guid for element in elements if element.ifc_type in {"IfcSpace", "IfcDoor", "IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"}}
    footprints = {}
    for ifc_type in ["IfcSpace", "IfcDoor", "IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"]:
        for obj in model.by_type(ifc_type):
            guid = getattr(obj, "GlobalId", None)
            if guid not in wanted:
                continue
            try:
                shape = ifcopenshell.geom.create_shape(settings, obj)
            except Exception:
                continue
            footprint = _shape_footprint(shape)
            if footprint and not footprint.is_empty:
                footprints[guid] = footprint
    return footprints


def _shape_footprint(shape):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    verts = getattr(shape.geometry, "verts", None)
    faces = getattr(shape.geometry, "faces", None)
    if not verts or not faces:
        return None
    polygons = []
    for index in range(0, len(faces), 3):
        points = []
        for vertex_index in faces[index : index + 3]:
            offset = vertex_index * 3
            points.append((verts[offset], verts[offset + 1]))
        polygon = Polygon(points)
        if polygon.is_valid and polygon.area > 1e-6:
            polygons.append(polygon)
    if not polygons:
        return None
    return unary_union(polygons).buffer(0)


def _space_walkable_areas(
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
) -> dict[str, object]:
    from shapely.ops import unary_union

    blockers = []
    for ifc_type in ["IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"]:
        for element in groups.get(ifc_type, []):
            polygon = _element_polygon(element, footprints)
            if polygon and not polygon.is_empty:
                blockers.append(polygon)
    blocked = unary_union(blockers).buffer(0) if blockers else None
    doors_by_guid = {door.guid: door for door in doors}
    door_guids_by_space: dict[str, list[str]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        if door_guid not in doors_by_guid:
            continue
        for space_guid in space_guids:
            door_guids_by_space[space_guid].append(door_guid)

    result = {}
    for space in spaces:
        polygon = _element_polygon(space, footprints)
        if not polygon or polygon.is_empty:
            continue
        area = polygon.buffer(0)
        if blocked and not blocked.is_empty:
            area = area.difference(blocked).buffer(0)
        openings = []
        for door_guid in door_guids_by_space.get(space.guid, []):
            door_polygon = _element_polygon(doors_by_guid[door_guid], footprints)
            if door_polygon and not door_polygon.is_empty:
                openings.append(door_polygon.buffer(0.22, cap_style=2, join_style=2))
        if openings:
            opening_area = unary_union(openings).intersection(polygon.buffer(0.75))
            area = unary_union([area, opening_area]).buffer(0)
        if not area.is_empty:
            result[space.guid] = area
    return result


def _set_space_clearance_regions(
    spaces: list[Element],
    doors: list[Element],
    footprints: dict[str, object],
    space_areas: dict[str, object],
) -> None:
    for space in spaces:
        if not space.extra.get("isCorridorLike"):
            continue
        space.issue_regions = [region for region in space.issue_regions if region.get("rule_id") != "corridor_width"]
        area = space_areas.get(space.guid)
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


def _floor_walkable_area(
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]] | None = None,
):
    from shapely.ops import unary_union

    if spaces_by_door is not None:
        areas = _space_walkable_areas(spaces, doors, groups, footprints, spaces_by_door)
        return unary_union(list(areas.values())).buffer(0) if areas else None

    space_polygons = [_element_polygon(space, footprints) for space in spaces]
    space_polygons = [polygon for polygon in space_polygons if polygon and not polygon.is_empty]
    if not space_polygons:
        return None
    space_area = unary_union(space_polygons).buffer(0)
    blockers = []
    for ifc_type in ["IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"]:
        for element in groups.get(ifc_type, []):
            polygon = _element_polygon(element, footprints)
            if polygon and not polygon.is_empty:
                blockers.append(polygon)
    blocked = unary_union(blockers).buffer(0) if blockers else None
    openings = []
    for door in doors:
        polygon = _element_polygon(door, footprints)
        if polygon and not polygon.is_empty:
            openings.append(polygon.buffer(0.18, cap_style=2, join_style=2))
    opening_area = unary_union(openings).buffer(0) if openings else None
    walkable = space_area.difference(blocked) if blocked and not blocked.is_empty else space_area
    if opening_area and not opening_area.is_empty:
        walkable = walkable.union(opening_area.intersection(space_area.buffer(0.8))).buffer(0)
    return walkable.buffer(0)


def _element_polygon(element: Element, footprints: dict[str, object]):
    from shapely.geometry import box

    polygon = footprints.get(element.guid)
    if polygon and not polygon.is_empty:
        return polygon
    if not element.bbox_min or not element.bbox_max:
        return None
    return box(element.bbox_min[0], element.bbox_min[1], element.bbox_max[0], element.bbox_max[1])


def _door_normals(model, elements: list[Element]) -> dict[str, tuple[float, float]]:
    from ifcopenshell.util.placement import get_local_placement

    result = {}
    for element in elements:
        if element.ifc_type != "IfcDoor":
            continue
        nx = _num(element.extra.get("doorDepthAxisX"))
        ny = _num(element.extra.get("doorDepthAxisY"))
        if nx is None or ny is None:
            continue
        length = math.hypot(nx, ny)
        if length > 0.5:
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


def _door_normal(
    door: Element,
    footprints: dict[str, object],
    door_normals: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    normal = door_normals.get(door.guid)
    if normal:
        return normal
    polygon = _element_polygon(door, footprints)
    if polygon is None or polygon.is_empty:
        return None
    min_x, min_y, max_x, max_y = polygon.bounds
    width = max_x - min_x
    depth = max_y - min_y
    if min(width, depth) <= 0 or max(width, depth) < min(width, depth) * 1.5:
        return None
    return (1.0, 0.0) if width < depth else (0.0, 1.0)


def _door_space_target(point, polygon, normal: tuple[float, float]):
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    candidates = []
    for sign in [-1, 1]:
        target = (
            point.x + normal[0] * sign * 1.5,
            point.y + normal[1] * sign * 1.5,
        )
        intersection = LineString([(point.x, point.y), target]).intersection(polygon.buffer(0.02))
        if intersection.is_empty:
            continue
        candidate = nearest_points(point, intersection)[1]
        candidates.append((point.distance(candidate), candidate.x, candidate.y))
    if not candidates:
        return None
    _distance, x, y = min(candidates)
    return x, y


def _filter_door_spaces(
    spaces_by_door: dict[str, list[str]],
    elements: list[Element],
    footprints: dict[str, object],
    door_normals: dict[str, tuple[float, float]],
) -> None:
    from shapely.geometry import Point

    by_guid = {element.guid: element for element in elements}
    for door_guid, space_guids in list(spaces_by_door.items()):
        door = by_guid.get(door_guid)
        if door is None or not door.center:
            continue
        normal = _door_normal(door, footprints, door_normals)
        if normal is None:
            continue
        point = Point(door.center[0], door.center[1])
        accepted = []
        for space_guid in space_guids:
            space = by_guid.get(space_guid)
            if space is None:
                accepted.append(space_guid)
                continue
            polygon = _element_polygon(space, footprints)
            if polygon is None or polygon.is_empty or _door_space_target(point, polygon, normal) is not None:
                accepted.append(space_guid)
        spaces_by_door[door_guid] = accepted


def _add_geometric_door_spaces(
    spaces_by_door: dict[str, list[str]],
    elements: list[Element],
    footprints: dict[str, object],
    floor_refs: list[tuple[float, str]],
) -> None:
    spaces = [
        element
        for element in elements
        if element.ifc_type == "IfcSpace" and not element.extra.get("isExcludedSpace") and element.bbox_min and element.bbox_max
    ]
    for door in elements:
        if door.ifc_type != "IfcDoor" or spaces_by_door.get(door.guid) or door.extra.get("isExcludedRouteDoor") or not door.center:
            continue
        door_polygon = _element_polygon(door, footprints)
        if door_polygon is None or door_polygon.is_empty:
            continue
        door_floor = _route_floor_name(door, floor_refs)
        if not door_floor:
            continue
        candidates = []
        for space in spaces:
            if _route_floor_name(space, floor_refs) != door_floor:
                continue
            space_polygon = _element_polygon(space, footprints)
            if space_polygon is None or space_polygon.is_empty:
                continue
            gap = door_polygon.distance(space_polygon)
            if gap <= 0.35:
                candidates.append((gap, space.guid))
        if not candidates:
            continue
        candidates.sort()
        best = candidates[0][0]
        spaces_by_door[door.guid] = [guid for gap, guid in candidates if gap <= best + 0.20][:2]
        door.extra["routeSpaceInference"] = "same-storey geometry proximity"


def _walkable_floor_edges(
    floor_name: str,
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
    space_areas: dict[str, object],
    walkable,
    door_normals: dict[str, tuple[float, float]],
) -> list[RouteEdge]:
    graph, points, door_nodes, edge_costs, edge_spaces, edge_kinds = _space_navigation_graph(
        spaces,
        doors,
        groups,
        footprints,
        spaces_by_door,
        space_areas,
        door_normals,
    )
    terminals = [node for node in door_nodes.values() if graph.get(node)]
    if len(terminals) < 2:
        return []
    physical_tree = _terminal_route_tree(graph, edge_costs, terminals)
    rule_graph = _rule_navigation_graph(
        points,
        edge_costs,
        edge_spaces,
        edge_kinds,
        doors,
        groups,
        space_areas,
    )
    rule_tree = _terminal_route_tree(rule_graph, edge_costs, terminals)
    if not physical_tree and not rule_tree:
        return []
    rule_edges = _route_edges_from_tree(
        floor_name,
        rule_tree,
        rule_graph,
        points,
        door_nodes,
        edge_spaces,
        edge_kinds,
        spaces,
        doors,
        groups,
        footprints,
        space_areas,
        walkable,
        enforce_width=True,
    ) if rule_tree else []
    physical_edges = _route_edges_from_tree(
        floor_name,
        physical_tree,
        graph,
        points,
        door_nodes,
        edge_spaces,
        edge_kinds,
        spaces,
        doors,
        groups,
        footprints,
        space_areas,
        walkable,
    ) if physical_tree else []
    result = _route_network_forest(rule_edges, physical_edges, set(door_nodes))
    for edge in result:
        edge.measurements["routeNetworkRole"] = "candidate" if _route_measurement_reasons(edge) else "accessible"
    return result


def _space_navigation_graph(
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
    space_areas: dict[str, object],
    door_normals: dict[str, tuple[float, float]],
):
    from shapely.geometry import Point

    graph: dict[tuple, dict[tuple, float]] = defaultdict(dict)
    points = {}
    edge_costs = {}
    edge_spaces = {}
    edge_kinds = {}
    floor_z = _average_z([door.center[2] for door in doors if door.center])
    door_nodes = {}
    for door in doors:
        node = ("d", door.guid)
        door_nodes[door.guid] = node
        points[node] = (door.center[0], door.center[1], floor_z)

    by_guid = {door.guid: door for door in doors}
    doors_by_space: dict[str, list[Element]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        door = by_guid.get(door_guid)
        if not door:
            continue
        for space_guid in space_guids:
            doors_by_space[space_guid].append(door)

    for space in spaces:
        area = space_areas.get(space.guid)
        space_doors = doors_by_space.get(space.guid, [])
        if not area or area.is_empty or len(space_doors) < 2:
            continue
        transit = bool(space.extra.get("isCorridorLike")) or len(space_doors) >= SPINE_ROUTE_DOOR_LIMIT
        cost_factor = 1.0 if transit else NON_TRANSIT_ROUTE_COST
        skeleton = _space_skeleton_geometry(area) if transit else None
        grid_nodes, clearance = _space_grid_nodes(
            graph,
            points,
            edge_costs,
            edge_spaces,
            edge_kinds,
            space,
            area,
            floor_z,
            cost_factor,
            skeleton,
        )
        local_nodes = _add_space_door_edges(
            graph,
            points,
            edge_costs,
            edge_spaces,
            edge_kinds,
            space,
            space_doors,
            area,
            footprints,
            door_nodes,
            grid_nodes,
            clearance,
            cost_factor,
            floor_z,
            skeleton,
            door_normals,
        )
        if transit and (skeleton is None or skeleton.is_empty):
            _add_space_spine_edges(
                graph,
                points,
                edge_costs,
                edge_spaces,
                edge_kinds,
                space,
                space_doors,
                area,
                local_nodes,
                grid_nodes,
                clearance,
                floor_z,
            )
        if not transit or not grid_nodes:
            for first, second in combinations(space_doors, 2):
                first_node = local_nodes.get(first.guid)
                second_node = local_nodes.get(second.guid)
                if first_node is None or second_node is None:
                    continue
                if not _walkable_segment_ok(points[first_node], points[second_node], area, ROUTE_NAVIGATION_HALF):
                    continue
                value = distance(points[first_node], points[second_node]) * cost_factor
                _add_navigation_edge(
                    graph,
                    edge_costs,
                    edge_spaces,
                    edge_kinds,
                    first_node,
                    second_node,
                    value,
                    space.guid,
                    "grid",
                )
    return graph, points, door_nodes, edge_costs, edge_spaces, edge_kinds


def _space_grid_nodes(
    graph: dict,
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    space: Element,
    area,
    z: float,
    cost_factor: float,
    skeleton=None,
):
    from shapely.geometry import Point

    min_x, min_y, max_x, max_y = area.bounds
    x_count = max(1, math.ceil((max_x - min_x) / ROUTE_GRID_STEP))
    y_count = max(1, math.ceil((max_y - min_y) / ROUTE_GRID_STEP))
    grid = {}
    for ix in range(x_count + 1):
        x = min_x + ix * ROUTE_GRID_STEP
        for iy in range(y_count + 1):
            y = min_y + iy * ROUTE_GRID_STEP
            point = Point(x, y)
            if not area.covers(point) or point.distance(area.boundary) + 0.005 < ROUTE_NAVIGATION_HALF:
                continue
            node = ("g", space.guid, ix, iy)
            grid[(ix, iy)] = node
            points[node] = (x, y, z)
    clearance = {node: Point(points[node][0], points[node][1]).distance(area.boundary) for node in grid.values()}
    skeleton_nodes = {
        node
        for node in grid.values()
        if skeleton is not None and Point(points[node][0], points[node][1]).distance(skeleton) <= ROUTE_GRID_STEP * 0.8
    }
    target_half = RULE_LIMITS.clearance_width_m / 2
    offsets = [(1, 0), (0, 1)]
    for (ix, iy), node in grid.items():
        for dx, dy in offsets:
            other = grid.get((ix + dx, iy + dy))
            if other is None or not _walkable_segment_ok(points[node], points[other], area, ROUTE_NAVIGATION_HALF):
                continue
            value = _walkable_edge_cost(
                points[node],
                points[other],
                clearance[node],
                clearance[other],
                target_half,
            ) * cost_factor
            kind = "grid"
            if node in skeleton_nodes and other in skeleton_nodes:
                value *= SPINE_ROUTE_COST
                kind = "skeleton"
            _add_navigation_edge(
                graph,
                edge_costs,
                edge_spaces,
                edge_kinds,
                node,
                other,
                value,
                space.guid,
                kind,
            )
    return list(grid.values()), clearance


def _add_space_door_edges(
    graph: dict,
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    space: Element,
    doors: list[Element],
    area,
    footprints: dict[str, object],
    door_nodes: dict[str, tuple],
    grid_nodes: list,
    clearance: dict,
    cost_factor: float,
    z: float,
    skeleton=None,
    door_normals: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple]:
    result = {}
    for door in doors:
        terminal = door_nodes[door.guid]
        anchor = _space_door_anchor(door, area, footprints, door_normals or {})
        if anchor is None:
            continue
        anchor_point = (anchor[0], anchor[1], z)
        local_node = terminal
        if distance(points[terminal], anchor_point) > 0.03:
            local_node = ("a", space.guid, door.guid)
            points[local_node] = anchor_point
            if _walkable_segment_ok(points[terminal], anchor_point, area, ROUTE_NAVIGATION_HALF):
                _add_navigation_edge(
                    graph,
                    edge_costs,
                    edge_spaces,
                    edge_kinds,
                    terminal,
                    local_node,
                    distance(points[terminal], anchor_point) * cost_factor,
                    space.guid,
                    "door",
                )
            else:
                continue
        result[door.guid] = local_node
        if skeleton is not None and _connect_skeleton_door_node(
            graph,
            points,
            edge_costs,
            edge_spaces,
            edge_kinds,
            local_node,
            door,
            grid_nodes,
            area,
            space.guid,
            cost_factor,
            skeleton,
        ):
            continue
        _connect_navigation_node(
            graph,
            points,
            edge_costs,
            edge_spaces,
            edge_kinds,
            local_node,
            grid_nodes,
            clearance,
            area,
            space.guid,
            cost_factor,
            "door",
            ROUTE_GRID_STEP * 6,
            8,
        )
    return result


def _space_door_anchor(
    door: Element,
    area,
    footprints: dict[str, object],
    door_normals: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points

    point = Point(door.center[0], door.center[1])
    normal = _door_normal(door, footprints, door_normals)
    if normal:
        line = LineString(
            [
                (point.x - normal[0] * 1.5, point.y - normal[1] * 1.5),
                (point.x + normal[0] * 1.5, point.y + normal[1] * 1.5),
            ]
        )
        opening = line.buffer(0.05, cap_style=2).intersection(area)
        if not opening.is_empty:
            target = nearest_points(point, opening)[1]
            if point.distance(target) <= 1.5:
                return target.x, target.y
        return None
    if area.buffer(0.02).covers(point):
        return point.x, point.y
    door_polygon = _element_polygon(door, footprints)
    if door_polygon and not door_polygon.is_empty:
        opening = door_polygon.buffer(0.22, cap_style=2, join_style=2).intersection(area)
        if not opening.is_empty:
            target = nearest_points(point, opening)[1]
            if point.distance(target) <= 1.5:
                return target.x, target.y
    target = nearest_points(point, area)[1]
    if point.distance(target) <= 1.5:
        return target.x, target.y
    return None


def _connect_skeleton_door_node(
    graph: dict,
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    node,
    door: Element,
    grid_nodes: list,
    area,
    space_guid: str,
    cost_factor: float,
    skeleton,
) -> bool:
    from shapely.geometry import Point

    source = points[node]
    skeleton_nodes = [
        other
        for other in grid_nodes
        if Point(points[other][0], points[other][1]).distance(skeleton) <= ROUTE_GRID_STEP * 0.8
    ]
    candidates = []
    for other in sorted(skeleton_nodes, key=lambda value: distance(source, points[value]))[:96]:
        if distance(source, points[other]) > ROUTE_GRID_STEP * 10:
            continue
        for preference, path in _door_connector_paths(source, points[other], door):
            if not _walkable_path_ok(path, area, ROUTE_NAVIGATION_HALF):
                continue
            cost = sum(_walkable_segment_cost(first, second, area) for first, second in zip(path, path[1:])) * cost_factor
            candidates.append((cost + preference, cost, str(other), other, path))
    if not candidates:
        return False
    _score, _cost, _key, target, path = min(candidates)
    path_nodes = [node]
    for index, point in enumerate(path[1:-1], start=1):
        connector = "c", space_guid, door.guid, index, round(point[0], 3), round(point[1], 3)
        points[connector] = point
        path_nodes.append(connector)
    path_nodes.append(target)
    for first, second in zip(path_nodes, path_nodes[1:]):
        _add_navigation_edge(
            graph,
            edge_costs,
            edge_spaces,
            edge_kinds,
            first,
            second,
            _walkable_segment_cost(points[first], points[second], area) * cost_factor,
            space_guid,
            "door",
        )
    return True


def _door_connector_paths(start: tuple[float, float, float], end: tuple[float, float, float], door: Element) -> list[tuple[float, list]]:
    z = (start[2] + end[2]) / 2
    width = abs(door.bbox_max[0] - door.bbox_min[0]) if door.bbox_min and door.bbox_max else 0
    depth = abs(door.bbox_max[1] - door.bbox_min[1]) if door.bbox_min and door.bbox_max else 0
    first = (start[0], end[1], z) if width >= depth else (end[0], start[1], z)
    second = (end[0], start[1], z) if width >= depth else (start[0], end[1], z)
    result = []
    if abs(start[0] - end[0]) <= 0.03 or abs(start[1] - end[1]) <= 0.03:
        result.append((0.0, _dedupe_route_points([start, end])))
    result.append((0.0, _dedupe_route_points([start, first, end])))
    result.append((0.2, _dedupe_route_points([start, second, end])))
    return result


def _space_skeleton_geometry(area):
    from shapely import union_all
    from shapely.geometry import LineString

    segments = _space_skeleton_segments(area)
    if not segments:
        return None
    return union_all([LineString(segment) for segment in segments], grid_size=0.001)


def _space_skeleton_segments(area) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    from shapely import STRtree, union_all, voronoi_polygons
    from shapely.geometry import LineString, MultiPoint

    samples = _space_boundary_samples(area)
    if len(samples) < 4:
        return []
    sample_points = [item[0] for item in samples]
    safe = area.buffer(-SKELETON_CLEARANCE)
    if safe.is_empty:
        safe = area.buffer(-ROUTE_NAVIGATION_HALF)
    if safe.is_empty:
        return []
    try:
        tree = STRtree(sample_points)
        edges = voronoi_polygons(MultiPoint(sample_points), extend_to=area.envelope, only_edges=True)
    except Exception:
        return []
    tolerance = max(0.03, ROUTE_GRID_STEP * 0.08)
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
            first_point = (float(first[0]), float(first[1]))
            second_point = (float(second[0]), float(second[1]))
            key = tuple(sorted((tuple(round(value, 3) for value in first_point), tuple(round(value, 3) for value in second_point))))
            result[key] = (first_point, second_point)
    return list(result.values())


def _space_boundary_samples(area) -> list[tuple[object, int, int, int]]:
    from shapely.geometry import LineString

    result = []
    polygons = [area] if area.geom_type == "Polygon" else list(area.geoms)
    ring_index = 0
    for polygon in polygons:
        for ring in [polygon.exterior, *polygon.interiors]:
            line = LineString(ring.coords)
            count = max(4, math.ceil(line.length / ROUTE_GRID_STEP))
            for index in range(count):
                result.append((line.interpolate(index / count, normalized=True), ring_index, index, count))
            ring_index += 1
    return result


def _add_space_spine_edges(
    graph: dict,
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    space: Element,
    doors: list[Element],
    area,
    local_nodes: dict[str, tuple],
    grid_nodes: list,
    clearance: dict,
    z: float,
) -> None:
    if not space.bbox_min or not space.bbox_max:
        return
    axis, lane, _margin = _space_spine_axis(space)
    items = []
    for door in doors:
        local_node = local_nodes.get(door.guid)
        if local_node is None:
            continue
        point = points[local_node]
        coord = point[0] if axis == "x" else point[1]
        items.append({"door": door, "node": local_node, "point": point, "coord": coord})
    grouped = []
    for item in sorted(items, key=lambda value: (value["coord"], value["door"].guid)):
        if not grouped or abs(item["coord"] - grouped[-1]["coord"]) > 0.75:
            grouped.append({"coord": item["coord"], "items": [item]})
        else:
            grouped[-1]["items"].append(item)
            grouped[-1]["coord"] = sum(value["coord"] for value in grouped[-1]["items"]) / len(grouped[-1]["items"])

    spine_nodes = []
    for index, group in enumerate(grouped, start=1):
        point = (group["coord"], lane, z) if axis == "x" else (lane, group["coord"], z)
        if not _walkable_point_ok(point, area, ROUTE_NAVIGATION_HALF):
            continue
        node = ("s", space.guid, index)
        points[node] = point
        spine_nodes.append(node)
        _connect_navigation_node(
            graph,
            points,
            edge_costs,
            edge_spaces,
            edge_kinds,
            node,
            grid_nodes,
            clearance,
            area,
            space.guid,
            1.0,
            "grid",
            ROUTE_GRID_STEP * 4,
            6,
        )
        for item in group["items"]:
            source = item["node"]
            source_point = points[source]
            projection = (source_point[0], lane, z) if axis == "x" else (lane, source_point[1], z)
            projection_node = ("p", space.guid, item["door"].guid)
            if not _walkable_segment_ok(source_point, projection, area, ROUTE_NAVIGATION_HALF):
                continue
            points[projection_node] = projection
            _add_navigation_edge(
                graph,
                edge_costs,
                edge_spaces,
                edge_kinds,
                source,
                projection_node,
                distance(source_point, projection) * SPINE_ROUTE_COST,
                space.guid,
                "door-spine",
            )
            if _walkable_segment_ok(projection, point, area, ROUTE_NAVIGATION_HALF):
                _add_navigation_edge(
                    graph,
                    edge_costs,
                    edge_spaces,
                    edge_kinds,
                    projection_node,
                    node,
                    distance(projection, point) * SPINE_ROUTE_COST,
                    space.guid,
                    "spine",
                )
    for first, second in zip(spine_nodes, spine_nodes[1:]):
        if not _walkable_segment_ok(points[first], points[second], area, ROUTE_NAVIGATION_HALF):
            continue
        _add_navigation_edge(
            graph,
            edge_costs,
            edge_spaces,
            edge_kinds,
            first,
            second,
            distance(points[first], points[second]) * SPINE_ROUTE_COST,
            space.guid,
            "spine",
        )


def _connect_navigation_node(
    graph: dict,
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    node,
    grid_nodes: list,
    clearance: dict,
    area,
    space_guid: str,
    cost_factor: float,
    kind: str,
    max_distance: float,
    max_count: int,
) -> None:
    target_half = RULE_LIMITS.clearance_width_m / 2
    candidates = []
    for other in sorted(grid_nodes, key=lambda value: distance(points[node], points[value]))[:64]:
        length = distance(points[node], points[other])
        if length > max_distance or not _walkable_segment_ok(points[node], points[other], area, ROUTE_NAVIGATION_HALF):
            continue
        value = _walkable_edge_cost(
            points[node],
            points[other],
            target_half,
            clearance.get(other, target_half),
            target_half,
        ) * cost_factor
        candidates.append((value, str(other), other))
    for value, _key, other in sorted(candidates)[:max_count]:
        _add_navigation_edge(
            graph,
            edge_costs,
            edge_spaces,
            edge_kinds,
            node,
            other,
            value,
            space_guid,
            kind,
        )


def _add_navigation_edge(
    graph: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    first,
    second,
    cost: float,
    space_guid: str,
    kind: str,
) -> None:
    if first == second:
        return
    key = _graph_edge_key(first, second)
    if cost >= edge_costs.get(key, float("inf")):
        return
    graph[first][second] = cost
    graph[second][first] = cost
    edge_costs[key] = cost
    edge_spaces[key] = space_guid
    edge_kinds[key] = kind


def _graph_edge_key(first, second) -> tuple:
    return (first, second) if str(first) <= str(second) else (second, first)


def _rule_navigation_graph(
    points: dict,
    edge_costs: dict,
    edge_spaces: dict,
    edge_kinds: dict,
    doors: list[Element],
    groups: dict[str, list[Element]],
    space_areas: dict[str, object],
) -> dict:
    graph: dict[tuple, dict[tuple, float]] = defaultdict(dict)
    approaches: dict[tuple, set] = defaultdict(set)
    doors_by_guid = {door.guid: door for door in doors}
    ramps = groups.get("IfcRamp", []) + groups.get("IfcRampFlight", [])
    for key, cost in edge_costs.items():
        first, second = key
        route_doors = [doors_by_guid.get(node[1]) for node in [first, second] if node[0] == "d"]
        route_doors = [door for door in route_doors if door is not None]
        if any((_num(door.extra.get("derivedDoorWidthM")) or 0) < RULE_LIMITS.route_door_width_m for door in route_doors):
            continue
        path = [points[first], points[second]]
        ramp = _ramp_measurements(ramps, None, path)
        if ramp.get("routeRampSlopePercent") is not None and ramp["routeRampSlopePercent"] > RULE_LIMITS.ramp_slope_percent:
            continue
        if ramp.get("routeRampUsableWidthM") is not None and ramp["routeRampUsableWidthM"] < RULE_LIMITS.ramp_width_m:
            continue
        approaches[first].add(second)
        approaches[second].add(first)
        if edge_kinds.get(key) in {"door", "door-spine"}:
            continue
        space_guid = edge_spaces.get(key)
        area = space_areas.get(space_guid)
        if area is None:
            continue
        width = _route_clear_width_measurement(path, area, [], {})
        if width is not None and width[0] < RULE_LIMITS.corridor_width_m:
            continue
        graph[first][second] = cost
        graph[second][first] = cost
    _add_rule_door_approaches(graph, approaches, points, edge_costs, doors_by_guid)
    return graph


def _add_rule_door_approaches(
    graph: dict,
    approaches: dict,
    points: dict,
    edge_costs: dict,
    doors_by_guid: dict[str, Element],
) -> None:
    components = _graph_components(graph)
    limit = RULE_LIMITS.movement_depth_m + ROUTE_GRID_STEP
    for guid, door in doors_by_guid.items():
        if (_num(door.extra.get("derivedDoorWidthM")) or 0) < RULE_LIMITS.route_door_width_m:
            continue
        terminal = ("d", guid)
        if terminal not in approaches:
            continue
        queue = [(0.0, str(terminal), terminal)]
        best = {terminal: 0.0}
        previous = {}
        targets = {}
        while queue:
            value, _key, node = heapq.heappop(queue)
            if value != best.get(node):
                continue
            if node != terminal and node in components:
                targets.setdefault(components[node], node)
                continue
            for other in approaches.get(node, set()):
                if other[0] == "d" and other != terminal:
                    continue
                next_value = value + distance(points[node], points[other])
                if next_value > limit or next_value >= best.get(other, float("inf")):
                    continue
                best[other] = next_value
                previous[other] = node
                heapq.heappush(queue, (next_value, str(other), other))
        for target in targets.values():
            node = target
            while node != terminal:
                other = previous[node]
                key = _graph_edge_key(node, other)
                cost = edge_costs[key]
                graph[node][other] = cost
                graph[other][node] = cost
                node = other


def _graph_components(graph: dict) -> dict:
    components = {}
    component = 0
    for start in graph:
        if start in components:
            continue
        component += 1
        queue = deque([start])
        components[start] = component
        while queue:
            node = queue.popleft()
            for other in graph.get(node, {}):
                if other in components:
                    continue
                components[other] = component
                queue.append(other)
    return components


def _route_network_forest(rule_edges: list[RouteEdge], physical_edges: list[RouteEdge], door_guids: set[str]) -> list[RouteEdge]:
    ordered = []
    seen = set()
    for source_priority, edges in enumerate([rule_edges, physical_edges]):
        for edge in edges:
            if edge.edge_id in seen:
                continue
            seen.add(edge.edge_id)
            blocked = bool(_route_measurement_reasons(edge))
            ordered.append((blocked, source_priority, edge.distance_m, len(edge.path), edge.edge_id, edge))

    parent = {}

    def find(node):
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(first, second):
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return False
        parent[second_root] = first_root
        return True

    selected = []
    for _blocked, _source, _distance, _points, _edge_id, edge in sorted(ordered):
        if edge.start_guid == edge.end_guid or not union(edge.start_guid, edge.end_guid):
            continue
        selected.append(edge)

    selected = _prune_route_forest(selected, door_guids)
    graph: dict[str, dict[str, float]] = defaultdict(dict)
    for edge in selected:
        graph[edge.start_guid][edge.end_guid] = edge.distance_m
        graph[edge.end_guid][edge.start_guid] = edge.distance_m

    components = _graph_components(graph)
    door_counts: dict[object, int] = defaultdict(int)
    for guid in door_guids:
        component = components.get(guid)
        if component is not None:
            door_counts[component] += 1
    included = {component for component, count in door_counts.items() if count >= 2}
    return [edge for edge in selected if components.get(edge.start_guid) in included]


def _prune_route_forest(edges: list[RouteEdge], door_guids: set[str]) -> list[RouteEdge]:
    incident: dict[str, set[int]] = defaultdict(set)
    for index, edge in enumerate(edges):
        incident[edge.start_guid].add(index)
        incident[edge.end_guid].add(index)
    active = set(range(len(edges)))
    queue = deque(node for node, edge_ids in incident.items() if node not in door_guids and len(edge_ids) <= 1)
    while queue:
        node = queue.popleft()
        edge_id = next((index for index in incident.get(node, set()) if index in active), None)
        if edge_id is None:
            continue
        active.remove(edge_id)
        edge = edges[edge_id]
        for endpoint in [edge.start_guid, edge.end_guid]:
            incident[endpoint].discard(edge_id)
            if endpoint not in door_guids and len(incident[endpoint]) <= 1:
                queue.append(endpoint)
    return [edge for index, edge in enumerate(edges) if index in active]


def _route_measurement_reasons(edge: RouteEdge) -> list[str]:
    values = edge.measurements
    reasons = []
    if values.get("routeDoorWidthMinM") is not None and values["routeDoorWidthMinM"] < RULE_LIMITS.route_door_width_m:
        reasons.append("door_width")
    if values.get("routeClearWidthM") is not None and values["routeClearWidthM"] < RULE_LIMITS.corridor_width_m:
        reasons.append("route_width")
    if values.get("routeHasTurn") and values.get("routeTurningSpaceM") is not None and values["routeTurningSpaceM"] < RULE_LIMITS.turning_space_m:
        reasons.append("turning_space")
    if values.get("routeHitsWall"):
        reasons.append("wall_block")
    if values.get("routeHitsStair"):
        reasons.append("stair_block")
    if values.get("routeRampSlopePercent") is not None and values["routeRampSlopePercent"] > RULE_LIMITS.ramp_slope_percent:
        reasons.append("ramp_slope")
    if values.get("routeRampUsableWidthM") is not None and values["routeRampUsableWidthM"] < RULE_LIMITS.ramp_width_m:
        reasons.append("ramp_width")
    return reasons


def _terminal_route_tree(graph: dict, edge_costs: dict, terminals: list) -> set[tuple]:
    shortest = {}
    pairs = []
    ordered = sorted(set(terminals), key=str)
    for index, start in enumerate(ordered):
        dist, prev = _navigation_shortest_paths(graph, start)
        shortest[start] = prev
        for end in ordered[index + 1 :]:
            if end in dist:
                pairs.append((dist[end], str(start), str(end), start, end))

    parent = {node: node for node in ordered}

    def find(node):
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(first, second):
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return False
        parent[second_root] = first_root
        return True

    selected = set()
    for _value, _first_key, _second_key, first, second in sorted(pairs):
        if not union(first, second):
            continue
        path = _reconstruct_navigation_path(shortest[first], first, second)
        selected.update(_graph_edge_key(a, b) for a, b in zip(path, path[1:]))

    parent = {}

    def graph_find(node):
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = graph_find(parent[node])
        return parent[node]

    tree = set()
    for key in sorted(selected, key=lambda value: (edge_costs[value], str(value))):
        first, second = key
        first_root = graph_find(first)
        second_root = graph_find(second)
        if first_root == second_root:
            continue
        parent[second_root] = first_root
        tree.add(key)
    return _prune_route_tree(tree, set(ordered))


def _navigation_shortest_paths(graph: dict, start):
    queue = [(0.0, str(start), start)]
    best = {start: 0.0}
    prev = {}
    while queue:
        value, _key, node = heapq.heappop(queue)
        if value != best.get(node):
            continue
        for other, cost in graph.get(node, {}).items():
            next_value = value + cost
            if next_value >= best.get(other, float("inf")):
                continue
            best[other] = next_value
            prev[other] = node
            heapq.heappush(queue, (next_value, str(other), other))
    return best, prev


def _reconstruct_navigation_path(prev: dict, start, end) -> list:
    path = [end]
    while path[-1] != start:
        node = prev.get(path[-1])
        if node is None:
            return []
        path.append(node)
    return list(reversed(path))


def _prune_route_tree(edges: set[tuple], terminals: set) -> set[tuple]:
    adjacency: dict[tuple, set] = defaultdict(set)
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    queue = deque(node for node, values in adjacency.items() if len(values) <= 1 and node not in terminals)
    while queue:
        node = queue.popleft()
        if node in terminals or len(adjacency[node]) != 1:
            continue
        other = next(iter(adjacency[node]))
        adjacency[node].clear()
        adjacency[other].discard(node)
        if other not in terminals and len(adjacency[other]) <= 1:
            queue.append(other)
    return {
        _graph_edge_key(first, second)
        for first, values in adjacency.items()
        for second in values
        if str(first) < str(second)
    }


def _route_tree_chains(edges: set[tuple], terminals: set, edge_spaces: dict) -> list[dict]:
    adjacency: dict[tuple, set] = defaultdict(set)
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    critical = set()
    for node, neighbors in adjacency.items():
        spaces = {edge_spaces.get(_graph_edge_key(node, other)) for other in neighbors}
        if len(neighbors) != 2 or node in terminals or len(spaces) > 1:
            critical.add(node)
    seen = set()
    result = []
    for start in sorted(critical, key=str):
        for neighbor in sorted(adjacency[start], key=str):
            key = _graph_edge_key(start, neighbor)
            if key in seen:
                continue
            seen.add(key)
            nodes = [start, neighbor]
            keys = [key]
            previous = start
            current = neighbor
            while current not in critical:
                next_node = next(node for node in adjacency[current] if node != previous)
                next_key = _graph_edge_key(current, next_node)
                seen.add(next_key)
                nodes.append(next_node)
                keys.append(next_key)
                previous, current = current, next_node
            result.append({"nodes": nodes, "edges": keys})
    return result


def _route_edges_from_tree(
    floor_name: str,
    tree: set[tuple],
    graph: dict,
    points: dict,
    door_nodes: dict[str, tuple],
    edge_spaces: dict,
    edge_kinds: dict,
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    space_areas: dict[str, object],
    walkable,
    enforce_width: bool = False,
) -> list[RouteEdge]:
    terminals = set(door_nodes.values())
    door_by_node = {node: guid for guid, node in door_nodes.items()}
    door_by_guid = {door.guid: door for door in doors}
    space_by_guid = {space.guid: space for space in spaces}
    obstacles = [
        element
        for ifc_type in ["IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"]
        for element in groups.get(ifc_type, [])
        if element.bbox_min and element.bbox_max
    ]
    result = []
    for chain in _route_tree_chains(tree, terminals, edge_spaces):
        nodes = chain["nodes"]
        keys = chain["edges"]
        spaces_for_chain = {edge_spaces.get(key) for key in keys if edge_spaces.get(key)}
        space_guid = next(iter(spaces_for_chain)) if len(spaces_for_chain) == 1 else None
        area = space_areas.get(space_guid, walkable)
        raw_path = _dedupe_route_points([points[node] for node in nodes])
        path = _simplify_navigation_path(nodes, keys, points, edge_kinds, area)
        if len(path) < 2 or not _walkable_path_ok(path, area, ROUTE_NAVIGATION_HALF):
            path = raw_path
        if len(path) < 2 or not _walkable_path_ok(path, area, ROUTE_NAVIGATION_HALF):
            continue
        route_doors = [door_by_guid[guid] for guid in [door_by_node.get(nodes[0]), door_by_node.get(nodes[-1])] if guid in door_by_guid]
        if enforce_width:
            width = _route_clear_width_measurement(path, area, route_doors, footprints)
            if width is not None and width[0] < RULE_LIMITS.corridor_width_m:
                path = raw_path
        start_guid = door_by_node.get(nodes[0]) or _network_node_guid(floor_name, nodes[0], path[0])
        end_guid = door_by_node.get(nodes[-1]) or _network_node_guid(floor_name, nodes[-1], path[-1])
        if start_guid == end_guid:
            continue
        edge = _route_edge(
            _route_edge_id("walkable-net", floor_name, start_guid, end_guid, str(path)),
            start_guid,
            end_guid,
            path,
            route_doors,
            obstacles,
            space_by_guid.get(space_guid),
            "IFC walkable area pathfinding",
        )
        _set_walkable_route_measurements(edge, path, area, route_doors, footprints, groups)
        result.append(edge)
    return _geometric_route_edges(
        floor_name,
        result,
        spaces,
        doors,
        groups,
        footprints,
        space_areas,
        walkable,
    )


def _geometric_route_edges(
    floor_name: str,
    edges: list[RouteEdge],
    spaces: list[Element],
    doors: list[Element],
    groups: dict[str, list[Element]],
    footprints: dict[str, object],
    space_areas: dict[str, object],
    walkable,
) -> list[RouteEdge]:
    from shapely import union_all
    from shapely.geometry import LineString, Point

    grouped: dict[str | None, list[RouteEdge]] = defaultdict(list)
    for edge in edges:
        if len(edge.path) >= 2:
            grouped[edge.via_space_guid].append(edge)
    door_by_guid = {door.guid: door for door in doors}
    terminal_points: dict[str | None, list[tuple[str, Point]]] = defaultdict(list)
    for space_guid, space_edges in grouped.items():
        for edge in space_edges:
            if edge.start_guid in door_by_guid:
                terminal_points[space_guid].append((edge.start_guid, Point(edge.path[0][0], edge.path[0][1])))
            if edge.end_guid in door_by_guid:
                terminal_points[space_guid].append((edge.end_guid, Point(edge.path[-1][0], edge.path[-1][1])))

    points = {}
    segment_costs = {}
    segment_spaces = {}
    terminal_nodes = {}
    floor_z = _average_z([point[2] for edge in edges for point in edge.path])

    def geometry_node(space_guid, coordinate):
        point = Point(coordinate[0], coordinate[1])
        for door_guid, terminal in terminal_points.get(space_guid, []):
            if point.distance(terminal) <= 0.025:
                node = ("d", door_guid)
                terminal_nodes[door_guid] = node
                door = door_by_guid[door_guid]
                points.setdefault(node, (door.center[0], door.center[1], floor_z))
                return node
        node = ("n", space_guid or "floor", round(coordinate[0], 3), round(coordinate[1], 3))
        points[node] = (coordinate[0], coordinate[1], floor_z)
        return node

    for space_guid, space_edges in grouped.items():
        area = space_areas.get(space_guid, walkable)
        lines = [LineString([(point[0], point[1]) for point in edge.path]) for edge in space_edges]
        geometry = union_all(lines, grid_size=0.001)
        for part in _line_parts(geometry):
            coordinates = list(part.coords)
            for first_coordinate, second_coordinate in zip(coordinates, coordinates[1:]):
                first = geometry_node(space_guid, first_coordinate)
                second = geometry_node(space_guid, second_coordinate)
                if first == second:
                    continue
                key = _graph_edge_key(first, second)
                value = _walkable_segment_cost(first_coordinate, second_coordinate, area)
                if value >= segment_costs.get(key, float("inf")):
                    continue
                segment_costs[key] = value
                segment_spaces[key] = space_guid

    parent = {}

    def find(node):
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    tree = set()
    for key in sorted(segment_costs, key=lambda value: (segment_costs[value], str(value))):
        first, second = key
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            continue
        parent[second_root] = first_root
        tree.add(key)
    tree = _prune_route_tree(tree, set(terminal_nodes.values()))

    door_by_node = {node: guid for guid, node in terminal_nodes.items()}
    space_by_guid = {space.guid: space for space in spaces}
    obstacles = [
        element
        for ifc_type in ["IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"]
        for element in groups.get(ifc_type, [])
        if element.bbox_min and element.bbox_max
    ]
    result = []
    for chain in _route_tree_chains(tree, set(terminal_nodes.values()), segment_spaces):
        nodes = chain["nodes"]
        keys = chain["edges"]
        spaces_for_chain = {segment_spaces.get(key) for key in keys if segment_spaces.get(key)}
        space_guid = next(iter(spaces_for_chain)) if len(spaces_for_chain) == 1 else None
        area = space_areas.get(space_guid, walkable)
        path = _compress_walkable_path([points[node] for node in nodes])
        if len(path) < 2 or not _walkable_path_ok(path, area, ROUTE_NAVIGATION_HALF - 0.01):
            continue
        start_guid = door_by_node.get(nodes[0]) or _network_node_guid(floor_name, nodes[0], path[0])
        end_guid = door_by_node.get(nodes[-1]) or _network_node_guid(floor_name, nodes[-1], path[-1])
        if start_guid == end_guid:
            continue
        route_doors = [door_by_guid[guid] for guid in [start_guid, end_guid] if guid in door_by_guid]
        edge = _route_edge(
            _route_edge_id("walkable-geometry", floor_name, start_guid, end_guid, str(path)),
            start_guid,
            end_guid,
            path,
            route_doors,
            obstacles,
            space_by_guid.get(space_guid),
            "IFC walkable area pathfinding",
        )
        _set_walkable_route_measurements(edge, path, area, route_doors, footprints, groups)
        result.append(edge)
    return result


def _simplify_navigation_path(nodes: list, keys: list[tuple], points: dict, edge_kinds: dict, area) -> list[tuple[float, float, float]]:
    if len(nodes) <= 2:
        return _dedupe_route_points([points[node] for node in nodes])
    kinds = [edge_kinds.get(key, "grid") for key in keys]
    breaks = [0]
    for index in range(1, len(nodes) - 1):
        if kinds[index - 1] != kinds[index]:
            breaks.append(index)
    breaks.append(len(nodes) - 1)
    result = []
    for first, last in zip(breaks, breaks[1:]):
        path = [points[node] for node in nodes[first : last + 1]]
        kind = kinds[first] if first < len(kinds) else "grid"
        if kind == "grid":
            path = _smooth_walkable_path(path, area, ROUTE_NAVIGATION_HALF)
        elif kind == "skeleton":
            path = _smooth_rectilinear_path(path, area, ROUTE_NAVIGATION_HALF)
        path = _compress_walkable_path(path)
        if result and path and distance(result[-1], path[0]) <= 0.03:
            result.extend(path[1:])
        else:
            result.extend(path)
    result = _dedupe_route_points(result)
    result = _remove_short_route_bends(result, area)
    return result if _walkable_path_ok(result, area, ROUTE_NAVIGATION_HALF) else [points[node] for node in nodes]


def _remove_short_route_bends(path: list[tuple[float, float, float]], area) -> list[tuple[float, float, float]]:
    result = list(path)
    changed = True
    while changed and len(result) > 2:
        changed = False
        for index in range(1, len(result) - 1):
            if min(distance(result[index - 1], result[index]), distance(result[index], result[index + 1])) > 0.22:
                continue
            if not _walkable_segment_ok(result[index - 1], result[index + 1], area, ROUTE_NAVIGATION_HALF):
                continue
            result.pop(index)
            changed = True
            break
    return _dedupe_route_points(result)


def _dedupe_route_points(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    if len(points) <= 1:
        return list(points)
    result = [points[0]]
    for point in points[1:-1]:
        if distance(result[-1], point) > 0.03:
            result.append(point)
    last = points[-1]
    if len(result) == 1 or distance(result[-1], last) > 0.03:
        result.append(last)
    else:
        result[-1] = last
    return result


def _set_walkable_route_measurements(
    edge: RouteEdge,
    path: list[tuple[float, float, float]],
    area,
    route_doors: list[Element],
    footprints: dict[str, object],
    groups: dict[str, list[Element]],
) -> None:
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    for key in [
        "routeClearWidthM",
        "routeClearWidthPointX",
        "routeClearWidthPointY",
        "routeClearWidthPointZ",
        "routeTurningSpaceM",
        "routeTurningPointX",
        "routeTurningPointY",
        "routeTurningPointZ",
    ]:
        edge.measurements.pop(key, None)
    required_path = _route_required_turn_path(path, area)
    turn_points = _route_turn_points(required_path)
    edge.measurements["routeHasTurn"] = bool(turn_points)
    edge.measurements["routeRequiredTurnCount"] = len(turn_points)
    clear_width = _route_clear_width_measurement(path, area, route_doors, footprints)
    if clear_width is not None:
        value, point = clear_width
        edge.measurements["routeClearWidthM"] = value
        _set_route_measurement_point(edge.measurements, "routeClearWidthPoint", point)
    turning_space = _route_turning_space_measurement(required_path, area)
    if turning_space is not None:
        value, point = turning_space
        edge.measurements["routeTurningSpaceM"] = value
        _set_route_measurement_point(edge.measurements, "routeTurningPoint", point)
    door_areas = []
    for door in route_doors:
        polygon = _element_polygon(door, footprints)
        if polygon and not polygon.is_empty:
            door_areas.append(polygon.buffer(0.22, cap_style=2, join_style=2))
    route_area = unary_union([area, *door_areas]).buffer(0) if door_areas else area
    line = LineString([(point[0], point[1]) for point in path])
    edge.measurements["routeHitsWall"] = not route_area.buffer(0.03).covers(line)
    stair_polygons = []
    for ifc_type in ["IfcStair", "IfcStairFlight"]:
        for stair in groups.get(ifc_type, []):
            polygon = _element_polygon(stair, footprints)
            if polygon and not polygon.is_empty:
                stair_polygons.append(polygon)
    edge.measurements["routeHitsStair"] = any(line.buffer(0.01).intersects(polygon) for polygon in stair_polygons)


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
        if polygon and not polygon.is_empty:
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
            if door_zone and door_zone.covers(point):
                continue
            cross = LineString([(x - nx * span, y - ny * span), (x + nx * span, y + ny * span)]).intersection(area)
            values = [part.length for part in _line_parts(cross) if part.distance(point) <= 0.03]
            if values:
                widths.append((max(values), (x, y, z)))
    if not widths:
        return None
    width, point = min(widths, key=lambda value: value[0])
    return round(width, 4), point


def _route_turning_space_measurement(
    path: list[tuple[float, float, float]],
    area,
) -> tuple[float, tuple[float, float, float]] | None:
    from shapely.geometry import Point
    from shapely.ops import polylabel

    values = []
    for middle in _route_turn_points(path):
        point = Point(middle[0], middle[1])
        local = area.intersection(point.buffer(1.20))
        polygons = [value for value in _polygon_parts(local) if not value.is_empty]
        polygons = [value for value in polygons if value.buffer(0.03).covers(point)]
        if not polygons:
            continue
        polygon = max(polygons, key=lambda value: value.area)
        center = polylabel(polygon, tolerance=0.02)
        values.append((center.distance(polygon.boundary) * 2, middle))
    if not values:
        return None
    width, point = min(values, key=lambda value: value[0])
    return round(width, 4), point


def _route_required_turn_path(path: list[tuple[float, float, float]], area) -> list[tuple[float, float, float]]:
    points = _dedupe_route_points(path)
    if len(points) <= 2:
        return points
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        while next_index > index + 1 and not _walkable_segment_ok(points[index], points[next_index], area, ROUTE_NAVIGATION_HALF):
            next_index -= 1
        result.append(points[next_index])
        index = next_index
    return _dedupe_route_points(result)


def _set_route_measurement_point(
    measurements: dict[str, float | str | bool | None],
    key: str,
    point: tuple[float, float, float],
) -> None:
    measurements[f"{key}X"] = round(point[0], 4)
    measurements[f"{key}Y"] = round(point[1], 4)
    measurements[f"{key}Z"] = round(point[2], 4)


def _route_turn_points(path: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    result = []
    for first, middle, last in zip(path, path[1:], path[2:]):
        a = (middle[0] - first[0], middle[1] - first[1])
        b = (last[0] - middle[0], last[1] - middle[1])
        first_length = math.hypot(*a)
        second_length = math.hypot(*b)
        scale = first_length * second_length
        if min(first_length, second_length) < 0.35 or scale <= 1e-6:
            continue
        if abs(a[0] * b[1] - a[1] * b[0]) / scale > 0.15:
            result.append(middle)
    return result


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


def _network_node_guid(floor_name: str, node, point: tuple[float, float, float]) -> str:
    value = f"{floor_name}|{node}|{point[0]:.3f}|{point[1]:.3f}|{point[2]:.3f}".encode("utf-8")
    return "route-node:" + hashlib.sha1(value).hexdigest()[:12]


def _walkable_edge_cost(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    start_clearance: float,
    end_clearance: float,
    half_width: float,
) -> float:
    length = distance(start, end)
    clearance = max((start_clearance + end_clearance) / 2, 0.05)
    return length * (1 + (half_width / clearance) ** 2.0)


def _walkable_segment_cost(start, end, area) -> float:
    from shapely.geometry import LineString

    line = LineString([(start[0], start[1]), (end[0], end[1])])
    if line.length <= 0.001:
        return 0.0
    count = max(1, math.ceil(line.length / ROUTE_GRID_STEP))
    clearances = [line.interpolate((index + 0.5) / count, normalized=True).distance(area.boundary) for index in range(count)]
    clearance = min(clearances) if clearances else ROUTE_NAVIGATION_HALF
    target_half = RULE_LIMITS.clearance_width_m / 2
    return line.length * (1 + (target_half / max(clearance, ROUTE_NAVIGATION_HALF)) ** 2.0)


def _walkable_point_ok(point: tuple[float, float, float], area, half_width: float) -> bool:
    from shapely.geometry import Point

    swept = Point(point[0], point[1]).buffer(half_width)
    return area.covers(swept) or swept.difference(area).area <= 0.001


def _walkable_path_ok(path: list[tuple[float, float, float]], walkable, half_width: float) -> bool:
    return len(path) >= 2 and all(_walkable_segment_ok(start, end, walkable, half_width) for start, end in zip(path, path[1:]))


def _walkable_segment_ok(start: tuple[float, float, float], end: tuple[float, float, float], walkable, half_width: float) -> bool:
    from shapely.geometry import LineString

    line = LineString([(start[0], start[1]), (end[0], end[1])])
    if line.length <= 0.001:
        return _walkable_point_ok(start, walkable, half_width)
    swept = line.buffer(half_width, cap_style=2, join_style=2)
    return walkable.covers(swept) or swept.difference(walkable).area <= 0.003


def _smooth_walkable_path(points: list[tuple[float, float, float]], walkable, half_width: float) -> list[tuple[float, float, float]]:
    points = _dedupe_route_points(points)
    if len(points) <= 2:
        return points
    cumulative_costs = [0.0]
    for start, end in zip(points, points[1:]):
        cumulative_costs.append(cumulative_costs[-1] + _walkable_segment_cost(start, end, walkable))
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        while next_index > index + 1:
            direct_cost = _walkable_segment_cost(points[index], points[next_index], walkable)
            original_cost = cumulative_costs[next_index] - cumulative_costs[index]
            if _walkable_segment_ok(points[index], points[next_index], walkable, half_width) and direct_cost <= original_cost * 1.02:
                break
            next_index -= 1
        result.append(points[next_index])
        index = next_index
    return _dedupe_route_points(result)


def _smooth_rectilinear_path(points: list[tuple[float, float, float]], walkable, half_width: float) -> list[tuple[float, float, float]]:
    points = _dedupe_route_points(points)
    if len(points) <= 2:
        return points
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        replacement = None
        while next_index > index:
            replacement = _rectilinear_shortcut(points[index : next_index + 1], walkable, half_width)
            if replacement:
                break
            next_index -= 1
        if not replacement:
            replacement = [points[index], points[index + 1]]
            next_index = index + 1
        result.extend(replacement[1:])
        index = next_index
    return _dedupe_route_points(result)


def _rectilinear_shortcut(points: list[tuple[float, float, float]], walkable, half_width: float) -> list[tuple[float, float, float]] | None:
    if len(points) < 2:
        return None
    start = points[0]
    end = points[-1]
    z = (start[2] + end[2]) / 2
    candidates = []

    def add(values):
        path = _compress_walkable_path(_dedupe_route_points(values))
        if len(path) < 2 or not _walkable_path_ok(path, walkable, half_width):
            return
        cost = sum(_walkable_segment_cost(first, second, walkable) for first, second in zip(path, path[1:]))
        candidates.append((cost + max(0, len(path) - 2) * 0.35, cost, path))

    if abs(start[0] - end[0]) <= 0.03 or abs(start[1] - end[1]) <= 0.03:
        add([start, end])
    add([start, (end[0], start[1], z), end])
    add([start, (start[0], end[1], z), end])
    for y in sorted({round(point[1], 6) for point in points}):
        add([start, (start[0], y, z), (end[0], y, z), end])
    for x in sorted({round(point[0], 6) for point in points}):
        add([start, (x, start[1], z), (x, end[1], z), end])
    if not candidates:
        return None
    original_cost = sum(_walkable_segment_cost(first, second, walkable) for first, second in zip(points, points[1:]))
    valid = [candidate for candidate in candidates if candidate[1] <= original_cost * 1.05 + 0.01]
    return min(valid, key=lambda value: (value[0], len(value[2])))[2] if valid else None


def _compress_walkable_path(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    if len(points) <= 2:
        return points
    result = [points[0]]
    for index in range(1, len(points) - 1):
        prev = result[-1]
        current = points[index]
        nxt = points[index + 1]
        a = (current[0] - prev[0], current[1] - prev[1])
        b = (nxt[0] - current[0], nxt[1] - current[1])
        scale = max(math.hypot(*a) * math.hypot(*b), 1e-9)
        if abs(a[0] * b[1] - a[1] * b[0]) / scale > 0.01:
            result.append(current)
    result.append(points[-1])
    return _dedupe_route_points(result)


def _average_z(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _route_floor_refs(elements: list[Element]) -> list[tuple[str, float]]:
    grouped: dict[str, list[float]] = {}
    source = [element for element in elements if element.ifc_type == "IfcDoor" and element.storey and element.center]
    if not source:
        source = [element for element in elements if element.storey and element.center]
    for element in source:
        grouped.setdefault(element.storey, []).append(float(element.center[2]))
    if grouped:
        return [(name, sum(values) / len(values)) for name, values in grouped.items()]
    centers = sorted(float(element.center[2]) for element in elements if element.ifc_type == "IfcDoor" and element.center)
    refs: list[tuple[str, float]] = []
    for z in centers:
        if not refs or abs(refs[-1][1] - z) > 1.8:
            refs.append((f"z={z:.2f}", z))
        else:
            name, current = refs[-1]
            refs[-1] = (name, (current + z) / 2)
    return refs


def _route_floor_name(element: Element, floor_refs: list[tuple[str, float]]) -> str | None:
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
        if _use_spine_routes(space, unique_doors):
            spine_edges = _spine_route_edges(space, unique_doors, obstacles)
            if spine_edges:
                edges.extend(spine_edges)
                continue
        for door_a, door_b in combinations(unique_doors, 2):
            pair_key = tuple(sorted((door_a.guid, door_b.guid)) + [space_guid])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            path = _path_through_space(door_a, door_b, space)
            if _route_hits_wall(path, obstacles, [door_a, door_b]):
                continue
            edges.append(
                _route_edge(
                    _route_edge_id("space", space_guid, door_a.guid, door_b.guid),
                    door_a.guid,
                    door_b.guid,
                    path,
                    [door_a, door_b],
                    obstacles,
                    space,
                    "IFC space boundaries and floor geometry",
                )
            )
    return edges


def _use_spine_routes(space: Element, doors: list[Element]) -> bool:
    if len(doors) > SPINE_ROUTE_DOOR_LIMIT:
        return True
    if space.extra.get("isCorridorLike") and len(doors) > 2:
        return True
    if not space.bbox_min or not space.bbox_max or len(doors) < 3:
        return False
    width = abs(space.bbox_max[0] - space.bbox_min[0])
    depth = abs(space.bbox_max[1] - space.bbox_min[1])
    short = max(min(width, depth), 0.001)
    return max(width, depth) / short >= 2.8


def _spine_route_edges(space: Element, doors: list[Element], obstacles: list[Element]) -> list[RouteEdge]:
    groups = _spine_node_groups(space, doors, obstacles)
    if len(groups) < 2:
        return []
    edges: list[RouteEdge] = []
    for group in groups:
        node_guid = group["node_guid"]
        node_point = group["point"]
        for item in group["doors"]:
            path = _dedupe_points([item["door"].center, item["entry"], node_point])
            if _route_hits_wall(path, obstacles, [item["door"]]):
                continue
            edge_id = _route_edge_id("spine-door", space.guid, item["door"].guid, node_guid)
            edges.append(
                _route_edge(
                    edge_id,
                    item["door"].guid,
                    node_guid,
                    path,
                    [item["door"]],
                    obstacles,
                    space,
                    "IFC space boundary to corridor spine",
                )
            )
    for lane_key in sorted({group["lane_key"] for group in groups}):
        lane_groups = [group for group in groups if group["lane_key"] == lane_key]
        for first, second in zip(lane_groups, lane_groups[1:]):
            path = _dedupe_points([first["point"], second["point"]])
            if _route_hits_blocking_obstacle(path, obstacles, []):
                continue
            edge_id = _route_edge_id("spine-lane", space.guid, first["node_guid"], second["node_guid"])
            edges.append(
                _route_edge(
                    edge_id,
                    first["node_guid"],
                    second["node_guid"],
                    path,
                    [],
                    obstacles,
                    space,
                    "IFC corridor spine geometry",
                )
            )
    for coord_key in sorted({group["coord_key"] for group in groups}):
        coord_groups = [group for group in groups if group["coord_key"] == coord_key]
        for first, second in zip(coord_groups, coord_groups[1:]):
            path = _dedupe_points([first["point"], second["point"]])
            if _route_hits_blocking_obstacle(path, obstacles, []):
                continue
            edge_id = _route_edge_id("spine-connector", space.guid, first["node_guid"], second["node_guid"])
            edges.append(
                _route_edge(
                    edge_id,
                    first["node_guid"],
                    second["node_guid"],
                    path,
                    [],
                    obstacles,
                    space,
                    "IFC corridor spine connector",
                )
            )
    edges.extend(_spine_cross_lane_edges(space, groups, obstacles))
    return edges


def _spine_cross_lane_edges(space: Element, groups: list[dict], obstacles: list[Element]) -> list[RouteEdge]:
    axis, _base_lane, _margin = _space_spine_axis(space)
    lanes = []
    for lane_key in sorted({group["lane_key"] for group in groups}):
        lane_groups = [group for group in groups if group["lane_key"] == lane_key]
        lane_groups.sort(key=lambda group: _spine_group_coord(group, axis))
        lanes.append(lane_groups)
    edges: list[RouteEdge] = []
    for first_lane, second_lane in zip(lanes, lanes[1:]):
        candidates = []
        for first in first_lane:
            for second in second_lane:
                if first["coord_key"] == second["coord_key"]:
                    continue
                coord_gap = abs(_spine_group_coord(first, axis) - _spine_group_coord(second, axis))
                candidates.append((coord_gap, distance(first["point"], second["point"]), first, second))
        for _coord_gap, _length, first, second in sorted(candidates, key=lambda item: (item[0], item[1])):
            path = next((item for item in _spine_cross_lane_paths(first["point"], second["point"], axis) if not _route_hits_blocking_obstacle(item, obstacles, [])), None)
            if not path:
                continue
            edge_id = _route_edge_id("spine-lane-connector", space.guid, first["node_guid"], second["node_guid"])
            edges.append(
                _route_edge(
                    edge_id,
                    first["node_guid"],
                    second["node_guid"],
                    path,
                    [],
                    obstacles,
                    space,
                    "IFC corridor spine connector",
                )
            )
            break
    return edges


def _spine_group_coord(group: dict, axis: str) -> float:
    point = group["point"]
    return point[0] if axis == "x" else point[1]


def _spine_cross_lane_paths(first: tuple[float, float, float], second: tuple[float, float, float], axis: str) -> list[list[tuple[float, float, float]]]:
    z = max(first[2], second[2])
    if axis == "x":
        return [
            _dedupe_points([first, (first[0], second[1], z), second]),
            _dedupe_points([first, (second[0], first[1], z), second]),
            _dedupe_points([first, second]),
        ]
    return [
        _dedupe_points([first, (second[0], first[1], z), second]),
        _dedupe_points([first, (first[0], second[1], z), second]),
        _dedupe_points([first, second]),
    ]


def _spine_node_groups(space: Element, doors: list[Element], obstacles: list[Element]) -> list[dict]:
    axis, base_lane, margin = _space_spine_axis(space)
    grouped: list[dict] = []
    items = []
    for door in doors:
        if not door.center:
            continue
        entry, point, coord, lane = _spine_door_points(door, space, axis, base_lane, margin, obstacles)
        items.append({"door": door, "entry": entry, "point": point, "coord": coord, "lane": lane})
    for item in sorted(items, key=lambda value: (round(value["lane"] * 2) / 2, value["coord"], value["door"].guid)):
        lane_key = round(item["lane"] * 2) / 2
        if not grouped or grouped[-1]["lane_key"] != lane_key or abs(item["coord"] - grouped[-1]["coord"]) > 0.75:
            grouped.append({"coord": item["coord"], "lane": item["lane"], "lane_key": lane_key, "items": [item]})
        else:
            grouped[-1]["items"].append(item)
            grouped[-1]["coord"] = sum(member["coord"] for member in grouped[-1]["items"]) / len(grouped[-1]["items"])
            grouped[-1]["lane"] = sum(member["lane"] for member in grouped[-1]["items"]) / len(grouped[-1]["items"])
    groups = []
    for index, group in enumerate(grouped, start=1):
        coord = group["coord"]
        lane = group["lane"]
        point = _spine_point(space, axis, lane, coord)
        groups.append(
            {
                "node_guid": f"route-node:{space.guid}:{index:03d}",
                "point": point,
                "lane_key": group["lane_key"],
                "coord_key": round(coord * 2) / 2,
                "doors": [
                    {**item, "point": point, "lane": lane}
                    for item in sorted(group["items"], key=lambda value: value["door"].guid)
                ],
            }
        )
    return groups


def _space_spine_axis(space: Element) -> tuple[str, float, float]:
    min_x, min_y, _min_z = space.bbox_min
    max_x, max_y, _max_z = space.bbox_max
    width = abs(max_x - min_x)
    depth = abs(max_y - min_y)
    margin = min(0.35, max(0.05, min(width, depth) * 0.08))
    if width >= depth:
        return "x", _clamp(space.center[1], min_y + margin, max_y - margin), margin
    return "y", _clamp(space.center[0], min_x + margin, max_x - margin), margin


def _spine_door_points(
    door: Element,
    space: Element,
    axis: str,
    base_lane: float,
    margin: float,
    obstacles: list[Element],
) -> tuple[tuple[float, float, float], tuple[float, float, float], float, float]:
    min_x, min_y, _min_z = space.bbox_min
    max_x, max_y, _max_z = space.bbox_max
    z = max(door.center[2], space.center[2])
    entry = (
        _clamp(door.center[0], min_x + margin, max_x - margin),
        _clamp(door.center[1], min_y + margin, max_y - margin),
        z,
    )
    lane = _door_spine_lane(door, space, axis, base_lane, margin, obstacles, entry, z)
    if axis == "x":
        point = (entry[0], lane, z)
        return entry, point, entry[0], lane
    point = (lane, entry[1], z)
    return entry, point, entry[1], lane


def _door_spine_lane(
    door: Element,
    space: Element,
    axis: str,
    base_lane: float,
    margin: float,
    obstacles: list[Element],
    entry: tuple[float, float, float],
    z: float,
) -> float:
    min_x, min_y, _min_z = space.bbox_min
    max_x, max_y, _max_z = space.bbox_max
    if axis == "x":
        low, high = min_y + margin, max_y - margin
        door_low, door_high, door_center = door.bbox_min[1], door.bbox_max[1], door.center[1]
        candidates = [door_center, entry[1], door_low + 0.2, door_high - 0.2, door_low - 0.2, door_high + 0.2, base_lane]
    else:
        low, high = min_x + margin, max_x - margin
        door_low, door_high, door_center = door.bbox_min[0], door.bbox_max[0], door.center[0]
        candidates = [door_center, entry[0], door_low + 0.2, door_high - 0.2, door_low - 0.2, door_high + 0.2, base_lane]
    scored = []
    for candidate in candidates:
        lane = _clamp(candidate, low, high)
        point = (entry[0], lane, z) if axis == "x" else (lane, entry[1], z)
        connector = _dedupe_points([door.center, entry, point])
        corridor = _spine_lane_probe(space, axis, lane, z)
        blocked_connector = _route_hits_blocking_obstacle(connector, obstacles, [door])
        blocked_corridor = _route_hits_blocking_obstacle(corridor, obstacles, [])
        score = (1 if blocked_connector else 0, 1 if blocked_corridor else 0, abs(lane - door_center))
        scored.append((score, lane))
    return min(scored, key=lambda item: item[0])[1]


def _spine_lane_probe(space: Element, axis: str, lane: float, z: float) -> list[tuple[float, float, float]]:
    min_x, min_y, _min_z = space.bbox_min
    max_x, max_y, _max_z = space.bbox_max
    if axis == "x":
        return [(min_x, lane, z), (max_x, lane, z)]
    return [(lane, min_y, z), (lane, max_y, z)]


def _spine_point(space: Element, axis: str, lane: float, coord: float) -> tuple[float, float, float]:
    z = space.center[2]
    if axis == "x":
        return (coord, lane, z)
    return (lane, coord, z)


def _route_edge(
    edge_id: str,
    start_guid: str,
    end_guid: str,
    path: list[tuple[float, float, float]],
    route_doors: list[Element],
    obstacles: list[Element],
    space: Element | None,
    source: str,
) -> RouteEdge:
    measurements = _route_measurements(route_doors, path, obstacles, space)
    dist = sum(distance(path[i], path[i + 1]) for i in range(len(path) - 1))
    return RouteEdge(
        edge_id=edge_id,
        start_guid=start_guid,
        end_guid=end_guid,
        distance_m=dist,
        status="unchecked",
        reasons=[],
        path=path,
        source=source,
        via_space_guid=space.guid if space else None,
        via_space_label=space.label if space else None,
        measurements=measurements,
    )


def _route_edge_id(*parts: str) -> str:
    value = "|".join(parts).encode("utf-8")
    return "E" + hashlib.sha1(value).hexdigest()[:10].upper()


def _add_unreachable_door_edges(edges: list[RouteEdge], elements: list[Element]) -> list[RouteEdge]:
    result = list(edges)
    connected = {guid for edge in edges for guid in [edge.start_guid, edge.end_guid]}
    floor_refs = _route_floor_refs(elements)
    for door in sorted(elements, key=lambda item: item.guid):
        if (
            door.ifc_type != "IfcDoor"
            or not door.extra.get("isRouteRelevantDoor")
            or door.extra.get("isExcludedRouteDoor")
            or not door.center
            or door.guid in connected
        ):
            continue
        floor_name = _route_floor_name(door, floor_refs) or door.storey or "unknown"
        end_guid = _network_node_guid(floor_name, ("unreachable", door.guid), door.center)
        result.append(
            RouteEdge(
                edge_id=_route_edge_id("unreachable", door.guid),
                start_guid=door.guid,
                end_guid=end_guid,
                distance_m=0.0,
                status="unchecked",
                reasons=[],
                path=[door.center],
                source="IFC route topology",
                measurements={
                    "routeReachable": False,
                    "routeHasTurn": False,
                    "routeNetworkRole": "candidate",
                },
            )
        )
    return result


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
    route_doors: list[Element],
    path: list[tuple[float, float, float]],
    obstacles: list[Element],
    space: Element | None,
) -> dict[str, float | str | bool | None]:
    measurements: dict[str, float | str | bool | None] = {}
    widths = [float(door.extra.get("derivedDoorWidthM")) for door in route_doors if door.extra.get("derivedDoorWidthM") is not None]
    if widths:
        measurements["routeDoorWidthMinM"] = min(widths)
    turn_points = []
    if space:
        clear = _num(space.extra.get("derivedClearSpaceWidthM"))
        turn = _num(space.extra.get("turningSpaceM"))
        if clear is not None:
            measurements["routeClearWidthM"] = clear
        if turn is not None:
            measurements["routeTurningSpaceM"] = turn
        if len(path) > 1:
            direct = [path[0], path[-1]]
            direct_blocked = _route_hits_wall(direct, obstacles, route_doors) or _route_hits_stair(direct, obstacles, space)
            if direct_blocked:
                turn_points = _route_turn_points(path)
        if turn_points:
            _set_route_measurement_point(measurements, "routeTurningPoint", turn_points[0])
    measurements["routeHasTurn"] = bool(turn_points)
    measurements["routeRequiredTurnCount"] = len(turn_points)
    measurements["routeHitsWall"] = _route_hits_wall(path, obstacles, route_doors)
    measurements["routeHitsStair"] = _route_hits_stair(path, obstacles, space)
    measurements.update(_ramp_measurements(obstacles, space, path))
    return measurements


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _path_has_turn(path: list[tuple[float, float, float]]) -> bool:
    return bool(_route_turn_points(path))


def _route_hits_stair(path: list[tuple[float, float, float]], obstacles: list[Element], space: Element | None) -> bool:
    stairs = [item for item in obstacles if item.ifc_type in {"IfcStair", "IfcStairFlight"}]
    if not stairs:
        return False
    return _route_intersects_any(path, stairs)


def _route_hits_wall(path: list[tuple[float, float, float]], obstacles: list[Element], allowed_elements: list[Element]) -> bool:
    walls = [item for item in obstacles if item.ifc_type in {"IfcWall", "IfcColumn"}]
    if not walls:
        return False
    return _route_centerline_intersects_any(path, walls, allowed_elements)


def _route_hits_blocking_obstacle(path: list[tuple[float, float, float]], obstacles: list[Element], allowed_elements: list[Element]) -> bool:
    return _route_hits_wall(path, obstacles, allowed_elements) or _route_hits_stair(path, obstacles, None)


def _route_centerline_intersects_any(path: list[tuple[float, float, float]], obstacles: list[Element], allowed_elements: list[Element]) -> bool:
    for start, end in zip(path, path[1:]):
        for obstacle in obstacles:
            if not obstacle.bbox_min or not obstacle.bbox_max:
                continue
            if not _segment_z_overlaps(start, end, obstacle):
                continue
            hit = _segment_box_hit_2d(start, end, obstacle.bbox_min, obstacle.bbox_max)
            if not hit:
                continue
            point = _segment_point(start, end, (hit[0] + hit[1]) / 2)
            if _point_allowed_through_element(point, obstacle, allowed_elements):
                continue
            return True
    return False


def _segment_z_overlaps(start: tuple[float, float, float], end: tuple[float, float, float], obstacle: Element) -> bool:
    low = min(start[2], end[2]) - ROUTE_Z_TOLERANCE
    high = max(start[2], end[2]) + ROUTE_Z_TOLERANCE
    return low <= obstacle.bbox_max[2] and high >= obstacle.bbox_min[2]


def _segment_box_hit_2d(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    box_min: tuple[float, float, float],
    box_max: tuple[float, float, float],
) -> tuple[float, float] | None:
    t0 = 0.0
    t1 = 1.0
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    for p, q in [(-dx, start[0] - box_min[0]), (dx, box_max[0] - start[0]), (-dy, start[1] - box_min[1]), (dy, box_max[1] - start[1])]:
        if abs(p) < 1e-9:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t1 - t0 <= 1e-6:
        return None
    return t0, t1


def _segment_point(start: tuple[float, float, float], end: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    return (
        start[0] + (end[0] - start[0]) * t,
        start[1] + (end[1] - start[1]) * t,
        start[2] + (end[2] - start[2]) * t,
    )


def _point_allowed_through_element(point: tuple[float, float, float], obstacle: Element, allowed_elements: list[Element]) -> bool:
    for element in allowed_elements:
        if not element.bbox_min or not element.bbox_max:
            continue
        if not _elements_overlap_2d(element, obstacle):
            continue
        margin = 0.12
        if (
            element.bbox_min[0] - margin <= point[0] <= element.bbox_max[0] + margin
            and element.bbox_min[1] - margin <= point[1] <= element.bbox_max[1] + margin
            and element.bbox_min[2] - 0.2 <= point[2] <= element.bbox_max[2] + 0.2
        ):
            return True
    return False


def _elements_overlap_2d(a: Element, b: Element) -> bool:
    overlap_x = min(a.bbox_max[0], b.bbox_max[0]) - max(a.bbox_min[0], b.bbox_min[0])
    overlap_y = min(a.bbox_max[1], b.bbox_max[1]) - max(a.bbox_min[1], b.bbox_min[1])
    return overlap_x > 0.02 and overlap_y > 0.02


def _route_intersects_any(path: list[tuple[float, float, float]], obstacles: list[Element]) -> bool:
    half = RULE_LIMITS.clearance_width_m / 2
    for point in _sample_path(path, step=0.35):
        x, y, z = point
        route_min = (x - half, y - half, z - ROUTE_Z_TOLERANCE)
        route_max = (x + half, y + half, z + ROUTE_Z_TOLERANCE)
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


def routes_from_start(edges: list[RouteEdge], start_guid: str, pass_only: bool = False, target_guids: set[str] | None = None) -> list[dict]:
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
