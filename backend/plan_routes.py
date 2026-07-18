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
from .routes import route_measurements

PLAN_GRID_STEP = 0.20
PLAN_ROUTE_HALF_WIDTH = 0.04
PLAN_GEOMETRY_TOLERANCE = 0.005
CORRIDOR_BOUNDARY_STEP = 0.55
CORRIDOR_SKELETON_CLEARANCE = 0.12
CORRIDOR_MOVEMENT_STEP = 0.40


def prepare_plan_geometry(
    ifc_path: Path,
    elements: list[Element],
) -> tuple[dict[str, object], dict[str, list[str]], list[dict]]:
    import ifcopenshell

    model = ifcopenshell.open(str(ifc_path))
    footprints = _element_footprints(model, elements)
    open_boundaries = _open_boundary_portals(model, elements, footprints)
    floor_refs = _floor_refs(elements)
    spaces_by_door = _door_to_spaces(model)
    _add_geometric_door_spaces(spaces_by_door, elements, footprints, floor_refs)
    _filter_door_spaces(spaces_by_door, elements, footprints, _door_normals(model, elements))
    _set_space_clearance_regions(elements, footprints, spaces_by_door)
    return footprints, spaces_by_door, open_boundaries


def build_plan_route_edges(
    ifc_path: Path,
    elements: list[Element],
    route_edges: list[RouteEdge],
    prepared: tuple[dict[str, object], dict[str, list[str]], list[dict]] | None = None,
) -> list[RouteEdge]:
    footprints, spaces_by_door, open_boundaries = prepared or prepare_plan_geometry(ifc_path, elements)
    candidates = _plan_candidates(elements, footprints, spaces_by_door, route_edges)
    candidates.extend(_open_boundary_candidates(elements, footprints, spaces_by_door, open_boundaries, candidates))
    candidates.extend(_exterior_ramp_candidates(elements, footprints, spaces_by_door))
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
        path_area = _route_path_area(area) if area is not None and not area.is_empty else None
        grid = _area_grid(area) if area is not None and not area.is_empty else None
        approaches = {
            door.guid: _door_space_approach(door, space, area, footprints, path_area) for door in unique_doors
        }
        if grid:
            grid["door_cells"] = _grid_door_cells(grid, _route_door_zone(unique_doors, footprints))
            grid["target_cells"] = {
                cell
                for door in unique_doors
                for cell in [_nearest_cell(grid, approaches.get(door.guid) or door.center)]
                if cell is not None
            }
            grid["door_cells"].update(grid["target_cells"])
        turning_cache = {}
        width_cache = {}
        for first, second in combinations(unique_doors, 2):
            key = _pair_key(first.guid, second.guid, space_guid)
            matched = min(current.get(key, []), key=lambda edge: edge.distance_m, default=None)
            paths = []
            first_approach = approaches.get(first.guid)
            second_approach = approaches.get(second.guid)
            matched_path = _oriented_path(matched.path, first, second) if matched else None
            if matched_path and (area is None or _path_inside_area(matched_path, area, path_area)):
                _add_plan_path(paths, matched_path, area, path_area)
            choices = _plan_path_choices(
                paths,
                first,
                second,
                obstacles,
                space,
                area,
                footprints,
                turning_cache,
                width_cache,
                path_area,
                first_approach,
                second_approach,
            )
            if grid and not any(_preferred_plan_choice(choice) for choice in choices):
                previous_count = len(paths)
                _add_plan_path(
                    paths,
                    _path_in_area(
                        first,
                        second,
                        space,
                        area,
                        grid,
                        path_area=path_area,
                        first_approach=first_approach,
                        second_approach=second_approach,
                    ),
                    area,
                    path_area,
                )
                choices.extend(
                    _plan_path_choices(
                        paths[previous_count:],
                        first,
                        second,
                        obstacles,
                        space,
                        area,
                        footprints,
                        turning_cache,
                        width_cache,
                        path_area,
                        first_approach,
                        second_approach,
                    )
                )
            if grid and not any(not choice[2] for choice in choices) and _accessible_search_needed(choices):
                previous_count = len(paths)
                _add_plan_path(
                    paths,
                    _path_in_area(
                        first,
                        second,
                        space,
                        area,
                        grid,
                        accessible=True,
                        path_area=path_area,
                        first_approach=first_approach,
                        second_approach=second_approach,
                    ),
                    area,
                    path_area,
                )
                choices.extend(
                    _plan_path_choices(
                        paths[previous_count:],
                        first,
                        second,
                        obstacles,
                        space,
                        area,
                        footprints,
                        turning_cache,
                        width_cache,
                        path_area,
                        first_approach,
                        second_approach,
                    )
                )
            if not choices:
                continue
            passing = [choice for choice in choices if not choice[2]]
            path, measurements, reasons = min(passing or choices, key=_plan_choice_key)
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


def _exterior_ramp_candidates(
    elements: list[Element],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
) -> list[RouteEdge]:
    from shapely.geometry import LineString

    by_guid = {element.guid: element for element in elements}
    doors = [element for element in elements if _route_door(element) and len(spaces_by_door.get(element.guid, [])) == 1]
    obstacles = obstacle_elements(elements)
    result = []
    for ramp in elements:
        if ramp.ifc_type not in {"IfcRamp", "IfcRampFlight"}:
            continue
        lower = _ramp_point(ramp, "rampLowerPoint")
        upper = _ramp_point(ramp, "rampUpperPoint")
        axis = _ramp_axis(ramp)
        polygon = _element_polygon(ramp, footprints)
        if lower is None or upper is None or axis is None or polygon is None or polygon.is_empty:
            continue
        centerline = LineString([(lower[0], lower[1]), (upper[0], upper[1])])
        if not polygon.buffer(0.06).covers(centerline):
            continue
        matches = []
        for door in doors:
            display_storey = ramp.extra.get("rampDisplayStorey") or ramp.storey
            if display_storey and door.storey and str(display_storey) != door.storey:
                continue
            door_axes = _door_axes(door, _element_polygon(door, footprints))
            if door_axes is None or abs(axis[0] * door_axes[1][0] + axis[1] * door_axes[1][1]) < 0.80:
                continue
            distances = [
                (math.hypot(door.center[0] - point[0], door.center[1] - point[1]), name, point, other)
                for name, point, other in [("lower", lower, upper), ("upper", upper, lower)]
            ]
            gap, endpoint, near, far = min(distances, key=lambda value: value[0])
            far_gap = math.hypot(door.center[0] - far[0], door.center[1] - far[1])
            floor_z = door.bbox_min[2] if door.bbox_min else door.center[2]
            if gap > 0.80 or far_gap < gap + 0.20 or abs(near[2] - floor_z) > 0.40:
                continue
            z_offset = 0.05
            path = [
                (door.center[0], door.center[1], near[2] + z_offset),
                (near[0], near[1], near[2] + z_offset),
                (far[0], far[1], far[2] + z_offset),
            ]
            if not _route_avoids_walls(path, ramp, obstacles, [door], footprints):
                continue
            matches.append((gap, door.guid, door, endpoint, path))
        if not matches:
            continue
        gap, _door_guid, door, endpoint, path = min(matches)
        measurements = route_measurements(door, ramp, path, obstacles, None)
        measurements["routeUsesRamp"] = True
        measurements["routeRampGuid"] = ramp.guid
        measurements["routeExteriorAccess"] = True
        measurements["routeRampConnectionEndpoint"] = endpoint
        measurements["routeRampConnectionDistanceM"] = round(gap, 4)
        measurements["routeHitsWall"] = False
        for route_key, ramp_key in [
            ("routeRampSlopePercent", "rampSlopePercent"),
            ("routeRampUsableWidthM", "rampUsableWidthM"),
            ("routeRampRunLengthM", "rampRunLengthM"),
        ]:
            value = _number(ramp.extra.get(ramp_key))
            if value is not None:
                measurements[route_key] = value
        reasons = _measurement_reasons(measurements)
        space_guid = spaces_by_door[door.guid][0]
        space = by_guid.get(space_guid)
        result.append(
            RouteEdge(
                edge_id=_plan_edge_id(door.guid, ramp.guid, "exterior-ramp"),
                start_guid=door.guid,
                end_guid=ramp.guid,
                distance_m=_path_length(path),
                status="fail" if reasons else "pass",
                reasons=reasons,
                path=path,
                source="2D exterior ramp route",
                via_space_guid=space_guid,
                via_space_label=space.label if space else None,
                measurements=measurements,
            )
        )
    return result


