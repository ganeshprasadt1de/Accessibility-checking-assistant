from __future__ import annotations

import hashlib
import heapq
import json
import math
import shutil
import threading
import zlib
from collections import OrderedDict, deque
from pathlib import Path

import numpy as np
from shapely import intersects_xy
from shapely.geometry import shape

from .config import RULE_LIMITS


FORMAT_VERSION = 1
RESOLUTION_M = 0.01
TILE_SIZE_M = 5.0
WHEELCHAIR_CLEARANCE_M = 0.90
WHEELCHAIR_RADIUS_M = WHEELCHAIR_CLEARANCE_M / 2
ACCESSIBLE_ROUTE_WIDTH_M = 1.50
GEOMETRY_TOLERANCE_M = RESOLUTION_M / 2
TILE_CELLS = round(TILE_SIZE_M / RESOLUTION_M)
TILE_CACHE_LIMIT = 64

WALL_TYPES = {"IfcWall"}
HARD_OBSTACLE_TYPES = {"IfcColumn", "IfcStair", "IfcStairFlight"}
RAMP_TYPES = {"IfcRamp", "IfcRampFlight"}


class NavigationError(RuntimeError):
    pass


def build_navigation_package(app_data_path: Path, output_dir: Path) -> dict:
    """Build a tiled, versioned point-routing package from the generated app data."""
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    elements_by_guid = {item.get("guid"): item for item in data.get("elements", []) if item.get("guid")}
    route_edges_by_id = {item.get("edgeId"): item for item in data.get("routeEdges", []) if item.get("edgeId")}
    temporary = output_dir / "navigation.tmp"
    target = output_dir / "navigation"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)

    index = {
        "formatVersion": FORMAT_VERSION,
        "resolutionM": RESOLUTION_M,
        "tileSizeM": TILE_SIZE_M,
        "tileCells": TILE_CELLS,
        "wheelchairClearanceM": WHEELCHAIR_CLEARANCE_M,
        "accessibleRouteWidthM": ACCESSIBLE_ROUTE_WIDTH_M,
        "geometryToleranceM": GEOMETRY_TOLERANCE_M,
        "floors": {},
    }
    try:
        for floor in data.get("floors", []):
            floor_guids = set(floor.get("elementGuids", []))
            floor_surface = _floor_surface_elevation(floor, elements_by_guid)
            clearance_min_z = floor_surface
            clearance_max_z = floor_surface + RULE_LIMITS.clearance_height_m
            for guid, item in elements_by_guid.items():
                if (item.get("ifcType") in WALL_TYPES or _hard_obstacle(item)) and _intersects_height_band(
                    item,
                    clearance_min_z,
                    clearance_max_z,
                ):
                    floor_guids.add(guid)
            for edge_id in floor.get("routeEdgeIds", []):
                edge = route_edges_by_id.get(edge_id) or {}
                for endpoint in (edge.get("startGuid"), edge.get("endGuid")):
                    item = elements_by_guid.get(endpoint)
                    if item and (_hard_obstacle(item) or item.get("ifcType") in RAMP_TYPES):
                        floor_guids.add(endpoint)
            floor_elements = [elements_by_guid[guid] for guid in sorted(floor_guids) if guid in elements_by_guid]
            floor_issue_regions = [
                region
                for region in data.get("issueRegions", [])
                if region.get("element_guid") in floor_guids
            ]
            floor_index = _build_floor_package(temporary, floor, floor_elements, floor_issue_regions)
            if floor_index:
                index["floors"][floor["name"]] = floor_index
        (temporary / "index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return index


def _build_floor_package(root: Path, floor: dict, elements: list[dict], issue_regions: list[dict]) -> dict | None:
    spaces = [item for item in elements if _accessible_space(item)]
    local_restrictions = _corridor_issue_shapes(issue_regions)
    local_restriction_guids = {
        region.get("element_guid")
        for region in issue_regions
        if region.get("rule_id") == "corridor_width" and region.get("geometry")
    }
    restricted_spaces = [
        item
        for item in elements
        if _restricted_space(item) and item.get("guid") not in local_restriction_guids
    ]
    if not spaces:
        return None
    accessible_doors = [item for item in elements if _accessible_door(item)]
    walls = [item for item in elements if item.get("ifcType") in WALL_TYPES and _has_plan_box(item)]
    hard = [item for item in elements if _hard_obstacle(item)]

    wall_rects, portal_rects = _split_walls_and_portals(walls, accessible_doors)
    walkable_rects = [_rect(item) for item in spaces] + portal_rects
    clearance = WHEELCHAIR_RADIUS_M - GEOMETRY_TOLERANCE_M
    blocked_rects = [_inflate(rect, clearance) for rect in wall_rects]
    blocked_rects.extend(_inflate(_rect(item), clearance) for item in hard)
    blocked_rects.extend(_rect(item) for item in restricted_spaces)

    bounds_rects = walkable_rects or [_rect(item) for item in spaces]
    min_x = math.floor(min(rect[0] for rect in bounds_rects) / RESOLUTION_M) * RESOLUTION_M
    min_y = math.floor(min(rect[1] for rect in bounds_rects) / RESOLUTION_M) * RESOLUTION_M
    max_x = math.ceil(max(rect[2] for rect in bounds_rects) / RESOLUTION_M) * RESOLUTION_M
    max_y = math.ceil(max(rect[3] for rect in bounds_rects) / RESOLUTION_M) * RESOLUTION_M
    nx = max(1, math.ceil((max_x - min_x) / RESOLUTION_M))
    ny = max(1, math.ceil((max_y - min_y) / RESOLUTION_M))
    tiles_x = math.ceil(nx / TILE_CELLS)
    tiles_y = math.ceil(ny / TILE_CELLS)
    floor_key = hashlib.sha256(floor["name"].encode("utf-8")).hexdigest()[:16]
    floor_dir = root / "tiles" / floor_key
    floor_dir.mkdir(parents=True)

    tiles: dict[str, dict] = {}
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            width = min(TILE_CELLS, nx - tx * TILE_CELLS)
            height = min(TILE_CELLS, ny - ty * TILE_CELLS)
            tile = _raster_tile(min_x, min_y, tx, ty, width, height, walkable_rects, blocked_rects, local_restrictions)
            if not tile.any():
                continue
            packed = np.packbits(tile.reshape(-1), bitorder="little").tobytes()
            compressed = zlib.compress(packed, level=9)
            relative = f"tiles/{floor_key}/{tx}_{ty}.nav"
            (root / relative).write_bytes(compressed)
            components = _tile_components(tile)
            tiles[f"{tx},{ty}"] = {
                "x": tx,
                "y": ty,
                "width": width,
                "height": height,
                "file": relative,
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "components": components,
            }

    graph = _component_graph(tiles)
    return {
        "name": floor["name"],
        "key": floor_key,
        "origin": [min_x, min_y],
        "size": [nx, ny],
        "tiles": [tiles_x, tiles_y],
        "elevation": float(floor.get("elevation")) if floor.get("elevation") is not None else 0.0,
        "tileIndex": tiles,
        "componentGraph": graph,
        "blockedRects": [list(rect) for rect in blocked_rects],
        "localBlockedRegionCount": len(local_restrictions),
        "accessibleDoorGuids": [item["guid"] for item in accessible_doors],
        "accessibleRouteWidthM": ACCESSIBLE_ROUTE_WIDTH_M,
    }


def _floor_surface_elevation(floor: dict, elements_by_guid: dict[str, dict]) -> float:
    """Find the floor surface used by the vertical clearance volume."""
    elevations = []
    for guid in floor.get("doorGuids", []):
        item = elements_by_guid.get(guid) or {}
        bbox_min = item.get("bboxMin")
        if bbox_min and len(bbox_min) >= 3:
            elevations.append(float(bbox_min[2]))
    if elevations:
        elevations.sort()
        middle = len(elevations) // 2
        if len(elevations) % 2:
            return elevations[middle]
        return (elevations[middle - 1] + elevations[middle]) / 2
    elevation = _number(floor.get("elevation"))
    return elevation if elevation is not None else 0.0


def _intersects_height_band(item: dict, minimum: float, maximum: float) -> bool:
    bbox_min = item.get("bboxMin")
    bbox_max = item.get("bboxMax")
    return bool(
        bbox_min
        and bbox_max
        and len(bbox_min) >= 3
        and len(bbox_max) >= 3
        and float(bbox_min[2]) <= maximum
        and float(bbox_max[2]) >= minimum
    )


def _accessible_space(item: dict) -> bool:
    return item.get("ifcType") == "IfcSpace" and _has_plan_box(item)


def _restricted_space(item: dict) -> bool:
    if item.get("ifcType") != "IfcSpace" or not _has_plan_box(item):
        return False
    clear_width = _number((item.get("extra") or {}).get("derivedClearSpaceWidthM"))
    return clear_width is not None and clear_width + GEOMETRY_TOLERANCE_M < ACCESSIBLE_ROUTE_WIDTH_M


def _raster_tile(
    origin_x: float,
    origin_y: float,
    tx: int,
    ty: int,
    width: int,
    height: int,
    walkable_rects: list[tuple[float, float, float, float]],
    blocked_rects: list[tuple[float, float, float, float]],
    blocked_shapes: list,
) -> np.ndarray:
    tile = np.zeros((height, width), dtype=np.bool_)
    cell_x = tx * TILE_CELLS
    cell_y = ty * TILE_CELLS
    for rect in walkable_rects:
        selection = _rect_slice(rect, origin_x, origin_y, cell_x, cell_y, width, height)
        if selection:
            tile[selection] = True
    for rect in blocked_rects:
        selection = _rect_slice(rect, origin_x, origin_y, cell_x, cell_y, width, height)
        if selection:
            tile[selection] = False
    for geometry in blocked_shapes:
        _raster_blocked_shape(tile, geometry, origin_x, origin_y, cell_x, cell_y, width, height)
    return tile


def _corridor_issue_shapes(issue_regions: list[dict]) -> list:
    result = []
    for region in issue_regions:
        if region.get("rule_id") != "corridor_width" or not region.get("geometry"):
            continue
        geometry = shape(region["geometry"])
        geometry = geometry if geometry.is_valid else geometry.buffer(0)
        if geometry.is_empty:
            continue
        if geometry.geom_type == "Polygon":
            result.append(geometry)
        else:
            result.extend(part for part in geometry.geoms if part.geom_type == "Polygon" and not part.is_empty)
    return result


def _raster_blocked_shape(tile, geometry, origin_x, origin_y, cell_x, cell_y, width, height):
    selection = _rect_slice(geometry.bounds, origin_x, origin_y, cell_x, cell_y, width, height)
    if not selection:
        return
    y_slice, x_slice = selection
    local_x = np.arange(x_slice.start, x_slice.stop)
    local_y = np.arange(y_slice.start, y_slice.stop)
    xs = origin_x + (cell_x + local_x + 0.5) * RESOLUTION_M
    ys = origin_y + (cell_y + local_y + 0.5) * RESOLUTION_M
    grid_x, grid_y = np.meshgrid(xs, ys)
    blocked = intersects_xy(geometry, grid_x, grid_y)
    tile[y_slice, x_slice][blocked] = False


def _rect_slice(rect, origin_x, origin_y, cell_x, cell_y, width, height):
    min_x, min_y, max_x, max_y = rect
    global_x0 = math.ceil((min_x - origin_x) / RESOLUTION_M - 0.5 - 1e-9)
    global_x1 = math.floor((max_x - origin_x) / RESOLUTION_M - 0.5 + 1e-9)
    global_y0 = math.ceil((min_y - origin_y) / RESOLUTION_M - 0.5 - 1e-9)
    global_y1 = math.floor((max_y - origin_y) / RESOLUTION_M - 0.5 + 1e-9)
    x0 = max(0, global_x0 - cell_x)
    x1 = min(width - 1, global_x1 - cell_x)
    y0 = max(0, global_y0 - cell_y)
    y1 = min(height - 1, global_y1 - cell_y)
    if x0 > x1 or y0 > y1:
        return None
    return np.s_[y0 : y1 + 1, x0 : x1 + 1]


def _tile_components(tile: np.ndarray) -> list[dict]:
    """Connected components using scanline runs, avoiding one Python object per cell."""
    parent: list[int] = []
    samples: list[tuple[int, int]] = []
    rows: list[list[tuple[int, int, int]]] = []
    previous: list[tuple[int, int, int]] = []

    def make_label(sample):
        label = len(parent)
        parent.append(label)
        samples.append(sample)
        return label

    def find(label):
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for y, row in enumerate(tile):
        padded = np.concatenate(([False], row, [False])).astype(np.int8)
        changes = np.diff(padded)
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1) - 1
        current: list[tuple[int, int, int]] = []
        p = 0
        for start, end in zip(starts.tolist(), ends.tolist()):
            while p < len(previous) and previous[p][1] < start:
                p += 1
            overlaps = []
            q = p
            while q < len(previous) and previous[q][0] <= end:
                overlaps.append(previous[q][2])
                q += 1
            label = overlaps[0] if overlaps else make_label((start, y))
            for other in overlaps[1:]:
                union(label, other)
            current.append((start, end, label))
        rows.append(current)
        previous = current

    roots = sorted({find(label) for row in rows for _start, _end, label in row})
    compact = {root: index for index, root in enumerate(roots)}
    components = [{"id": index, "sample": None, "boundaries": {"west": [], "east": [], "south": [], "north": []}} for index in range(len(roots))]
    side_cells: dict[tuple[int, str], list[int]] = {}
    height, width = tile.shape
    for y, row in enumerate(rows):
        for start, end, label in row:
            component = compact[find(label)]
            if components[component]["sample"] is None:
                components[component]["sample"] = [start, y]
            if y == 0:
                components[component]["boundaries"]["south"].append([start, end])
            if y == height - 1:
                components[component]["boundaries"]["north"].append([start, end])
            if start == 0:
                side_cells.setdefault((component, "west"), []).append(y)
            if end == width - 1:
                side_cells.setdefault((component, "east"), []).append(y)
    for (component, side), values in side_cells.items():
        components[component]["boundaries"][side] = _compress_values(values)
    return components


