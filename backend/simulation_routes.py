from __future__ import annotations

import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from pathlib import Path

from .navigation import NavigationPackage
from .model import Element, RouteEdge
from .resource_control import low_end_enabled, low_end_throttle
from .routes import apply_navigation_result_to_edge


_STRICT_ROUTE_WORKER_NAVIGATION: NavigationPackage | None = None


def _initialize_strict_route_worker(package_dir: str) -> None:
    global _STRICT_ROUTE_WORKER_NAVIGATION
    _STRICT_ROUTE_WORKER_NAVIGATION = NavigationPackage(Path(package_dir))


def _strict_route_worker(request: tuple[str, tuple[tuple[str, list[float], list[float]], ...]]):
    if _STRICT_ROUTE_WORKER_NAVIGATION is None:
        raise RuntimeError("Strict route worker was not initialized.")
    floor_name, directions = request
    return _calculate_directional_routes(_STRICT_ROUTE_WORKER_NAVIGATION, floor_name, directions)


def _direct_route_worker(request: tuple[str, list[float], list[float]]) -> dict:
    if _STRICT_ROUTE_WORKER_NAVIGATION is None:
        raise RuntimeError("Direct route worker was not initialized.")
    return _STRICT_ROUTE_WORKER_NAVIGATION.route(*request)


def _calculate_directional_routes(
    navigation: NavigationPackage,
    floor_name: str,
    directions: tuple[tuple[str, list[float], list[float]], ...],
) -> list[tuple[str, dict, dict | None]]:
    directional: list[tuple[str, dict, dict | None]] = []
    for start_guid, start_point, end_point in directions:
        result = navigation.route(floor_name, start_point, end_point)
        result.setdefault("resolutionM", navigation.index["resolutionM"])
        result.setdefault("clearanceM", navigation.index["wheelchairClearanceM"])
        result.setdefault("routeWidthM", navigation.index["accessibleRouteWidthM"])
        directional.append((start_guid, result, _record(result, navigation, floor_name)))
    return directional