def _ramp_point(ramp: Element, prefix: str) -> tuple[float, float, float] | None:
    values = tuple(_number(ramp.extra.get(f"{prefix}{suffix}")) for suffix in ["X", "Y", "Z"])
    return values if all(value is not None for value in values) else None


def _ramp_axis(ramp: Element) -> tuple[float, float] | None:
    x = _number(ramp.extra.get("rampRunAxisX"))
    y = _number(ramp.extra.get("rampRunAxisY"))
    length = math.hypot(x or 0.0, y or 0.0)
    if x is None or y is None or length <= 1e-6:
        return None
    return x / length, y / length


def _open_boundary_candidates(
    elements: list[Element],
    footprints: dict[str, object],
    spaces_by_door: dict[str, list[str]],
    portals: list[dict],
    candidates: list[RouteEdge],
) -> list[RouteEdge]:
    if not portals:
        return []
    by_guid = {element.guid: element for element in elements}
    doors_by_space: dict[str, list[Element]] = defaultdict(list)
    for door_guid, space_guids in spaces_by_door.items():
        door = by_guid.get(door_guid)
        if not _route_door(door):
            continue
        for space_guid in space_guids:
            doors_by_space[space_guid].append(door)

    parent = {}

    def find(value):
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(first, second):
        first = find(first)
        second = find(second)
        if first != second:
            parent[second] = first

    physical_parent = {}

    def find_physical(value):
        physical_parent.setdefault(value, value)
        if physical_parent[value] != value:
            physical_parent[value] = find_physical(physical_parent[value])
        return physical_parent[value]

    def union_physical(first, second):
        first = find_physical(first)
        second = find_physical(second)
        if first != second:
            physical_parent[second] = first

    for edge in candidates:
        union_physical(edge.start_guid, edge.end_guid)
        if edge.status == "pass":
            union(edge.start_guid, edge.end_guid)

    space_parent = {}

    def find_space(value):
        space_parent.setdefault(value, value)
        if space_parent[value] != value:
            space_parent[value] = find_space(space_parent[value])
        return space_parent[value]

    for portal in portals:
        first = find_space(portal["spaces"][0])
        second = find_space(portal["spaces"][1])
        if first != second:
            space_parent[second] = first
    spaces_by_root: dict[str, set[str]] = defaultdict(set)
    portals_by_root: dict[str, list[dict]] = defaultdict(list)
    for portal in portals:
        root = find_space(portal["spaces"][0])
        spaces_by_root[root].update(portal["spaces"])
        portals_by_root[root].append(portal)

    obstacles = obstacle_elements(elements)
    result = []
    for root in sorted(spaces_by_root):
        space_guids = spaces_by_root[root]
        cluster_portals = portals_by_root[root]
        doors = sorted(
            {
                door.guid: door
                for space_guid in space_guids
                for door in doors_by_space.get(space_guid, [])
                if door.center
            }.values(),
            key=lambda door: door.guid,
        )
        groups: dict[str, list[Element]] = defaultdict(list)
        for door in doors:
            groups[find(door.guid)].append(door)
        if len(groups) < 2:
            continue
        area, reference = _open_boundary_walkable_area(
            space_guids, cluster_portals, by_guid, doors_by_space, obstacles, footprints
        )
        if area is None or reference is None:
            continue
        path_area = _route_path_area(area)
        grid = _area_grid(area)
        approaches = {
            door.guid: _open_boundary_door_approach(
                door, space_guids, by_guid, spaces_by_door, area, footprints, path_area
            )
            for door in doors
        }
        if grid:
            grid["door_cells"] = _grid_door_cells(grid, _route_door_zone(doors, footprints))
            grid["target_cells"] = {
                cell
                for door in doors
                for cell in [_nearest_cell(grid, approaches.get(door.guid) or door.center)]
                if cell is not None
            }
            grid["door_cells"].update(grid["target_cells"])
        turning_cache = {}
        width_cache = {}
        grouped_doors = [groups[key] for key in sorted(groups)]
        for first_group, second_group in combinations(grouped_doors, 2):
            edge = _open_boundary_bridge(
                first_group,
                second_group,
                space_guids,
                cluster_portals,
                reference,
                area,
                grid,
                path_area,
                approaches,
                obstacles,
                footprints,
                turning_cache,
                width_cache,
            )
            if edge is not None and (
                edge.status == "pass" or find_physical(edge.start_guid) != find_physical(edge.end_guid)
            ):
                result.append(edge)
    return result


def _open_boundary_walkable_area(
    space_guids: set[str],
    portals: list[dict],
    by_guid: dict[str, Element],
    doors_by_space: dict[str, list[Element]],
    obstacles: list[Element],
    footprints: dict[str, object],
):
    from shapely.ops import unary_union

    spaces = [by_guid[guid] for guid in sorted(space_guids) if guid in by_guid]
    areas = []
    polygons = []
    for space in spaces:
        polygon = _element_polygon(space, footprints)
        if polygon is not None and not polygon.is_empty:
            polygons.append(polygon)
        area = _space_walkable_area(space, doors_by_space.get(space.guid, []), obstacles, footprints)
        if area is not None and not area.is_empty:
            areas.append(area)
    if not spaces or not areas:
        return None, None
    source = unary_union(areas).buffer(0)
    footprint = unary_union(polygons).buffer(0) if polygons else source
    blockers = []
    for obstacle in obstacles:
        if obstacle.ifc_type not in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"}:
            continue
        if not _z_overlap(spaces[0], obstacle):
            continue
        polygon = _element_polygon(obstacle, footprints)
        if polygon is not None and not polygon.is_empty:
            blockers.append(polygon)
    blocked = unary_union(blockers) if blockers else None
    zones = []
    for portal in portals:
        zone = portal["geometry"].buffer(PLAN_ROUTE_HALF_WIDTH + 0.02, cap_style=2).intersection(
            footprint.buffer(0.03)
        )
        if blocked is not None:
            zone = zone.difference(blocked)
        if not zone.is_empty:
            zones.append(zone)
    if zones:
        source = unary_union([source, *zones]).buffer(0)
    return (source if not source.is_empty else None), spaces[0]


def _open_boundary_door_approach(
    door: Element,
    space_guids: set[str],
    by_guid: dict[str, Element],
    spaces_by_door: dict[str, list[str]],
    area,
    footprints: dict[str, object],
    path_area,
):
    for space_guid in spaces_by_door.get(door.guid, []):
        if space_guid not in space_guids:
            continue
        space = by_guid.get(space_guid)
        if space is None:
            continue
        point = _door_space_approach(door, space, area, footprints, path_area)
        if point is not None:
            return point
    return None