def _compress_values(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    result = []
    start = previous = values[0]
    for value in values[1:]:
        if value != previous + 1:
            result.append([start, previous])
            start = value
        previous = value
    result.append([start, previous])
    return result


def _component_graph(tiles: dict[str, dict]) -> dict[str, list[str]]:
    graph: dict[str, set[str]] = {}
    for key, tile in tiles.items():
        for component in tile["components"]:
            graph.setdefault(_node(key, component["id"]), set())
    for key, tile in tiles.items():
        tx, ty = tile["x"], tile["y"]
        east = tiles.get(f"{tx + 1},{ty}")
        north = tiles.get(f"{tx},{ty + 1}")
        if east:
            _connect_boundaries(graph, key, tile, "east", f"{tx + 1},{ty}", east, "west")
        if north:
            _connect_boundaries(graph, key, tile, "north", f"{tx},{ty + 1}", north, "south")
    return {node: sorted(neighbours) for node, neighbours in graph.items()}


def _connect_boundaries(graph, key_a, tile_a, side_a, key_b, tile_b, side_b):
    for component_a in tile_a["components"]:
        for component_b in tile_b["components"]:
            if _intervals_overlap(component_a["boundaries"][side_a], component_b["boundaries"][side_b]):
                node_a = _node(key_a, component_a["id"])
                node_b = _node(key_b, component_b["id"])
                graph[node_a].add(node_b)
                graph[node_b].add(node_a)


def _intervals_overlap(a, b) -> bool:
    i = j = 0
    while i < len(a) and j < len(b):
        if max(a[i][0], b[j][0]) <= min(a[i][1], b[j][1]):
            return True
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return False


def _node(tile_key: str, component_id: int) -> str:
    return f"{tile_key}:{component_id}"


def _accessible_door(item: dict) -> bool:
    if item.get("ifcType") != "IfcDoor" or not _has_plan_box(item) or not item.get("center"):
        return False
    width = _number((item.get("extra") or {}).get("derivedDoorWidthM"))
    if width is None:
        width = _number(item.get("width"))
    return width is not None and width + GEOMETRY_TOLERANCE_M >= WHEELCHAIR_CLEARANCE_M


def _hard_obstacle(item: dict) -> bool:
    if not _has_plan_box(item):
        return False
    kind = item.get("ifcType")
    if kind in HARD_OBSTACLE_TYPES:
        return True
    if kind not in RAMP_TYPES:
        return False
    extra = item.get("extra") or {}
    slope = _number(extra.get("rampSlopePercent"))
    width = _number(extra.get("rampUsableWidthM"))
    return slope is None or width is None or slope > 6.0 + 1e-9 or width + GEOMETRY_TOLERANCE_M < 1.20


def _split_walls_and_portals(walls: list[dict], doors: list[dict]):
    wall_rects: list[tuple[float, float, float, float]] = []
    portal_rects: list[tuple[float, float, float, float]] = []
    doors_used: set[str] = set()
    for wall in walls:
        wx0, wy0, wx1, wy1 = _rect(wall)
        along_x = (wx1 - wx0) >= (wy1 - wy0)
        openings = []
        for door in doors:
            door_rect = _rect(door)
            if not _boxes_touch(_rect(wall), door_rect, 0.22):
                continue
            if along_x:
                opening = (max(wx0, door_rect[0]), min(wx1, door_rect[2]))
                portal = (opening[0], wy0 - WHEELCHAIR_RADIUS_M, opening[1], wy1 + WHEELCHAIR_RADIUS_M)
            else:
                opening = (max(wy0, door_rect[1]), min(wy1, door_rect[3]))
                portal = (wx0 - WHEELCHAIR_RADIUS_M, opening[0], wx1 + WHEELCHAIR_RADIUS_M, opening[1])
            if opening[1] - opening[0] <= GEOMETRY_TOLERANCE_M:
                continue
            openings.append(opening)
            portal_rects.append(portal)
            doors_used.add(door["guid"])
        merged = _merge_intervals([opening for opening in openings if opening[1] > opening[0]])
        start, end = (wx0, wx1) if along_x else (wy0, wy1)
        cursor = start
        for opening_start, opening_end in [*merged, (end, end)]:
            if opening_start > cursor:
                wall_rects.append((cursor, wy0, opening_start, wy1) if along_x else (wx0, cursor, wx1, opening_start))
            cursor = max(cursor, opening_end)
    for door in doors:
        if door["guid"] in doors_used:
            continue
        x0, y0, x1, y1 = _rect(door)
        portal_rects.append((x0 - 0.25, y0 - 0.25, x1 + 0.25, y1 + 0.25))
    return wall_rects, portal_rects


def _merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _boxes_touch(a, b, tolerance):
    return a[0] - tolerance <= b[2] and a[2] + tolerance >= b[0] and a[1] - tolerance <= b[3] and a[3] + tolerance >= b[1]


def _has_plan_box(item: dict) -> bool:
    return bool(item.get("bboxMin") and item.get("bboxMax") and len(item["bboxMin"]) >= 2 and len(item["bboxMax"]) >= 2)


def _rect(item: dict):
    return (float(item["bboxMin"][0]), float(item["bboxMin"][1]), float(item["bboxMax"][0]), float(item["bboxMax"][1]))


def _inflate(rect, amount):
    return (rect[0] - amount, rect[1] - amount, rect[2] + amount, rect[3] + amount)


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class NavigationPackage:
    def __init__(self, package_dir: Path):
        self.root = package_dir / "navigation"
        index_path = self.root / "index.json"
        if not index_path.exists():
            raise NavigationError("Point-navigation data is missing. Regenerate this model.")
        self.index = json.loads(index_path.read_text(encoding="utf-8"))
        if self.index.get("formatVersion") != FORMAT_VERSION:
            raise NavigationError("Point-navigation data has an unsupported format. Regenerate this model.")
        self.tile_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self.tile_cache_lock = threading.Lock()
        self.request_state = threading.local()

    def route(self, floor_name: str, start, end) -> dict:
        self.request_state.touched = set()
        floor = self.index.get("floors", {}).get(floor_name)
        if not floor:
            return _blocked("floor_unavailable", "This floor has no point-navigation package.")
        start_point = _valid_point(start, "start")
        end_point = _valid_point(end, "destination")
        start_cell = self._point_cell(floor, start_point)
        end_cell = self._point_cell(floor, end_point)
        if start_cell is None or not self._walkable(floor, *start_cell):
            return _blocked("start_not_walkable", "The start point is outside the accessible walking area or inside an obstacle.")
        if end_cell is None or not self._walkable(floor, *end_cell):
            return self._blocked_candidate(
                floor,
                start_point,
                end_point,
                "destination_not_walkable",
                "The destination point is outside the accessible walking area or inside an obstacle.",
            )

        start_node = self._cell_component(floor, start_cell)
        end_node = self._cell_component(floor, end_cell)
        if start_node is None or end_node is None:
            raise NavigationError("A walkable cell was not assigned to its precomputed tile component.")

        # An accessible grid is undirected, but equal-cost A* choices can depend on
        # which endpoint is searched first. Normalize connected queries to one
        # endpoint order, then reverse the finished path when the caller requested
        # the opposite direction. This keeps both geometry and distance symmetric.
        reverse_result = (end_cell, end_point) < (start_cell, start_point)
        search_start_point, search_end_point = (end_point, start_point) if reverse_result else (start_point, end_point)
        search_start_cell, search_end_cell = (end_cell, start_cell) if reverse_result else (start_cell, end_cell)
        search_start_node, search_end_node = (end_node, start_node) if reverse_result else (start_node, end_node)

        component_path = self._component_path(floor, search_start_node, search_end_node)
        if not component_path:
            return self._blocked_candidate(
                floor,
                start_point,
                end_point,
                "no_accessible_connection",
                "No wheelchair-accessible connection exists between the selected points.",
            )
        allowed_tiles = {node.rsplit(":", 1)[0] for node in component_path}
        cells, visited = self._astar_cells(floor, search_start_cell, search_end_cell, allowed_tiles)
        if not cells:
            raise NavigationError("The component graph reported a connection, but detailed routing could not reproduce it.")
        path = self._shorten_orthogonal_path(
            floor,
            self._exact_path(floor, search_start_point, search_end_point, cells),
        )
        if reverse_result:
            path = list(reversed(path))
        audit = self._audit_path(floor, path, start_point, end_point)
        if not all(audit.values()):
            raise NavigationError(f"Generated point route failed final validation: {audit}.")
        distance = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
        return {
            "status": "pass",
            "reason": "accessible_route_found",
            "floor": floor_name,
            "start": list(start_point),
            "end": list(end_point),
            "path": [[point[0], point[1], float(floor.get("elevation", 0))] for point in path],
            "distanceM": round(distance, 4),
            "resolutionM": RESOLUTION_M,
            "clearanceM": WHEELCHAIR_CLEARANCE_M,
            "routeWidthM": ACCESSIBLE_ROUTE_WIDTH_M,
            "streamedTiles": len(self.request_state.touched),
            "visitedCells": visited,
            "audit": audit,
        }

    def _blocked_candidate(self, floor, start, end, reason, message):
        start_cell = self._point_cell(floor, start)
        start_node = self._cell_component(floor, start_cell)
        reachable_nodes = self._reachable_component_nodes(floor, start_node)
        target = self._nearest_reachable_cell(floor, reachable_nodes, end)
        if target is None:
            raise NavigationError("A walkable start cell had no reachable candidate cell.")
        target_cell, target_node = target
        component_path = self._component_path(floor, start_node, target_node)
        if not component_path:
            raise NavigationError("The nearest candidate cell was not connected to the start component.")
        allowed_tiles = {node.rsplit(":", 1)[0] for node in component_path}
        cells, _visited = self._astar_cells(floor, start_cell, target_cell, allowed_tiles)
        if not cells:
            raise NavigationError("Detailed routing could not reach the nearest candidate cell.")
        target_point = self._cell_point(floor, target_cell)
        base_path = self._exact_path(floor, start, target_point, cells)
        tails = [
            self._walkable_prefix(floor, [target_point, (end[0], target_point[1]), end]),
            self._walkable_prefix(floor, [target_point, (target_point[0], end[1]), end]),
        ]
        tail = min(
            tails,
            key=lambda points: (
                math.hypot(points[-1][0] - end[0], points[-1][1] - end[1]),
                -_path_distance(points),
            ),
        )
        path = _dedupe([*base_path[:-1], *tail])
        reaches_destination = math.hypot(path[-1][0] - end[0], path[-1][1] - end[1]) <= 1e-9
        if reaches_destination:
            raise NavigationError("A blocked route had a complete collision-free orthogonal candidate.")
        simplified = _simplify(path)
        audit = {
            "endpointsExact": False,
            "orthogonal": all(abs(a[0] - b[0]) <= 1e-8 or abs(a[1] - b[1]) <= 1e-8 for a, b in zip(simplified, simplified[1:])),
            "collisionFree": self._path_walkable(floor, simplified),
            "reachesDestination": False,
        }
        if not audit["orthogonal"] or not audit["collisionFree"]:
            raise NavigationError(f"Blocked candidate failed final validation: {audit}.")
        return {
            "status": "blocked",
            "reason": reason,
            "message": message,
            "floor": floor["name"],
            "start": list(start),
            "end": list(end),
            "path": [[point[0], point[1], float(floor.get("elevation", 0))] for point in simplified],
            "distanceM": round(_path_distance(simplified), 4),
            "resolutionM": RESOLUTION_M,
            "clearanceM": WHEELCHAIR_CLEARANCE_M,
            "routeWidthM": ACCESSIBLE_ROUTE_WIDTH_M,
            "streamedTiles": len(self.request_state.touched),
            "audit": audit,
        }

    def _reachable_component_nodes(self, floor, start_node):
        if start_node is None:
            raise NavigationError("A walkable start cell had no tile component.")
        graph = floor["componentGraph"]
        queue = deque([start_node])
        seen = {start_node}
        while queue:
            current = queue.popleft()
            for neighbour in graph.get(current, []):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                queue.append(neighbour)
        return seen

    def _nearest_reachable_cell(self, floor, reachable_nodes, point):
        tiles = []
        for tile_key, metadata in floor["tileIndex"].items():
            component_ids = {
                component["id"]
                for component in metadata["components"]
                if _node(tile_key, component["id"]) in reachable_nodes
            }
            if not component_ids:
                continue
            tiles.append((self._tile_point_distance(floor, tile_key, metadata, point), tile_key, metadata, component_ids))
        tiles.sort(key=lambda item: (item[0], item[1]))
        best = None
        best_distance = math.inf
        for lower_bound, tile_key, metadata, component_ids in tiles:
            if lower_bound > best_distance + 1e-12:
                break
            candidate = self._nearest_reachable_cell_in_tile(floor, tile_key, metadata, component_ids, point)
            if candidate is None:
                continue
            cell, node, distance = candidate
            candidate_key = (distance, cell[1], cell[0], node)
            best_key = None if best is None else (best_distance, best[0][1], best[0][0], best[1])
            if best_key is None or candidate_key < best_key:
                best = (cell, node)
                best_distance = distance
        return best

    @staticmethod
    def _tile_point_distance(floor, tile_key, metadata, point):
        tx, ty = (int(value) for value in tile_key.split(","))
        gx0 = tx * TILE_CELLS
        gy0 = ty * TILE_CELLS
        min_x = floor["origin"][0] + (gx0 + 0.5) * RESOLUTION_M
        min_y = floor["origin"][1] + (gy0 + 0.5) * RESOLUTION_M
        max_x = floor["origin"][0] + (gx0 + metadata["width"] - 0.5) * RESOLUTION_M
        max_y = floor["origin"][1] + (gy0 + metadata["height"] - 0.5) * RESOLUTION_M
        dx = max(min_x - point[0], 0.0, point[0] - max_x)
        dy = max(min_y - point[1], 0.0, point[1] - max_y)
        return math.hypot(dx, dy)

    def _nearest_reachable_cell_in_tile(self, floor, tile_key, metadata, component_ids, point):
        tile = self._load_tile(floor, tile_key)
        if tile is None:
            return None
        all_ids = {component["id"] for component in metadata["components"]}
        if component_ids == all_ids:
            mask = tile
        else:
            mask = np.zeros(tile.shape, dtype=np.bool_)
            for component in metadata["components"]:
                if component["id"] not in component_ids:
                    continue
                sample = tuple(component["sample"])
                queue = deque([sample])
                mask[sample[1], sample[0]] = True
                while queue:
                    x, y = queue.popleft()
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if nx < 0 or ny < 0 or ny >= tile.shape[0] or nx >= tile.shape[1]:
                            continue
                        if mask[ny, nx] or not tile[ny, nx]:
                            continue
                        mask[ny, nx] = True
                        queue.append((nx, ny))
        positions = np.argwhere(mask)
        if positions.size == 0:
            return None
        tx, ty = (int(value) for value in tile_key.split(","))
        global_x = positions[:, 1] + tx * TILE_CELLS
        global_y = positions[:, 0] + ty * TILE_CELLS
        centres_x = floor["origin"][0] + (global_x + 0.5) * RESOLUTION_M
        centres_y = floor["origin"][1] + (global_y + 0.5) * RESOLUTION_M
        distances = np.hypot(centres_x - point[0], centres_y - point[1])
        index = int(np.argmin(distances))
        cell = (int(global_x[index]), int(global_y[index]))
        node = self._cell_component(floor, cell)
        if node not in {_node(tile_key, component_id) for component_id in component_ids}:
            raise NavigationError("A candidate tile mask included a cell outside the reachable component set.")
        return cell, node, float(distances[index])

    def _walkable_prefix(self, floor, path):
        result = [path[0]]
        for a, b in zip(path, path[1:]):
            distance = math.hypot(b[0] - a[0], b[1] - a[1])
            samples = max(1, math.ceil(distance / (RESOLUTION_M / 2)))
            for index in range(1, samples + 1):
                t = index / samples
                point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                cell = self._point_cell(floor, point)
                if cell is None or not self._walkable(floor, *cell):
                    return result
                result.append(point)
        return result

    def _load_tile(self, floor, tile_key):
        cache_key = (floor["key"], tile_key)
        touched = getattr(self.request_state, "touched", None)
        if touched is not None:
            touched.add(cache_key)
        with self.tile_cache_lock:
            cached = self.tile_cache.get(cache_key)
            if cached is not None:
                self.tile_cache.move_to_end(cache_key)
                return cached
        metadata = floor["tileIndex"].get(tile_key)
        if not metadata:
            return None
        path = (self.root / metadata["file"]).resolve()
        if self.root.resolve() not in path.parents:
            raise NavigationError("Navigation tile path escaped its package directory.")
        compressed = path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != metadata["sha256"]:
            raise NavigationError(f"Navigation tile {tile_key} failed its integrity check.")
        packed = zlib.decompress(compressed)
        count = metadata["width"] * metadata["height"]
        values = np.unpackbits(np.frombuffer(packed, dtype=np.uint8), bitorder="little")[:count]
        tile = values.reshape((metadata["height"], metadata["width"])).astype(np.bool_)
        with self.tile_cache_lock:
            cached = self.tile_cache.get(cache_key)
            if cached is not None:
                self.tile_cache.move_to_end(cache_key)
                return cached
            self.tile_cache[cache_key] = tile
            while len(self.tile_cache) > TILE_CACHE_LIMIT:
                self.tile_cache.popitem(last=False)
            return tile

    def _point_cell(self, floor, point):
        gx = math.floor((point[0] - floor["origin"][0]) / RESOLUTION_M)
        gy = math.floor((point[1] - floor["origin"][1]) / RESOLUTION_M)
        if gx < 0 or gy < 0 or gx >= floor["size"][0] or gy >= floor["size"][1]:
            return None
        return gx, gy

    def _walkable(self, floor, gx, gy):
        if gx < 0 or gy < 0 or gx >= floor["size"][0] or gy >= floor["size"][1]:
            return False
        tx, ty = gx // TILE_CELLS, gy // TILE_CELLS
        tile = self._load_tile(floor, f"{tx},{ty}")
        return bool(tile is not None and tile[gy - ty * TILE_CELLS, gx - tx * TILE_CELLS])

    def _cell_component(self, floor, cell):
        gx, gy = cell
        tx, ty = gx // TILE_CELLS, gy // TILE_CELLS
        tile_key = f"{tx},{ty}"
        metadata = floor["tileIndex"].get(tile_key)
        tile = self._load_tile(floor, tile_key)
        if not metadata or tile is None:
            return None
        local = (gx - tx * TILE_CELLS, gy - ty * TILE_CELLS)
        samples = {tuple(component["sample"]): component["id"] for component in metadata["components"]}
        queue = deque([local])
        seen = {local}
        while queue:
            x, y = queue.popleft()
            if (x, y) in samples:
                return _node(tile_key, samples[(x, y)])
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if nx < 0 or ny < 0 or ny >= tile.shape[0] or nx >= tile.shape[1] or not tile[ny, nx] or (nx, ny) in seen:
                    continue
                seen.add((nx, ny))
                queue.append((nx, ny))
        return None

    def _component_path(self, floor, start, end):
        graph = floor["componentGraph"]
        if start == end:
            return [start]
        queue = [(self._node_heuristic(start, end), 0, start)]
        cost = {start: 0}
        parent = {}
        while queue:
            _score, current_cost, current = heapq.heappop(queue)
            if current == end:
                return _restore(parent, current)
            if current_cost != cost.get(current):
                continue
            for neighbour in graph.get(current, []):
                next_cost = current_cost + 1
                if next_cost >= cost.get(neighbour, math.inf):
                    continue
                cost[neighbour] = next_cost
                parent[neighbour] = current
                h = self._node_heuristic(neighbour, end)
                heapq.heappush(queue, (next_cost + h, next_cost, neighbour))
        return None

    @staticmethod
    def _node_heuristic(a, b):
        ax, ay = (int(value) for value in a.split(":", 1)[0].split(","))
        bx, by = (int(value) for value in b.split(":", 1)[0].split(","))
        return abs(ax - bx) + abs(ay - by)

    def _astar_cells(self, floor, start, end, allowed_tiles):
        nx = floor["size"][0]
        start_key = start[1] * nx + start[0]
        end_key = end[1] * nx + end[0]
        queue = [(abs(start[0] - end[0]) + abs(start[1] - end[1]), abs(start[0] - end[0]) + abs(start[1] - end[1]), 0, start_key)]
        cost = {start_key: 0}
        parent = {}
        visited = 0
        while queue:
            _score, _tie, current_cost, key = heapq.heappop(queue)
            if current_cost != cost.get(key):
                continue
            visited += 1
            if key == end_key:
                keys = _restore(parent, key)
                return [(item % nx, item // nx) for item in keys], visited
            x, y = key % nx, key // nx
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                px, py = x + dx, y + dy
                tile_key = f"{px // TILE_CELLS},{py // TILE_CELLS}"
                if tile_key not in allowed_tiles or not self._walkable(floor, px, py):
                    continue
                neighbour = py * nx + px
                next_cost = current_cost + 1
                if next_cost >= cost.get(neighbour, math.inf):
                    continue
                cost[neighbour] = next_cost
                parent[neighbour] = key
                h = abs(px - end[0]) + abs(py - end[1])
                heapq.heappush(queue, (next_cost + h, h, next_cost, neighbour))
        return None, visited

    def _exact_path(self, floor, start, end, cells):
        centres = [self._cell_point(floor, cell) for cell in cells]
        centre_path = _simplify(centres)
        start_connector = self._connector(floor, start, centre_path[0])
        end_connector = list(reversed(self._connector(floor, end, centre_path[-1])))
        return _simplify(_dedupe([*start_connector, *centre_path[1:-1], *end_connector]))

    def _shorten_orthogonal_path(self, floor, path):
        """Remove grid-centre detours only when the replacement segment is safe."""
        points = _dedupe(path)
        if len(points) < 3:
            return points
        shortened = [points[0]]
        anchor = 0
        while anchor < len(points) - 1:
            next_index = anchor + 1
            for candidate_index in range(len(points) - 1, anchor, -1):
                a, b = points[anchor], points[candidate_index]
                aligned = abs(a[0] - b[0]) <= 1e-8 or abs(a[1] - b[1]) <= 1e-8
                if aligned and self._path_walkable(floor, [a, b]):
                    next_index = candidate_index
                    break
            shortened.append(points[next_index])
            anchor = next_index
        return _simplify(shortened)

    def _connector(self, floor, endpoint, centre):
        candidates = [
            [endpoint, (centre[0], endpoint[1]), centre],
            [endpoint, (endpoint[0], centre[1]), centre],
        ]
        valid = [candidate for candidate in candidates if self._path_walkable(floor, candidate)]
        if not valid:
            raise NavigationError("An exact clicked endpoint could not be connected to its walkable cell without a collision.")
        return min(valid, key=lambda path: sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])))

    def _cell_point(self, floor, cell):
        return (
            floor["origin"][0] + (cell[0] + 0.5) * RESOLUTION_M,
            floor["origin"][1] + (cell[1] + 0.5) * RESOLUTION_M,
        )

    def _path_walkable(self, floor, path):
        for a, b in zip(path, path[1:]):
            if abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8:
                return False
            distance = math.hypot(b[0] - a[0], b[1] - a[1])
            samples = max(1, math.ceil(distance / (RESOLUTION_M / 2)))
            for index in range(samples + 1):
                t = index / samples
                point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                cell = self._point_cell(floor, point)
                if cell is None or not self._walkable(floor, *cell):
                    return False
        return True

    def _audit_path(self, floor, path, start, end):
        endpoints_exact = bool(
            path
            and math.hypot(path[0][0] - start[0], path[0][1] - start[1]) <= 1e-9
            and math.hypot(path[-1][0] - end[0], path[-1][1] - end[1]) <= 1e-9
        )
        orthogonal = all(abs(a[0] - b[0]) <= 1e-8 or abs(a[1] - b[1]) <= 1e-8 for a, b in zip(path, path[1:]))
        collision_free = orthogonal and self._path_walkable(floor, path)
        return {"endpointsExact": endpoints_exact, "orthogonal": orthogonal, "collisionFree": collision_free}


def _valid_point(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise NavigationError(f"The {label} point must contain exactly two coordinates.")
    point = (_number(value[0]), _number(value[1]))
    if point[0] is None or point[1] is None:
        raise NavigationError(f"The {label} coordinates must be finite numbers.")
    return point


def _blocked(reason, message):
    return {"status": "blocked", "reason": reason, "message": message, "path": []}


def _restore(parent, current):
    path = [current]
    while current in parent:
        current = parent[current]
        path.append(current)
    return list(reversed(path))


def _dedupe(points):
    result = []
    for point in points:
        if not result or math.hypot(point[0] - result[-1][0], point[1] - result[-1][1]) > 1e-9:
            result.append(point)
    return result


def _path_distance(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _simplify(points):
    result = []
    for point in points:
        if len(result) >= 2:
            a, b = result[-2], result[-1]
            if (abs(a[0] - b[0]) <= 1e-9 and abs(b[0] - point[0]) <= 1e-9) or (abs(a[1] - b[1]) <= 1e-9 and abs(b[1] - point[1]) <= 1e-9):
                result[-1] = point
                continue
        result.append(point)
    return result