def apply_strict_navigation_to_edges(
    app_data_path: Path,
    package_dir: Path,
    elements: list[Element],
    edges: list[RouteEdge],
) -> tuple[dict, dict[str, dict]]:
    """Make audited 0.01 m navigation the final source for every graph edge."""
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    navigation = NavigationPackage(package_dir)
    element_by_guid = {element.guid: element for element in elements}
    floor_by_edge = {
        edge_id: floor["name"]
        for floor in data.get("floors", [])
        for edge_id in floor.get("routeEdgeIds", [])
    }
    records_by_edge: dict[str, dict] = {}
    passed = blocked = unavailable = swapped = 0

    route_requests: list[tuple[str, tuple[tuple[str, list[float], list[float]], ...]]] = []
    for edge_index, edge in enumerate(edges):
        low_end_throttle(edge_index, interval=4, delay_s=0.003)
        floor_name = floor_by_edge.get(edge.edge_id)
        start = element_by_guid.get(edge.start_guid)
        end = element_by_guid.get(edge.end_guid)
        if not floor_name or not start or not end or not start.center or not end.center:
            raise RuntimeError(f"Route {edge.edge_id} lacks a floor or exact IFC endpoint geometry.")
        route_requests.append((
            floor_name,
            (
                (
                    edge.start_guid,
                    [float(start.center[0]), float(start.center[1])],
                    [float(end.center[0]), float(end.center[1])],
                ),
                (
                    edge.end_guid,
                    [float(end.center[0]), float(end.center[1])],
                    [float(start.center[0]), float(start.center[1])],
                ),
            ),
        ))

    if low_end_enabled() or len(route_requests) < 2:
        directional_results = [
            _calculate_directional_routes(navigation, floor_name, directions)
            for floor_name, directions in route_requests
        ]
    else:
        worker_count = min(8, max(1, os.cpu_count() or 1), len(route_requests))
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_initialize_strict_route_worker,
            initargs=(str(package_dir),),
        ) as executor:
            directional_results = list(executor.map(_strict_route_worker, route_requests, chunksize=1))

    for edge, directional in zip(edges, directional_results, strict=True):
        floor_name = floor_by_edge[edge.edge_id]
        statuses = {result.get("status") for _guid, result, _record_value in directional}
        if "pass" in statuses and statuses != {"pass"}:
            raise RuntimeError(f"Strict navigation became directional for undirected edge {edge.edge_id}: {statuses}")

        route_records = {
            start_guid: record
            for start_guid, _result, record in directional
            if record is not None
        }
        records_by_edge[edge.edge_id] = route_records
        passing = [item for item in directional if item[1].get("status") == "pass" and item[2]]
        blocked_candidates = [item for item in directional if item[2] and item[2].get("collisionPath")]

        if passing:
            chosen_guid, chosen_result, chosen_record = passing[0]
            display_path = chosen_record["path"]
            passed += 1
        elif blocked_candidates:
            chosen_guid, chosen_result, chosen_record = max(
                blocked_candidates,
                key=lambda item: (float(item[2].get("distanceM") or 0.0), item[0]),
            )
            collision_audit = chosen_record.get("collisionAudit") or {}
            if not all(
                collision_audit.get(key) is True
                for key in ("endpointsExact", "orthogonal", "safePrefixCollisionFree", "collisionEncountered")
            ):
                raise RuntimeError(f"Blocked route {edge.edge_id} has an invalid collision attempt: {collision_audit}")
            display_path = chosen_record["collisionPath"]
            blocked += 1
        else:
            chosen_guid, chosen_result, _chosen_record = directional[0]
            display_path = []
            unavailable += 1

        if chosen_guid != edge.start_guid:
            edge.start_guid, edge.end_guid = edge.end_guid, edge.start_guid
            swapped += 1
        apply_navigation_result_to_edge(edge, elements, chosen_result, display_path)
        edge.measurements["routeFloor"] = floor_name
        edge.measurements["routeStrictDirectionalRecordCount"] = len(route_records)

    summary = {
        "edgeCount": len(edges),
        "passCount": passed,
        "blockedCount": blocked,
        "unavailableCount": unavailable,
        "orientationSwapCount": swapped,
        "resolutionM": navigation.index["resolutionM"],
        "wheelchairClearanceM": navigation.index["wheelchairClearanceM"],
        "accessibleRouteWidthM": navigation.index["accessibleRouteWidthM"],
    }
    if passed + blocked + unavailable != len(edges):
        raise RuntimeError(f"Strict route accounting failed: {summary}")
    return summary, records_by_edge


def add_floor_check_routes(
    app_data_path: Path,
    package_dir: Path,
    precomputed_records: dict[str, dict] | None = None,
) -> dict:
    """Attach strict navigation geometry used only by the 2.5D floor-check view."""
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    navigation = NavigationPackage(package_dir)
    element_by_guid = {element["guid"]: element for element in data.get("elements", [])}
    floor_by_edge = {
        edge_id: floor["name"]
        for floor in data.get("floors", [])
        for edge_id in floor.get("routeEdgeIds", [])
    }
    generated = 0
    unavailable = 0
    blocked = 0

    for edge_index, edge in enumerate(data.get("routeEdges", [])):
        low_end_throttle(edge_index, interval=4, delay_s=0.003)
        floor_name = floor_by_edge.get(edge.get("edgeId"))
        if precomputed_records is not None:
            edge["floorCheckRoutes"] = _floor_check_records(
                edge,
                deepcopy(precomputed_records.get(edge.get("edgeId"), {})),
                navigation,
                floor_name,
                element_by_guid,
            )
            generated += len(edge["floorCheckRoutes"])
            blocked += sum(
                route["navigationStatus"] == "blocked"
                for route in edge["floorCheckRoutes"].values()
            )
            if not floor_name or not edge["floorCheckRoutes"]:
                unavailable += 1
            continue
        endpoints = _edge_endpoints(edge, element_by_guid)
        edge["floorCheckRoutes"] = {}
        if not floor_name or not endpoints:
            unavailable += 1
            continue

        start, end = endpoints
        forward = navigation.route(floor_name, start, end)
        forward_record = _record(forward, navigation, floor_name)
        if forward_record:
            edge["floorCheckRoutes"][edge["startGuid"]] = forward_record

        if forward.get("status") == "pass" and forward_record:
            edge["floorCheckRoutes"][edge["endGuid"]] = _reverse_pass_record(forward_record)
        else:
            reverse = navigation.route(floor_name, end, start)
            reverse_record = _record(reverse, navigation, floor_name)
            if reverse_record:
                edge["floorCheckRoutes"][edge["endGuid"]] = reverse_record
            if not forward_record and reverse.get("status") == "pass" and reverse_record:
                edge["floorCheckRoutes"][edge["startGuid"]] = _reverse_pass_record(reverse_record)

        generated += len(edge["floorCheckRoutes"])
        blocked += sum(
            route["navigationStatus"] == "blocked"
            for route in edge["floorCheckRoutes"].values()
        )
        if not edge["floorCheckRoutes"]:
            unavailable += 1

    direct_routes = _direct_door_routes(data, navigation, element_by_guid, floor_by_edge)
    data["floorCheckDirectRoutes"] = direct_routes

    summary = {
        "edgeCount": len(data.get("routeEdges", [])),
        "directionalRouteCount": generated,
        "blockedCandidateCount": blocked,
        "unavailableEdgeCount": unavailable,
        "directDoorRouteCount": len(direct_routes),
        "resolutionM": navigation.index["resolutionM"],
        "wheelchairClearanceM": navigation.index["wheelchairClearanceM"],
    }
    data.setdefault("summary", {})["floorCheckNavigation"] = summary
    data.setdefault("sources", {})["floorCheckNavigation"] = (
        "strict 2.5D floor-check routes from the precomputed point-navigation occupancy tiles"
    )
    app_data_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return summary