def _open_boundary_bridge(
    first_doors: list[Element],
    second_doors: list[Element],
    space_guids: set[str],
    portals: list[dict],
    reference: Element,
    area,
    grid,
    path_area,
    approaches: dict[str, object],
    obstacles: list[Element],
    footprints: dict[str, object],
    turning_cache: dict,
    width_cache: dict,
) -> RouteEdge | None:
    pairs = sorted(
        [
            (
                math.hypot(first.center[0] - second.center[0], first.center[1] - second.center[1]),
                first,
                second,
            )
            for first in first_doors
            for second in second_doors
        ],
        key=lambda value: (value[0], value[1].guid, value[2].guid),
    )
    best = None
    for _distance, first, second in pairs:
        choices = _open_boundary_path_choices(
            first,
            second,
            reference,
            area,
            grid,
            path_area,
            approaches.get(first.guid),
            approaches.get(second.guid),
            obstacles,
            footprints,
            turning_cache,
            width_cache,
        )
        if not choices:
            continue
        passing = [choice for choice in choices if not choice[2]]
        choice = min(passing or choices, key=_plan_choice_key)
        if passing:
            return _open_boundary_edge(first, second, choice, space_guids, portals)
        if best is None or _plan_choice_key(choice) < _plan_choice_key(best[2]):
            best = first, second, choice
    if best is None:
        return None
    return _open_boundary_edge(best[0], best[1], best[2], space_guids, portals)


def _open_boundary_path_choices(
    first: Element,
    second: Element,
    reference: Element,
    area,
    grid,
    path_area,
    first_approach,
    second_approach,
    obstacles: list[Element],
    footprints: dict[str, object],
    turning_cache: dict,
    width_cache: dict,
):
    path = [first.center]
    if first_approach is not None:
        path.append(first_approach)
    if second_approach is not None:
        path.append(second_approach)
    path.append(second.center)
    paths = []
    _add_plan_path(paths, path, area, path_area)
    choices = _plan_path_choices(
        paths,
        first,
        second,
        obstacles,
        reference,
        area,
        footprints,
        turning_cache,
        width_cache,
        path_area,
        first_approach,
        second_approach,
    )
    if grid and not any(_preferred_plan_choice(choice) for choice in choices):
        previous_count = len(paths)
        _add_plan_path(
            paths,
            _path_in_area(
                first,
                second,
                reference,
                area,
                grid,
                path_area=path_area,
                first_approach=first_approach,
                second_approach=second_approach,
            ),
            area,
            path_area,
        )
        choices.extend(
            _plan_path_choices(
                paths[previous_count:],
                first,
                second,
                obstacles,
                reference,
                area,
                footprints,
                turning_cache,
                width_cache,
                path_area,
                first_approach,
                second_approach,
            )
        )
    if grid and not any(not choice[2] for choice in choices) and _accessible_search_needed(choices):
        previous_count = len(paths)
        _add_plan_path(
            paths,
            _path_in_area(
                first,
                second,
                reference,
                area,
                grid,
                accessible=True,
                path_area=path_area,
                first_approach=first_approach,
                second_approach=second_approach,
            ),
            area,
            path_area,
        )
        choices.extend(
            _plan_path_choices(
                paths[previous_count:],
                first,
                second,
                obstacles,
                reference,
                area,
                footprints,
                turning_cache,
                width_cache,
                path_area,
                first_approach,
                second_approach,
            )
        )
    return choices


def _open_boundary_edge(first, second, choice, space_guids: set[str], portals: list[dict]) -> RouteEdge:
    path, measurements, reasons = choice
    measurements = dict(measurements)
    measurements["planOpenBoundaryCount"] = len(portals)
    measurements["planOpenBoundaryHeightMinM"] = round(min(portal["height_m"] for portal in portals), 4)
    measurements["planOpenBoundaryWidthMaxM"] = round(max(portal["width_m"] for portal in portals), 4)
    measurements["planOpenBoundarySpaceGuids"] = " ".join(sorted(space_guids))
    cluster = "open:" + ":".join(sorted(space_guids))
    return RouteEdge(
        edge_id=_plan_edge_id(first.guid, second.guid, cluster),
        start_guid=first.guid,
        end_guid=second.guid,
        distance_m=_path_length(path),
        status="fail" if reasons else "pass",
        reasons=reasons,
        path=path,
        source="2D open-plan area route",
        measurements=measurements,
    )


def _add_plan_path(paths: list, path, area, path_area=None) -> None:
    if not path or len(path) < 2:
        return
    compact = _compact_path(path)
    candidates = [compact]
    if area is not None and not area.is_empty:
        candidates.insert(0, _simplify_path(compact, area, path_area))
    existing = {_path_signature(value) for value in paths}
    for candidate in candidates:
        if len(candidate) < 2 or (area is not None and not _path_inside_area(candidate, area, path_area)):
            continue
        signature = _path_signature(candidate)
        if signature not in existing:
            paths.append(candidate)
            existing.add(signature)


def _path_signature(path) -> tuple:
    return tuple(tuple(round(value, 4) for value in point) for point in path)


def _plan_choice_key(choice) -> tuple:
    path, measurements, reasons = choice
    turns = int(_number(measurements.get("routeRequiredTurnCount")) or 0)
    return len(reasons), _route_clearance_shortfall(measurements), turns, len(path), _path_length(path)


def _preferred_plan_choice(choice) -> bool:
    return not choice[2] and _route_clearance_shortfall(choice[1]) <= 0.0


def _route_clearance_shortfall(measurements: dict) -> float:
    clearance = _number(measurements.get("routeWallClearanceMinM"))
    target = _number(measurements.get("routeWallClearanceTargetM"))
    if clearance is None or target is None:
        return 0.0
    return max(0.0, target - clearance - PLAN_GRID_STEP / 2)


def _plan_path_choices(
    paths,
    first,
    second,
    obstacles,
    space,
    area,
    footprints,
    turning_cache,
    width_cache,
    path_area,
    first_approach=None,
    second_approach=None,
):
    choices = []
    for path in paths:
        if not _path_uses_door_approach(path, first, second, first_approach, second_approach):
            continue
        if not _route_avoids_walls(path, space, obstacles, [first, second], footprints):
            continue
        choice = _plan_path_choice(
            path, first, second, obstacles, space, area, footprints, turning_cache, width_cache, path_area
        )
        choice[1]["routeHitsWall"] = False
        choices.append(choice)
        if not choice[2]:
            break
    return choices


def _plan_path_choice(path, first, second, obstacles, space, area, footprints, turning_cache, width_cache, path_area):
    measurements = route_measurements(first, second, path, obstacles, space)
    measurements["routeGridStepM"] = PLAN_GRID_STEP
    if area is not None and not area.is_empty:
        _set_route_clear_width(measurements, path, area, [first, second], footprints, width_cache)
        _set_route_turning_space(measurements, path, area, turning_cache, path_area)
        clearance = _route_wall_clearance(path, area, [first, second], footprints)
        if clearance is not None:
            measurements["routeWallClearanceMinM"] = round(clearance, 4)
            measurements["routeWallClearanceTargetM"] = round(_area_clearance_target(area), 4)
    return path, measurements, _measurement_reasons(measurements)


def _accessible_search_needed(choices) -> bool:
    fixable = {"route_width", "turning_space"}
    return any(reasons and set(reasons).issubset(fixable) for _path, _measurements, reasons in choices)


def _element_footprints(model, elements: list[Element]) -> dict[str, object]:
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    wanted = {
        element.guid
        for element in elements
        if element.ifc_type in {
            "IfcSpace",
            "IfcDoor",
            "IfcWall",
            "IfcColumn",
            "IfcStair",
            "IfcStairFlight",
            "IfcRamp",
            "IfcRampFlight",
        }
    }
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


