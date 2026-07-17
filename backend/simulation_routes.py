from __future__ import annotations

import json
import math
from pathlib import Path

from .navigation import NavigationPackage


def add_floor_check_routes(app_data_path: Path, package_dir: Path) -> dict:
    """Attach strict navigation geometry used only by the 2.5D floor-check view."""
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    navigation = NavigationPackage(package_dir)
    element_by_guid = {element["guid"]: element for element in data.get("elements", [])}
    for key in ("routeEdges", "planRouteEdges"):
        for edge in data.get(key, []):
            edge.pop("floorCheckRoutes", None)
            edge.pop("floorCheckExcludedReason", None)
    floor_check_edges, floor_edge_key, edge_source = _floor_check_edge_source(data)
    floor_by_edge = {
        edge_id: floor["name"]
        for floor in data.get("floors", [])
        for edge_id in floor.get(floor_edge_key, [])
    }
    floor_by_element = {
        guid: floor["name"]
        for floor in data.get("floors", [])
        for guid in floor.get("elementGuids", [])
    }
    generated = 0
    unavailable = 0
    blocked = 0
    cross_floor = 0

    for edge in floor_check_edges:
        floor_name = floor_by_edge.get(edge.get("edgeId"))
        endpoints = _edge_endpoints(edge, element_by_guid)
        edge["floorCheckRoutes"] = {}
        if _cross_floor_edge(edge, floor_by_element):
            edge["floorCheckExcludedReason"] = "cross_floor"
            cross_floor += 1
            continue
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

    direct_routes, direct_route_mismatches = _direct_door_routes(
        data,
        navigation,
        element_by_guid,
        floor_by_edge,
        floor_by_element,
    )
    data["floorCheckDirectRoutes"] = direct_routes
    data["floorCheckDirectRouteMismatches"] = direct_route_mismatches

    summary = {
        "edgeCount": len(floor_check_edges),
        "edgeSource": edge_source,
        "directionalRouteCount": generated,
        "blockedCandidateCount": blocked,
        "unavailableEdgeCount": unavailable,
        "crossFloorExcludedEdgeCount": cross_floor,
        "directDoorRouteCount": len(direct_routes),
        "directDoorRouteMismatchCount": len(direct_route_mismatches),
        "resolutionM": navigation.index["resolutionM"],
        "wheelchairClearanceM": navigation.index["wheelchairClearanceM"],
    }
    data.setdefault("summary", {})["floorCheckNavigation"] = summary
    data.setdefault("sources", {})["floorCheckNavigation"] = (
        f"strict 2.5D floor-check routes over {edge_source} from the precomputed point-navigation occupancy tiles"
    )
    app_data_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return summary


def _floor_check_edge_source(data: dict) -> tuple[list[dict], str, str]:
    plan_edges = [
        edge
        for edge in data.get("planRouteEdges", [])
        if not (edge.get("measurements") or {}).get("planMarkerOnly")
    ]
    if plan_edges:
        return plan_edges, "planRouteEdgeIds", "planRouteEdges"
    return data.get("routeEdges", []), "routeEdgeIds", "routeEdges"