def _floor_check_records(
    edge: dict,
    records: dict[str, dict],
    navigation: NavigationPackage,
    floor_name: str | None,
    element_by_guid: dict[str, dict],
) -> dict[str, dict]:
    """Prepare automatic-floor-check geometry without changing graph routes.

    A stair centre is intentionally blocked.  General point routing therefore
    searches for the reachable cell nearest to that centre, which can be on the
    far side of a stair enclosure.  That candidate is correct for an arbitrary
    point query but visually wrong for a local stair approach.  Automatic floor
    checks instead make a direct, one-turn attempt from the door to the stair and
    stop at the first blocked cell.
    """
    if not floor_name:
        return records
    endpoints = {edge.get("startGuid"), edge.get("endGuid")}
    for start_guid, record in records.items():
        if record.get("navigationStatus") != "blocked" or start_guid not in endpoints:
            continue
        target_guid = edge.get("endGuid") if start_guid == edge.get("startGuid") else edge.get("startGuid")
        start = element_by_guid.get(start_guid) or {}
        target = element_by_guid.get(target_guid) or {}
        if target.get("ifcType") not in {"IfcStair", "IfcStairFlight"}:
            continue
        if not start.get("center") or not target.get("center"):
            raise RuntimeError(f"A stair floor-check route lacks endpoint geometry: {edge.get('edgeId')}")
        record.update(
            _direct_stair_collision_attempt(
                navigation,
                floor_name,
                start["center"],
                target["center"],
            )
        )
        record["displayGeometrySource"] = "direct orthogonal stair collision attempt"
    return records


def _direct_stair_collision_attempt(
    navigation: NavigationPackage,
    floor_name: str,
    start,
    destination,
) -> dict:
    floor = navigation.index.get("floors", {}).get(floor_name)
    if not floor:
        raise RuntimeError(f"Stair collision attempt has no floor package: {floor_name}")

    source = (float(start[0]), float(start[1]))
    target = (float(destination[0]), float(destination[1]))
    connectors = [
        _dedupe_plan_points([source, (target[0], source[1]), target]),
        _dedupe_plan_points([source, (source[0], target[1]), target]),
    ]
    ranked = []
    for index, connector in enumerate(connectors):
        prefix = navigation._walkable_prefix(floor, connector)
        safe_distance = _plan_distance(prefix)
        total_distance = _plan_distance(connector)
        reaches_target = math.hypot(prefix[-1][0] - target[0], prefix[-1][1] - target[1]) <= 1e-9
        if total_distance <= 0 or reaches_target or safe_distance >= total_distance - 1e-9:
            continue
        ranked.append((safe_distance, -index, connector, prefix, total_distance))
    if not ranked:
        raise RuntimeError("A blocked stair has no direct orthogonal collision attempt.")

    safe_distance, _index, connector, prefix, total_distance = max(ranked, key=lambda item: item[:2])
    elevation = float(floor.get("elevation", 0.0))
    collision_path = [[point[0], point[1], elevation] for point in connector]
    collision_progress = safe_distance / total_distance
    if collision_progress <= 0 or collision_progress >= 1:
        raise RuntimeError("A direct stair collision attempt has an invalid stop position.")
    return {
        "collisionPath": collision_path,
        "collisionDistanceM": round(total_distance, 4),
        "collisionProgress": collision_progress,
        "collisionStopPoint": [prefix[-1][0], prefix[-1][1], elevation],
        "collisionAudit": {
            "endpointsExact": True,
            "orthogonal": True,
            "safePrefixCollisionFree": True,
            "collisionEncountered": True,
            "maximumTurnCount": 1,
        },
    }