def _open_boundary_curve_points(curve) -> list[tuple[float, ...]]:
    if curve is None:
        return []
    if curve.is_a("IfcPolyline"):
        return [tuple(float(value) for value in point.Coordinates) for point in curve.Points]
    if curve.is_a("IfcCompositeCurve"):
        points = []
        for segment in curve.Segments:
            values = _open_boundary_curve_points(segment.ParentCurve)
            if not bool(segment.SameSense):
                values.reverse()
            if points and values and all(abs(first - second) <= 1e-7 for first, second in zip(points[-1], values[0])):
                values = values[1:]
            points.extend(values)
        return points
    return []


def _open_boundary_surface_points(points, placement, scale: float) -> list[tuple[float, float, float]]:
    from ifcopenshell.util.placement import get_axis2placement

    matrix = get_axis2placement(placement)
    result = []
    for point in points:
        coordinates = list(point) + [0.0] * (3 - len(point))
        value = coordinates[:3] + [1.0]
        result.append(
            tuple(
                float(sum(matrix[row][column] * value[column] for column in range(4))) * scale
                for row in range(3)
            )
        )
    return result


def _open_boundary_space_points(points, space, scale: float) -> list[tuple[float, float, float]]:
    from ifcopenshell.util.placement import get_local_placement

    matrix = get_local_placement(space.ObjectPlacement)
    result = []
    for point in points:
        result.append(
            tuple(
                float(sum(matrix[row][column] * point[column] for column in range(3)))
                + float(matrix[row][3]) * scale
                for row in range(3)
            )
        )
    return result


def _open_boundary_line(relation, scale: float):
    from shapely.geometry import LineString

    geometry = getattr(relation, "ConnectionGeometry", None)
    surface = getattr(geometry, "SurfaceOnRelatingElement", None) if geometry else None
    if surface is None:
        return None
    points = []
    if surface.is_a("IfcCurveBoundedPlane"):
        values = _open_boundary_curve_points(surface.OuterBoundary)
        points = _open_boundary_surface_points(values, surface.BasisSurface.Position, scale)
    elif surface.is_a("IfcSurfaceOfLinearExtrusion"):
        values = _open_boundary_curve_points(surface.SweptCurve.Curve)
        base = _open_boundary_surface_points(values, surface.Position, scale)
        from ifcopenshell.util.placement import get_axis2placement

        matrix = get_axis2placement(surface.Position)
        direction = list(surface.ExtrudedDirection.DirectionRatios) + [0.0] * (
            3 - len(surface.ExtrudedDirection.DirectionRatios)
        )
        vector = tuple(
            float(sum(matrix[row][column] * direction[column] for column in range(3))) for row in range(3)
        )
        length = math.sqrt(sum(value * value for value in vector)) or 1.0
        depth = float(surface.Depth) * scale
        offset = tuple(value / length * depth for value in vector)
        points = base + [tuple(point[index] + offset[index] for index in range(3)) for point in base]
    if len(points) < 2:
        return None
    space = getattr(relation, "RelatingSpace", None)
    if space is not None and getattr(space, "ObjectPlacement", None):
        points = _open_boundary_space_points(points, space, scale)
    height = max(point[2] for point in points) - min(point[2] for point in points)
    first = None
    second = None
    span = 0.0
    for index, point in enumerate(points):
        for other in points[index + 1 :]:
            value = math.hypot(point[0] - other[0], point[1] - other[1])
            if value > span:
                first = point
                second = other
                span = value
    if first is None or second is None or height < 0.20 or span < 0.20:
        return None
    return LineString([(first[0], first[1]), (second[0], second[1])]), height


def _aggregate_space(space: Element) -> bool:
    text = " ".join(
        str(value)
        for value in [space.extra.get("semanticText"), space.extra.get("semanticLongName"), space.label]
        if value
    ).lower()
    return any(term in text for term in ["bruttoareal", "gross floor area", "gross area"])


def _open_boundary_portals(model, elements: list[Element], footprints: dict[str, object]) -> list[dict]:
    from ifcopenshell.util.unit import calculate_unit_scale
    from shapely.ops import unary_union

    by_guid = {element.guid: element for element in elements}
    spaces_by_floor: dict[str | None, list[Element]] = defaultdict(list)
    for space in elements:
        if space.ifc_type != "IfcSpace" or space.extra.get("isExcludedSpace") or _aggregate_space(space):
            continue
        spaces_by_floor[space.storey].append(space)
    obstacles = [element for element in elements if element.ifc_type in {"IfcWall", "IfcColumn"}]
    scale = calculate_unit_scale(model)
    portals = {}
    blocked_by_floor = {}
    for relation in model.by_type("IfcRelSpaceBoundary"):
        if str(getattr(relation, "PhysicalOrVirtualBoundary", "")) != "VIRTUAL":
            continue
        if str(getattr(relation, "InternalOrExternalBoundary", "")) != "INTERNAL":
            continue
        relating = by_guid.get(getattr(getattr(relation, "RelatingSpace", None), "GlobalId", None))
        if relating is None or relating.extra.get("isExcludedSpace") or _aggregate_space(relating):
            continue
        boundary = _open_boundary_line(relation, scale)
        if boundary is None:
            continue
        line, height = boundary
        if height + PLAN_GEOMETRY_TOLERANCE < RULE_LIMITS.clearance_height_m:
            continue
        relating_polygon = _element_polygon(relating, footprints)
        if relating_polygon is None or relating_polygon.is_empty or relating_polygon.distance(line) > 0.20:
            continue
        floor_key = relating.storey, round(relating.center[2], 1) if relating.center else None
        if floor_key not in blocked_by_floor:
            blockers = []
            for obstacle in obstacles:
                if not _z_overlap(relating, obstacle):
                    continue
                polygon = _element_polygon(obstacle, footprints)
                if polygon is not None and not polygon.is_empty:
                    blockers.append(polygon)
            blocked_by_floor[floor_key] = unary_union(blockers).buffer(PLAN_ROUTE_HALF_WIDTH) if blockers else None
        blocked = blocked_by_floor[floor_key]
        clear = line.difference(blocked) if blocked is not None else line
        if clear.is_empty:
            continue
        candidates = []
        for other in spaces_by_floor.get(relating.storey, []):
            if other.guid == relating.guid or not _z_overlap(relating, other):
                continue
            polygon = _element_polygon(other, footprints)
            if polygon is None or polygon.is_empty or polygon.distance(line) > 0.12:
                continue
            geometry = clear.intersection(relating_polygon.buffer(0.08)).intersection(polygon.buffer(0.08))
            width = float(geometry.length)
            if width >= 0.20:
                candidates.append((width, other, geometry))
        if not candidates:
            continue
        best = max(value[0] for value in candidates)
        for width, other, geometry in candidates:
            if width < max(0.20, best * 0.45):
                continue
            key = tuple(sorted((relating.guid, other.guid)))
            current = portals.get(key)
            if current is None:
                portals[key] = {
                    "spaces": key,
                    "width_m": width,
                    "height_m": height,
                    "geometry": geometry,
                }
                continue
            current["width_m"] = max(current["width_m"], width)
            current["height_m"] = min(current["height_m"], height)
            current["geometry"] = unary_union([current["geometry"], geometry])
    return list(portals.values())


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


def _door_axis(door: Element, name: str) -> tuple[float, float] | None:
    x = _number(door.extra.get(f"{name}X"))
    y = _number(door.extra.get(f"{name}Y"))
    length = math.hypot(x or 0.0, y or 0.0)
    if x is None or y is None or length <= 1e-6:
        return None
    return x / length, y / length