def _direct_door_routes(
    data: dict,
    navigation: NavigationPackage,
    element_by_guid: dict[str, dict],
    floor_by_edge: dict[str, str],
    floor_by_element: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Build one visual floor-check path per reachable door pair.

    The door graph still decides reachability.  Its intermediate doors are not
    forced into the displayed geometry; the navigation grid calculates one
    collision-free path between the selected start and final doors instead.
    """
    requests = _direct_route_requests(data, element_by_guid, floor_by_edge, floor_by_element)

    records = []
    mismatches = []
    edge_by_id = {edge["edgeId"]: edge for edge in data.get("routeEdges", [])}
    for (start_guid, end_guid), request in sorted(requests.items()):
        floor_name = request["floor"]
        start = element_by_guid[start_guid].get("center")
        end = element_by_guid[end_guid].get("center")
        if not start or not end:
            raise RuntimeError(f"A direct floor-check door route has no endpoint geometry: {(start_guid, end_guid)}")
        result = navigation.route(
            floor_name,
            [float(start[0]), float(start[1])],
            [float(end[0]), float(end[1])],
        )
        audit = result.get("audit") or {}
        if result.get("status") != "pass" or not all(
            audit.get(key) is True
            for key in ("endpointsExact", "orthogonal", "collisionFree")
        ):
            mismatches.append({
                "startGuid": start_guid,
                "endGuid": end_guid,
                "floor": floor_name,
                "edgeIds": request["edgeIds"],
                "reason": result.get("reason"),
                "audit": audit,
            })
            continue
        direct_path = result["path"]
        direct_distance = _plan_distance([(point[0], point[1]) for point in direct_path])
        chain_edge_ids = request.get("chainEdgeIds") or []
        if chain_edge_ids:
            ordered_chain = chain_edge_ids if request["startGuid"] == start_guid else list(reversed(chain_edge_ids))
            chain_path = _composed_pass_path(start_guid, end_guid, ordered_chain, edge_by_id)
            chain_distance = _plan_distance([(point[0], point[1]) for point in chain_path])
        else:
            chain_path = []
            chain_distance = math.inf
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
            "edgeIds": request["edgeIds"],
            "path": chosen_path,
            "distanceM": round(chosen_distance, 4),
            "geometrySource": geometry_source,
            "resolutionM": result.get("resolutionM"),
            "clearanceM": result.get("clearanceM"),
            "routeWidthM": result.get("routeWidthM"),
            "streamedTiles": result.get("streamedTiles", 0),
            "audit": audit,
        })
    return records, mismatches


def _direct_route_requests(
    data: dict,
    element_by_guid: dict[str, dict],
    floor_by_edge: dict[str, str],
    floor_by_element: dict[str, str],
) -> dict[tuple[str, str], dict]:
    requests: dict[tuple[str, str], dict] = {}
    plan_edges = [
        edge
        for edge in data.get("planRouteEdges", [])
        if edge.get("status") == "pass"
        and "accessible" in str((edge.get("measurements") or {}).get("planNetworkRole", "")).split()
    ]
    if plan_edges:
        for edge in plan_edges:
            start_guid = edge.get("startGuid")
            target_guid = edge.get("endGuid")
            start = element_by_guid.get(start_guid)
            target = element_by_guid.get(target_guid)
            if not start or not target or start.get("ifcType") != "IfcDoor" or target.get("ifcType") != "IfcDoor":
                continue
            floor_name = _single_floor_route(start_guid, target_guid, [], floor_by_element, floor_by_edge)
            if not floor_name:
                continue
            pair = tuple(sorted((start_guid, target_guid)))
            requests[pair] = {
                "floor": floor_name,
                "startGuid": start_guid,
                "edgeIds": [edge["edgeId"]],
                "chainEdgeIds": [],
            }
        return requests

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
            floor_name = _single_floor_route(start_guid, target_guid, edge_ids, floor_by_element, floor_by_edge)
            if not floor_name:
                continue
            previous = requests.get(pair)
            request = {
                "floor": floor_name,
                "startGuid": start_guid,
                "edgeIds": edge_ids,
                "chainEdgeIds": edge_ids,
            }
            if not previous or start_guid == pair[0]:
                requests[pair] = request
    return requests


def _cross_floor_edge(edge: dict, floor_by_element: dict[str, str]) -> bool:
    start_floor = floor_by_element.get(edge.get("startGuid"))
    end_floor = floor_by_element.get(edge.get("endGuid"))
    return bool(start_floor and end_floor and start_floor != end_floor)


def _single_floor_route(
    start_guid: str,
    target_guid: str,
    edge_ids: list[str],
    floor_by_element: dict[str, str],
    floor_by_edge: dict[str, str],
) -> str | None:
    floor_name = floor_by_element.get(start_guid)
    if not floor_name or floor_by_element.get(target_guid) != floor_name:
        return None
    if any(floor_by_edge.get(edge_id) != floor_name for edge_id in edge_ids):
        return None
    return floor_name


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