def _dedupe_plan_points(points):
    result = []
    for point in points:
        if not result or math.hypot(point[0] - result[-1][0], point[1] - result[-1][1]) > 1e-9:
            result.append(point)
    return result


def _direct_door_routes(
    data: dict,
    navigation: NavigationPackage,
    element_by_guid: dict[str, dict],
    floor_by_edge: dict[str, str],
) -> list[dict]:
    """Build one visual floor-check path per reachable door pair.

    The door graph still decides reachability.  Its intermediate doors are not
    forced into the displayed geometry; the navigation grid calculates one
    collision-free path between the selected start and final doors instead.
    """
    requests: dict[tuple[str, str], dict] = {}
    for start_guid, routes in data.get("accessibleRoutesByDoor", {}).items():
        start = element_by_guid.get(start_guid)
        if not start or start.get("ifcType") != "IfcDoor":
            continue
        for route in routes:
            target_guid = route.get("target_guid")
            target = element_by_guid.get(target_guid)
            edge_ids = route.get("edge_ids") or []
            if not target or target.get("ifcType") != "IfcDoor" or not edge_ids:
                continue
            pair = tuple(sorted((start_guid, target_guid)))
            floor_name = floor_by_edge.get(edge_ids[0])
            if not floor_name:
                raise RuntimeError(f"A direct floor-check door route has no floor: {pair}")
            previous = requests.get(pair)
            if previous and previous["floor"] != floor_name:
                raise RuntimeError(f"A door pair was assigned to multiple floors: {pair}")
            request = {"floor": floor_name, "startGuid": start_guid, "edgeIds": edge_ids}
            if not previous or start_guid == pair[0]:
                requests[pair] = request

    request_items = sorted(requests.items())
    route_jobs = []
    for request_index, ((start_guid, end_guid), request) in enumerate(request_items):
        low_end_throttle(request_index, interval=4, delay_s=0.003)
        start = element_by_guid[start_guid].get("center")
        end = element_by_guid[end_guid].get("center")
        if not start or not end:
            raise RuntimeError(f"A direct floor-check door route has no endpoint geometry: {(start_guid, end_guid)}")
        route_jobs.append((
            request["floor"],
            [float(start[0]), float(start[1])],
            [float(end[0]), float(end[1])],
        ))

    if low_end_enabled() or len(route_jobs) < 2:
        route_results = [navigation.route(*job) for job in route_jobs]
    else:
        worker_count = min(8, max(1, os.cpu_count() or 1), len(route_jobs))
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_initialize_strict_route_worker,
            initargs=(str(navigation.root.parent),),
        ) as executor:
            route_results = list(executor.map(_direct_route_worker, route_jobs, chunksize=1))

    records = []
    edge_by_id = {edge["edgeId"]: edge for edge in data.get("routeEdges", [])}
    for ((start_guid, end_guid), request), result in zip(request_items, route_results, strict=True):
        floor_name = request["floor"]
        audit = result.get("audit") or {}
        if result.get("status") != "pass" or not all(
            audit.get(key) is True
            for key in ("endpointsExact", "orthogonal", "collisionFree")
        ):
            raise RuntimeError(
                f"The accessible door graph disagrees with strict navigation for "
                f"{start_guid} -> {end_guid}: {result.get('reason')}, {audit}"
            )
        direct_path = result["path"]
        direct_distance = _plan_distance([(point[0], point[1]) for point in direct_path])
        chain_edge_ids = request["edgeIds"] if request["startGuid"] == start_guid else list(reversed(request["edgeIds"]))
        chain_path = _composed_pass_path(start_guid, end_guid, chain_edge_ids, edge_by_id)
        chain_distance = _plan_distance([(point[0], point[1]) for point in chain_path])
        if chain_distance + 1e-9 < direct_distance:
            chosen_path = _simplify_collinear(chain_path)
            chosen_distance = chain_distance
            geometry_source = "shorter audited door-graph chain"
        else:
            chosen_path = direct_path
            chosen_distance = direct_distance
            geometry_source = "direct strict navigation"
        records.append({
            "startGuid": start_guid,
            "endGuid": end_guid,
            "floor": floor_name,
            "path": chosen_path,
            "distanceM": round(chosen_distance, 4),
            "geometrySource": geometry_source,
            "resolutionM": result.get("resolutionM"),
            "clearanceM": result.get("clearanceM"),
            "routeWidthM": result.get("routeWidthM"),
            "streamedTiles": result.get("streamedTiles", 0),
            "audit": audit,
        })
    return records