def _door_axes(door: Element, polygon=None):
    width_axis = _door_axis(door, "doorWidthAxis")
    normal = _door_axis(door, "doorDepthAxis")
    if width_axis is None and normal is not None:
        width_axis = -normal[1], normal[0]
    if normal is None and width_axis is not None:
        normal = -width_axis[1], width_axis[0]
    if width_axis is None and polygon is not None and not polygon.is_empty:
        rectangle = polygon.minimum_rotated_rectangle
        if rectangle.geom_type == "Polygon":
            points = list(rectangle.exterior.coords)[:4]
            sides = [
                (math.hypot(second[0] - first[0], second[1] - first[1]), first, second)
                for first, second in zip(points, points[1:] + points[:1])
            ]
            length, first, second = max(sides, default=(0.0, None, None))
            if length > 1e-6:
                width_axis = (second[0] - first[0]) / length, (second[1] - first[1]) / length
                normal = -width_axis[1], width_axis[0]
    if width_axis is None or normal is None:
        return None
    dot = width_axis[0] * normal[0] + width_axis[1] * normal[1]
    normal = normal[0] - dot * width_axis[0], normal[1] - dot * width_axis[1]
    length = math.hypot(normal[0], normal[1])
    if length <= 1e-6:
        normal = -width_axis[1], width_axis[0]
    else:
        normal = normal[0] / length, normal[1] / length
    return width_axis, normal


def _polygon_axis_size(polygon, center, width_axis) -> tuple[float, float] | None:
    if polygon is None or polygon.is_empty:
        return None
    from shapely.affinity import rotate

    angle = -math.degrees(math.atan2(width_axis[1], width_axis[0]))
    value = rotate(polygon, angle, origin=center)
    min_x, min_y, max_x, max_y = value.bounds
    return max_x - min_x, max_y - min_y


def _polygon_axis_bounds(polygon, width_axis, normal):
    if polygon is None or polygon.is_empty:
        return None
    hull = polygon.convex_hull
    if hull.geom_type == "Polygon":
        points = list(hull.exterior.coords)
    elif hasattr(hull, "coords"):
        points = list(hull.coords)
    else:
        return None
    along = [point[0] * width_axis[0] + point[1] * width_axis[1] for point in points]
    across = [point[0] * normal[0] + point[1] * normal[1] for point in points]
    return min(along), min(across), max(along), max(across)


def _door_portal_polygon(
    door: Element,
    footprints: dict[str, object],
    normal_extension: float = 0.22,
    width_margin: float = 0.02,
):
    from shapely.geometry import Point, Polygon

    polygon = _element_polygon(door, footprints)
    axes = _door_axes(door, polygon)
    if door.center:
        center = float(door.center[0]), float(door.center[1])
    elif polygon is not None and not polygon.is_empty:
        center = polygon.centroid.x, polygon.centroid.y
    else:
        return None
    if axes is None:
        return polygon.buffer(width_margin, cap_style=2, join_style=2) if polygon is not None else Point(center).buffer(0.05)
    width_axis, normal = axes
    polygon_size = _polygon_axis_size(polygon, center, width_axis)
    width = next(
        (
            value
            for value in [
                _number(door.extra.get("doorOpeningWidthM")),
                _number(door.extra.get("doorLocalWidthM")),
                _number(door.extra.get("derivedDoorWidthM")),
                _number(door.extra.get("doorDeclaredWidthM")),
                polygon_size[0] if polygon_size else None,
            ]
            if value is not None and value > 0.05
        ),
        0.90,
    )
    depth = next(
        (
            value
            for value in [
                _number(door.extra.get("doorOpeningDepthM")),
                _number(door.extra.get("doorLocalDepthM")),
                polygon_size[1] if polygon_size else None,
            ]
            if value is not None and value > 0.01
        ),
        0.20,
    )
    host_guid = door.extra.get("doorHostGuid")
    host_polygon = footprints.get(str(host_guid)) if host_guid else None
    if host_polygon is not None and not host_polygon.is_empty:
        local = host_polygon.intersection(Point(center).buffer(max(1.25, width)))
        bounds = _polygon_axis_bounds(local, width_axis, normal)
        if bounds is not None and 0.01 < bounds[3] - bounds[1] <= 1.0:
            along = center[0] * width_axis[0] + center[1] * width_axis[1]
            across = (bounds[1] + bounds[3]) / 2
            center = (
                width_axis[0] * along + normal[0] * across,
                width_axis[1] * along + normal[1] * across,
            )
            depth = max(depth, bounds[3] - bounds[1])
    half_width = width / 2 + width_margin
    half_depth = depth / 2 + normal_extension
    points = []
    for along, across in [(-half_width, -half_depth), (half_width, -half_depth), (half_width, half_depth), (-half_width, half_depth)]:
        points.append(
            (
                center[0] + width_axis[0] * along + normal[0] * across,
                center[1] + width_axis[1] * along + normal[1] * across,
            )
        )
    return Polygon(points)


def _area_clearance_target(area) -> float:
    min_x, min_y, max_x, max_y = area.bounds
    return min(0.75, max(0.20, min(max_x - min_x, max_y - min_y) / 5))


def _door_space_approach(door: Element, space: Element, area, footprints: dict[str, object], path_area=None):
    from shapely.geometry import LineString, Point

    if area is None or area.is_empty or not door.center:
        return None
    polygon = _element_polygon(space, footprints)
    axes = _door_axes(door, _element_polygon(door, footprints))
    if polygon is None or polygon.is_empty or axes is None:
        return None
    normal = axes[1]
    center = float(door.center[0]), float(door.center[1])
    target = _area_clearance_target(polygon)
    reference = polygon.representative_point()
    directions = [(normal[0], normal[1]), (-normal[0], -normal[1])]

    def direction_key(direction):
        end = center[0] + direction[0] * target, center[1] + direction[1] * target
        inside = LineString([center, end]).intersection(polygon.buffer(PLAN_GEOMETRY_TOLERANCE)).length
        toward = direction[0] * (reference.x - center[0]) + direction[1] * (reference.y - center[1])
        return inside, toward

    direction = max(directions, key=direction_key)
    steps = max(0, math.ceil((target - 0.20) / 0.05))
    distances = [target - index * 0.05 for index in range(steps + 1)]
    if not distances or distances[-1] > 0.20 + 1e-6:
        distances.append(0.20)
    z = float(door.center[2])
    for value in distances:
        if value < 0.20 - 1e-6:
            continue
        point = center[0] + direction[0] * value, center[1] + direction[1] * value, z
        if not polygon.buffer(PLAN_GEOMETRY_TOLERANCE).covers(Point(point[0], point[1])):
            continue
        if _path_inside_area([door.center, point], area, path_area):
            return point
    return None


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
    openings = []
    for door in doors:
        portal = _door_portal_polygon(door, footprints)
        if portal is not None and not portal.is_empty:
            openings.append(portal)
    source = polygon.buffer(0)
    if openings:
        opening_area = unary_union(openings).intersection(polygon.buffer(0.75))
        source = unary_union([source, opening_area]).buffer(0)
    blockers = []
    for obstacle in obstacles:
        if obstacle.ifc_type not in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight"}:
            continue
        if not _z_overlap(space, obstacle):
            continue
        obstacle_polygon = _element_polygon(obstacle, footprints)
        if obstacle_polygon is None or obstacle_polygon.is_empty or obstacle_polygon.distance(source) > 0.05:
            continue
        blocked = obstacle_polygon
        if obstacle.ifc_type == "IfcWall":
            for door in doors:
                if not _door_opens_wall(door, obstacle, footprints):
                    continue
                portal = _door_portal_polygon(door, footprints)
                if portal is not None:
                    blocked = blocked.difference(portal)
        blockers.append(blocked)
    area = source
    if blockers:
        area = area.difference(unary_union(blockers)).buffer(0)
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
        space.issue_regions = [
            region
            for region in space.issue_regions
            if region.get("rule_id") not in {"corridor_width", "corridor_movement_area"}
        ]
        space.passing_area_gaps = []
        doors = doors_by_space.get(space.guid, [])
        area = _space_walkable_area(space, doors, obstacles, footprints)
        if area is None or area.is_empty:
            continue
        segments = _space_skeleton_segments(area)
        region = _space_clearance_region(space, area, doors, footprints, segments)
        if region is not None:
            space.extra["derivedClearSpaceWidthM"] = region["measured"]
            space.issue_regions.append(region)
        movement = _corridor_movement_area_region(space, area, segments)
        if movement is not None:
            space.extra["corridorMovementAreaMaxGapM"] = movement["measured"]
            space.extra["derivedCorridorLengthM"] = max(
                float(space.extra.get("derivedCorridorLengthM") or 0.0),
                movement["length"],
            )
            space.passing_area_gaps.extend(movement["gaps"])


def _space_clearance_region(
    space: Element,
    area,
    doors: list[Element],
    footprints: dict[str, object],
    segments=None,
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
    for start, end in segments if segments is not None else _space_skeleton_segments(area):
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


def _corridor_movement_area_region(space: Element, area, segments=None) -> dict | None:
    from shapely.geometry import LineString, mapping
    from shapely.ops import unary_union

    segments = segments if segments is not None else _space_skeleton_segments(area)
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
        for a, b in zip(points, points[1:]):
            length = math.dist(a, b)
            graph[a][b] = min(length, graph[a].get(b, math.inf))
            graph[b][a] = min(length, graph[b].get(a, math.inf))
    if not graph:
        return None

    length, _start, _end = _graph_diameter(graph, set(graph))
    side = RULE_LIMITS.corridor_movement_space_m
    boundary = area.boundary
    wide = {
        node
        for node in graph
        if _corridor_movement_square_fits(area, node, side, boundary)
    }
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
            LineString([first, second])
            for first in component
            for second in graph[first]
            if second in component and first < second
        ]
        if not lines:
            continue
        geometry = unary_union(lines).buffer(0.20, cap_style=2, join_style=2).intersection(area).buffer(0)
        if geometry.is_empty:
            continue
        anchor = geometry.representative_point()
        measured = round(gap, 4)
        key_text = f"corridor_movement_area:{space.guid}:{anchor.x:.3f}:{anchor.y:.3f}"
        region_key = hashlib.sha1(key_text.encode("utf-8")).hexdigest()[:11].upper()
        gaps.append(
            {
                "evidence_id": f"G{region_key}",
                "region_id": f"R{region_key}",
                "rule_id": "corridor_movement_area",
                "element_guid": space.guid,
                "measured": measured,
                "required": RULE_LIMITS.corridor_movement_interval_m,
                "unit": "m",
                "geometry": mapping(geometry),
                "anchor": [round(anchor.x, 4), round(anchor.y, 4), round(space.center[2] if space.center else 0.0, 4)],
                "movement_space_m": side,
                "area_count": 1,
                "areas": [],
            }
        )

    gaps.sort(key=lambda gap: (gap["anchor"][0], gap["anchor"][1], gap["evidence_id"]))
    measured = max((gap["measured"] for gap in gaps), default=0.0)
    return {"measured": measured, "length": round(length, 4), "gaps": gaps}


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
            middle = (low[0] + high[0]) / 2, (low[1] + high[1]) / 2
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
    cache: dict | None = None,
) -> None:
    for key in ["routeClearWidthM", "routeClearWidthPointX", "routeClearWidthPointY", "routeClearWidthPointZ"]:
        measurements.pop(key, None)
    value = _route_clear_width_measurement(path, area, route_doors, footprints, cache)
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
    cache: dict | None = None,
) -> tuple[float, tuple[float, float, float]] | None:
    from shapely.geometry import LineString, Point

    door_zone = _route_door_zone(route_doors, footprints)
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
            key = round(x, 4), round(y, 4), round(abs(nx), 4), round(abs(ny), 4)
            width = cache.get(key) if cache is not None else None
            if width is None:
                cross = LineString([(x - nx * span, y - ny * span), (x + nx * span, y + ny * span)]).intersection(area)
                values = [part.length for part in _line_parts(cross) if part.distance(point) <= 0.03]
                width = max(values) if values else 0.0
                if cache is not None:
                    cache[key] = width
            if width > 0:
                widths.append((width, (x, y, z)))
    if not widths:
        return None
    width, point = min(widths, key=lambda value: value[0])
    return round(width, 4), point


def _route_door_zone(
    route_doors: list[Element],
    footprints: dict[str, object],
    normal_extension: float = 0.35,
    width_margin: float = 0.05,
):
    from shapely.geometry import Point
    from shapely.ops import unary_union

    zones = []
    for door in route_doors:
        portal = _door_portal_polygon(door, footprints, normal_extension, width_margin)
        if portal is not None and not portal.is_empty:
            zones.append(portal)
        elif door.center:
            zones.append(Point(door.center[0], door.center[1]).buffer(0.45))
    return unary_union(zones) if zones else None


def _route_wall_clearance(path, area, route_doors: list[Element], footprints: dict[str, object]) -> float | None:
    from shapely.geometry import LineString

    line = LineString([(point[0], point[1]) for point in path])
    door_zone = _route_door_zone(route_doors, footprints, normal_extension=0.80, width_margin=0.10)
    value = line.difference(door_zone) if door_zone is not None else line
    if value.is_empty or value.length <= 1e-6:
        return None
    return value.distance(area.boundary)


def _door_opens_wall(door: Element, wall: Element, footprints: dict[str, object]) -> bool:
    from shapely.geometry import Point

    host_guid = door.extra.get("doorHostGuid")
    if host_guid and str(host_guid) == wall.guid:
        return True
    if not door.center:
        return False
    door_polygon = _element_polygon(door, footprints)
    wall_polygon = _element_polygon(wall, footprints)
    axes = _door_axes(door, door_polygon)
    if wall_polygon is None or wall_polygon.is_empty or axes is None:
        return False
    if door_polygon is not None and door_polygon.distance(wall_polygon) > 0.22:
        return False
    if door_polygon is None and Point(door.center[0], door.center[1]).distance(wall_polygon) > 0.22:
        return False
    local = wall_polygon.intersection(Point(door.center[0], door.center[1]).buffer(1.25))
    size = _polygon_axis_size(local, (door.center[0], door.center[1]), axes[0])
    return bool(size and size[0] >= size[1])


def _route_avoids_walls(
    path,
    space: Element,
    obstacles: list[Element],
    route_doors: list[Element],
    footprints: dict[str, object],
) -> bool:
    from shapely.geometry import LineString

    if not path or len(path) < 2:
        return False
    route = LineString([(point[0], point[1]) for point in path]).buffer(
        PLAN_ROUTE_HALF_WIDTH, cap_style=2, join_style=2
    )
    for obstacle in obstacles:
        if obstacle.ifc_type not in {"IfcWall", "IfcColumn"} or not _z_overlap(space, obstacle):
            continue
        blocked = _element_polygon(obstacle, footprints)
        if blocked is None or blocked.is_empty or blocked.distance(route) > PLAN_GEOMETRY_TOLERANCE:
            continue
        if obstacle.ifc_type == "IfcWall":
            for door in route_doors:
                if not _door_opens_wall(door, obstacle, footprints):
                    continue
                portal = _door_portal_polygon(
                    door,
                    footprints,
                    normal_extension=0.22,
                    width_margin=PLAN_ROUTE_HALF_WIDTH,
                )
                if portal is not None:
                    blocked = blocked.difference(portal)
        overlap = route.intersection(blocked)
        if not overlap.is_empty and overlap.area > 1e-5:
            return False
    return True


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
    target = _area_clearance_target(area)
    return {
        "step": step,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "allowed": allowed,
        "clearance": clearance,
        "target": target,
        "area": area,
        "span": math.hypot(width, depth) + 2.0,
        "door_cells": set(),
        "accessible_cells": {},
        "accessible_paths": {},
        "accessible_sources": set(),
        "target_cells": set(),
        "widths": {},
    }