def _composed_pass_path(start_guid: str, end_guid: str, edge_ids: list[str], edge_by_id: dict[str, dict]) -> list[list[float]]:
    current = start_guid
    points: list[list[float]] = []
    for edge_id in edge_ids:
        edge = edge_by_id.get(edge_id)
        if not edge or current not in (edge.get("startGuid"), edge.get("endGuid")):
            raise RuntimeError(f"An accessible door route has a broken edge chain at {edge_id}.")
        route = (edge.get("floorCheckRoutes") or {}).get(current)
        if not route or route.get("navigationStatus") != "pass":
            raise RuntimeError(f"An accessible door route lacks strict passing geometry at {edge_id}.")
        for point in route["path"]:
            if not points or math.hypot(point[0] - points[-1][0], point[1] - points[-1][1]) > 1e-9:
                points.append(point)
        current = edge["endGuid"] if current == edge["startGuid"] else edge["startGuid"]
    if current != end_guid or len(points) < 2:
        raise RuntimeError(f"An accessible door route did not reach its final door: {start_guid} -> {end_guid}.")
    return points


def _simplify_collinear(points: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for point in points:
        if len(result) >= 2:
            a, b = result[-2], result[-1]
            if (
                abs(a[0] - b[0]) <= 1e-9 and abs(b[0] - point[0]) <= 1e-9
            ) or (
                abs(a[1] - b[1]) <= 1e-9 and abs(b[1] - point[1]) <= 1e-9
            ):
                result[-1] = point
                continue
        result.append(point)
    return result


def _edge_endpoints(edge: dict, element_by_guid: dict[str, dict]):
    path = edge.get("path") or []
    if len(path) >= 2:
        return ([float(path[0][0]), float(path[0][1])], [float(path[-1][0]), float(path[-1][1])])
    start = element_by_guid.get(edge.get("startGuid"), {}).get("center")
    end = element_by_guid.get(edge.get("endGuid"), {}).get("center")
    if not start or not end:
        return None
    return ([float(start[0]), float(start[1])], [float(end[0]), float(end[1])])


def _record(result: dict, navigation: NavigationPackage, floor_name: str):
    path = result.get("path") or []
    if len(path) < 2:
        return None
    audit = result.get("audit") or {}
    if not audit.get("orthogonal") or not audit.get("collisionFree"):
        raise RuntimeError(f"Floor-check route failed its navigation audit: {audit}")
    if result.get("status") == "pass" and not audit.get("endpointsExact"):
        raise RuntimeError(f"Passing floor-check route lost an endpoint: {audit}")
    if result.get("status") == "blocked" and audit.get("reachesDestination") is not False:
        raise RuntimeError(f"Blocked floor-check candidate reached its destination: {audit}")
    record = {
        "navigationStatus": result["status"],
        "reason": result.get("reason"),
        "path": path,
        "distanceM": result.get("distanceM"),
        "resolutionM": result.get("resolutionM"),
        "clearanceM": result.get("clearanceM"),
        "routeWidthM": result.get("routeWidthM"),
        "streamedTiles": result.get("streamedTiles", 0),
        "audit": audit,
    }
    if result.get("status") == "blocked":
        attempt = _collision_attempt(navigation, floor_name, path, result.get("end"))
        record.update(attempt)
    return record


def _collision_attempt(
    navigation: NavigationPackage,
    floor_name: str,
    safe_path: list[list[float]],
    destination,
) -> dict:
    """Extend a blocked floor-check display route to its IFC target.

    The navigation path remains the audited collision-free prefix.  This separate
    path is allowed to enter the blocked target so the floor-check animation can
    show where the wheelchair first collides without weakening route validation.
    """
    floor = navigation.index.get("floors", {}).get(floor_name)
    if not floor:
        raise RuntimeError(f"Floor-check collision attempt has no floor package: {floor_name}")
    if not isinstance(destination, (list, tuple)) or len(destination) < 2:
        raise RuntimeError("A blocked floor-check route has no intended destination.")

    safe_2d = [(float(point[0]), float(point[1])) for point in safe_path]
    target = (float(destination[0]), float(destination[1]))
    last = safe_2d[-1]
    tolerance = 1e-8
    if abs(last[0] - target[0]) <= tolerance or abs(last[1] - target[1]) <= tolerance:
        connectors = [[last, target]]
    else:
        connectors = [
            [last, (target[0], last[1]), target],
            [last, (last[0], target[1]), target],
        ]

    ranked = []
    for index, connector in enumerate(connectors):
        prefix = navigation._walkable_prefix(floor, connector)
        safe_extension = _plan_distance(prefix)
        remaining = math.hypot(prefix[-1][0] - target[0], prefix[-1][1] - target[1])
        ranked.append((safe_extension, -remaining, -index, connector, prefix))
    _safe_extension, _remaining, _index, connector, prefix = max(ranked, key=lambda item: item[:3])

    collision_reached_target = math.hypot(prefix[-1][0] - target[0], prefix[-1][1] - target[1]) <= 1e-9
    if collision_reached_target:
        raise RuntimeError("A blocked floor-check route reached its target without a collision.")

    elevation = float(floor.get("elevation", 0.0))
    collision_path = [*safe_path]
    collision_path.extend([[point[0], point[1], elevation] for point in connector[1:]])
    orthogonal = all(
        abs(a[0] - b[0]) <= tolerance or abs(a[1] - b[1]) <= tolerance
        for a, b in zip(collision_path, collision_path[1:])
    )
    endpoints_exact = (
        math.hypot(collision_path[0][0] - safe_path[0][0], collision_path[0][1] - safe_path[0][1]) <= 1e-9
        and math.hypot(collision_path[-1][0] - target[0], collision_path[-1][1] - target[1]) <= 1e-9
    )
    if not orthogonal or not endpoints_exact:
        raise RuntimeError("A floor-check collision attempt lost its orthogonal endpoint contract.")

    safe_distance = _plan_distance(safe_2d) + _plan_distance(prefix)
    collision_distance = _plan_distance([(point[0], point[1]) for point in collision_path])
    if collision_distance <= 0 or safe_distance >= collision_distance - 1e-9:
        raise RuntimeError("A floor-check collision attempt has no blocked terminal segment.")
    collision_progress = safe_distance / collision_distance

    return {
        "collisionPath": collision_path,
        "collisionDistanceM": round(collision_distance, 4),
        "collisionProgress": collision_progress,
        "collisionStopPoint": [prefix[-1][0], prefix[-1][1], elevation],
        "collisionAudit": {
            "endpointsExact": True,
            "orthogonal": True,
            "safePrefixCollisionFree": True,
            "collisionEncountered": True,
        },
    }


def _plan_distance(points) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(points, points[1:])
    )


def _reverse_pass_record(record: dict) -> dict:
    path = list(reversed(record["path"]))
    distance = sum(
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(path, path[1:])
    )
    return {
        **record,
        "path": path,
        "distanceM": round(distance, 4),
        "audit": {
            "endpointsExact": True,
            "orthogonal": True,
            "collisionFree": True,
        },
    }