def _path_in_area(
    first: Element,
    second: Element,
    space: Element,
    area,
    grid: dict,
    accessible: bool = False,
    path_area=None,
    first_approach=None,
    second_approach=None,
):
    z = _route_z(first, second, space)
    start = first.center[0], first.center[1], z
    end = second.center[0], second.center[1], z
    start_approach = (
        (first_approach[0], first_approach[1], z) if first_approach is not None else start
    )
    end_approach = (
        (second_approach[0], second_approach[1], z) if second_approach is not None else end
    )
    start_cell = _nearest_cell(grid, start_approach)
    end_cell = _nearest_cell(grid, end_approach)
    if start_cell is None or end_cell is None:
        return None
    cells = _astar(grid, start_cell, end_cell, accessible)
    if not cells:
        return None
    points = [_cell_point(grid, cell, z) for cell in cells]
    start_path = _endpoint_path(start, points[0], area, path_area, start_approach)
    end_path = _endpoint_path(end, points[-1], area, path_area, end_approach)
    if not start_path or not end_path:
        return None
    path = _compact_path(start_path + points[1:-1] + list(reversed(end_path)))
    return path if _path_inside_area(path, area, path_area) else None


def _astar(grid: dict, start: tuple[int, int], end: tuple[int, int], accessible: bool = False):
    if accessible:
        return _accessible_grid_path(grid, start, end)
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


def _accessible_grid_path(grid: dict, start: tuple[int, int], end: tuple[int, int]):
    if start not in grid["accessible_sources"]:
        _build_accessible_paths(grid, start)
    return grid["accessible_paths"].get((start, end))


def _build_accessible_paths(grid: dict, start: tuple[int, int]) -> None:
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    start_state = start, -1
    queue = [(0.0, start, -1)]
    costs = {start_state: 0.0}
    parents = {}
    remaining = set(grid["target_cells"])
    while queue and remaining:
        cost, cell, direction = heapq.heappop(queue)
        state = cell, direction
        if cost != costs.get(state):
            continue
        if cell in remaining:
            path = [cell]
            current = state
            while current != start_state:
                current = parents[current]
                path.append(current[0])
            path.reverse()
            grid["accessible_paths"][(start, cell)] = path
            grid["accessible_paths"][(cell, start)] = list(reversed(path))
            remaining.remove(cell)
        for next_direction, (dx, dy) in enumerate(directions):
            neighbour = cell[0] + dx, cell[1] + dy
            if neighbour not in grid["allowed"]:
                continue
            if not _accessible_grid_step(grid, cell, neighbour, direction, next_direction):
                continue
            turn_cost = 0.22 if direction >= 0 and direction != next_direction else 0.0
            shortfall = max(0.0, grid["target"] - grid["clearance"].get(neighbour, 0.0)) / grid["target"]
            next_cost = cost + grid["step"] * (1.0 + shortfall * shortfall * 2.4) + turn_cost
            next_state = neighbour, next_direction
            if next_cost >= costs.get(next_state, math.inf):
                continue
            costs[next_state] = next_cost
            parents[next_state] = state
            heapq.heappush(queue, (next_cost, neighbour, next_direction))
    grid["accessible_sources"].add(start)


def _accessible_grid_step(
    grid: dict,
    cell: tuple[int, int],
    neighbour: tuple[int, int],
    direction: int,
    next_direction: int,
) -> bool:
    if not _grid_cell_in_door_zone(grid, neighbour):
        if neighbour not in _grid_accessible_cells(grid, next_direction):
            return False
    if direction >= 0 and direction != next_direction and not _grid_cell_in_door_zone(grid, cell):
        clearance = grid["clearance"].get(cell, 0.0) * 2
        if clearance + grid["step"] < RULE_LIMITS.turning_space_m:
            return False
    return True


def _grid_cell_in_door_zone(grid: dict, cell: tuple[int, int]) -> bool:
    return cell in grid["door_cells"]


def _grid_door_cells(grid: dict, zone) -> set[tuple[int, int]]:
    from shapely.geometry import Point

    if zone is None:
        return set()
    min_x, min_y, max_x, max_y = zone.bounds
    low_x = math.floor((min_x - grid["origin_x"]) / grid["step"])
    high_x = math.ceil((max_x - grid["origin_x"]) / grid["step"])
    low_y = math.floor((min_y - grid["origin_y"]) / grid["step"])
    high_y = math.ceil((max_y - grid["origin_y"]) / grid["step"])
    result = set()
    for ix in range(max(0, low_x), min(high_x, max(cell[0] for cell in grid["allowed"])) + 1):
        for iy in range(max(0, low_y), min(high_y, max(cell[1] for cell in grid["allowed"])) + 1):
            cell = ix, iy
            if cell not in grid["allowed"]:
                continue
            x, y, _z = _cell_point(grid, cell, 0.0)
            if zone.covers(Point(x, y)):
                result.add(cell)
    return result


def _grid_accessible_cells(grid: dict, direction: int) -> set[tuple[int, int]]:
    axis = 0 if direction in {0, 1} else 1
    if axis not in grid["accessible_cells"]:
        grid["accessible_cells"][axis] = {
            cell
            for cell in grid["allowed"]
            if _grid_cross_width(grid, cell, direction) + 1e-6 >= RULE_LIMITS.corridor_width_m
        }
    return grid["accessible_cells"][axis]


def _grid_cross_width(grid: dict, cell: tuple[int, int], direction: int) -> float:
    from shapely.geometry import LineString, Point

    axis = 0 if direction in {0, 1} else 1
    key = cell, axis
    if key in grid["widths"]:
        return grid["widths"][key]
    x, y, _z = _cell_point(grid, cell, 0.0)
    span = grid["span"]
    line = LineString([(x, y - span), (x, y + span)]) if axis == 0 else LineString([(x - span, y), (x + span, y)])
    point = Point(x, y)
    values = [part.length for part in _line_parts(line.intersection(grid["area"])) if part.distance(point) <= 0.03]
    width = max(values, default=0.0)
    grid["widths"][key] = width
    return width


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


def _oriented_path(path, first: Element, second: Element):
    if not path or not first.center or not second.center:
        return path
    forward = math.hypot(path[0][0] - first.center[0], path[0][1] - first.center[1]) + math.hypot(
        path[-1][0] - second.center[0], path[-1][1] - second.center[1]
    )
    reverse = math.hypot(path[-1][0] - first.center[0], path[-1][1] - first.center[1]) + math.hypot(
        path[0][0] - second.center[0], path[0][1] - second.center[1]
    )
    return list(reversed(path)) if reverse < forward else list(path)


def _path_uses_door_approach(path, first: Element, second: Element, first_approach, second_approach) -> bool:
    value = _oriented_path(path, first, second)
    return _path_leaves_door(value, first, first_approach) and _path_leaves_door(
        list(reversed(value)), second, second_approach
    )


def _path_leaves_door(path, door: Element, approach) -> bool:
    if approach is None or not door.center:
        return True
    center = door.center
    if math.hypot(path[0][0] - center[0], path[0][1] - center[1]) > 0.12:
        return False
    dx = approach[0] - center[0]
    dy = approach[1] - center[1]
    required = math.hypot(dx, dy)
    if required <= 0.05:
        return True
    nx = dx / required
    ny = dy / required
    for point in path[1:]:
        px = point[0] - center[0]
        py = point[1] - center[1]
        length = math.hypot(px, py)
        if length <= 0.03:
            continue
        along = px * nx + py * ny
        across = abs(px * ny - py * nx)
        return along + PLAN_GRID_STEP / 2 >= required and across <= 0.05
    return False


def _endpoint_path(endpoint, grid_point, area, path_area=None, approach=None):
    anchor = approach or endpoint
    prefix = [endpoint]
    if distance(endpoint, anchor) > 1e-8:
        prefix.append(anchor)
    candidates = [
        prefix + [(grid_point[0], anchor[1], endpoint[2]), grid_point],
        prefix + [(anchor[0], grid_point[1], endpoint[2]), grid_point],
        prefix + [grid_point],
    ]
    valid = [_compact_path(candidate) for candidate in candidates if _path_inside_area(candidate, area, path_area)]
    return min(valid, key=lambda value: (_diagonal_segment_count(value), _path_length(value))) if valid else None


def _diagonal_segment_count(path) -> int:
    return sum(
        1
        for first, second in zip(path, path[1:])
        if abs(first[0] - second[0]) > 0.03 and abs(first[1] - second[1]) > 0.03
    )


def _route_path_area(area):
    value = area.buffer(PLAN_GEOMETRY_TOLERANCE).buffer(-PLAN_ROUTE_HALF_WIDTH, join_style=2)
    return value if not value.is_empty else area.buffer(PLAN_GEOMETRY_TOLERANCE)


def _path_inside_area(path, area, path_area=None) -> bool:
    from shapely.geometry import LineString

    if not path or len(path) < 2:
        return False
    line = LineString([(point[0], point[1]) for point in path])
    if path_area is not None and path_area.covers(line):
        return True
    outside = line.buffer(PLAN_ROUTE_HALF_WIDTH, cap_style=2, join_style=2).difference(
        area.buffer(PLAN_GEOMETRY_TOLERANCE)
    )
    return outside.is_empty or outside.area <= 1e-5


def _simplify_path(path, area, path_area=None):
    points = _compact_path(path)
    if len(points) <= 2:
        return points
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        replacement = None
        while next_index > index:
            replacement = _rectilinear_shortcut(points[index : next_index + 1], area, path_area)
            if replacement:
                break
            next_index -= 1
        if not replacement:
            next_index = index + 1
            replacement = [points[index], points[next_index]]
        if distance(result[-1], replacement[0]) <= 1e-8:
            result.extend(replacement[1:])
        else:
            result.extend(replacement)
        index = next_index
    return _compact_path(result)


def _rectilinear_shortcut(points, area, path_area=None):
    if len(points) < 2:
        return None
    start = points[0]
    end = points[-1]
    z = (start[2] + end[2]) / 2
    original_length = _path_length(points)
    candidates = []

    def add(values):
        candidate = _compact_path(values)
        length = _path_length(candidate)
        if (
            len(candidate) < 2
            or length > original_length * 1.05 + 0.01
            or not _path_inside_area(candidate, area, path_area)
        ):
            return
        candidates.append((length + max(0, len(candidate) - 2) * 0.35, len(candidate), candidate))

    if abs(start[0] - end[0]) <= 0.03 or abs(start[1] - end[1]) <= 0.03:
        add([start, end])
    add([start, (end[0], start[1], z), end])
    add([start, (start[0], end[1], z), end])
    for y in sorted({round(point[1], 6) for point in points}):
        add([start, (start[0], y, z), (end[0], y, z), end])
    for x in sorted({round(point[0], 6) for point in points}):
        add([start, (x, start[1], z), (x, end[1], z), end])
    return min(candidates, key=lambda value: (value[0], value[1]))[2] if candidates else None


def _set_route_turning_space(measurements: dict, path, area, cache: dict | None = None, path_area=None) -> None:
    for key in [
        "routeTurningSpaceM",
        "routeTurningPointX",
        "routeTurningPointY",
        "routeTurningPointZ",
        "routeRequiredTurnCount",
    ]:
        measurements.pop(key, None)
    required_path = _required_turn_path(path, area, path_area)
    turn_points = _route_turn_points(required_path)
    measurements["routeHasTurn"] = bool(turn_points)
    measurements["routeRequiredTurnCount"] = len(turn_points)
    turning_space = _route_turning_space_measurement(turn_points, area, cache)
    if turning_space is None:
        return
    side, point = turning_space
    measurements["routeTurningSpaceM"] = side
    measurements["routeTurningPointX"] = round(point[0], 4)
    measurements["routeTurningPointY"] = round(point[1], 4)
    measurements["routeTurningPointZ"] = round(point[2], 4)


def _required_turn_path(path, area, path_area=None):
    points = _compact_path(path)
    if len(points) <= 2:
        return points
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        while next_index > index + 1 and not _path_inside_area(
            [points[index], points[next_index]], area, path_area
        ):
            next_index -= 1
        result.append(points[next_index])
        index = next_index
    return _compact_path(result)


def _route_turn_points(path):
    result = []
    for first, middle, last in zip(path, path[1:], path[2:]):
        a = middle[0] - first[0], middle[1] - first[1]
        b = last[0] - middle[0], last[1] - middle[1]
        first_length = math.hypot(*a)
        second_length = math.hypot(*b)
        scale = first_length * second_length
        if min(first_length, second_length) < 0.35 or scale <= 1e-6:
            continue
        if abs(a[0] * b[1] - a[1] * b[0]) / scale > 0.15:
            result.append(middle)
    return result


def _route_turning_space_measurement(turn_points, area, cache: dict | None = None):
    from shapely.geometry import Point

    values = []
    for middle in turn_points:
        key = round(middle[0], 3), round(middle[1], 3)
        if cache is not None and key in cache:
            side = cache[key]
            if side is not None:
                values.append((side, middle))
            continue
        point = Point(middle[0], middle[1])
        polygons = [value for value in _polygon_parts(area) if not value.is_empty and value.buffer(0.03).covers(point)]
        if not polygons:
            if cache is not None:
                cache[key] = None
            continue
        polygon = max(polygons, key=lambda value: value.area)
        side = _turning_square_side(polygon, point)
        if cache is not None:
            cache[key] = side
        values.append((side, middle))
    if not values:
        return None
    side, point = min(values, key=lambda value: value[0])
    return round(side, 4), point


def _turning_square_side(area, point, limit: float | None = None) -> float:
    from shapely.affinity import rotate

    limit = limit or RULE_LIMITS.turning_space_m
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


def _stair_markers(route_edges: list[RouteEdge]) -> list[RouteEdge]:
    result = []
    for edge in route_edges:
        if "stair_block" not in edge.reasons and not edge.measurements.get("routeHitsStair"):
            continue
        measurements = dict(edge.measurements)
        measurements["planNetworkRole"] = "issue"
        measurements["planMarkerOnly"] = True
        result.append(
            replace(
                edge,
                edge_id=_plan_edge_id(edge.edge_id, edge.start_guid, "stair"),
                status="fail",
                reasons=["stair_block"],
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
    ramp_run_length = _number(measurements.get("routeRampRunLengthM"))
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
    if ramp_run_length is not None and ramp_run_length > RULE_LIMITS.ramp_run_length_m:
        reasons.append("ramp_run_length")
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
