import unittest

from backend.model import Element, RouteEdge
from backend.plan_routes import (
    _compact_path,
    _plan_candidates,
    _required_turn_path,
    _route_clear_width_measurement,
    _set_route_turning_space,
    _simplify_path,
    _space_clearance_region,
    build_plan_network,
)


def door(guid, x):
    return Element(
        guid=guid,
        ifc_type="IfcDoor",
        name=guid,
        label=guid,
        center=(x, 0.0, 0.0),
        extra={"isRouteRelevantDoor": True, "isExcludedRouteDoor": False},
    )


def edge(edge_id, start, end, status="pass", reasons=None, distance_m=1.0):
    return RouteEdge(
        edge_id=edge_id,
        start_guid=start,
        end_guid=end,
        distance_m=distance_m,
        status=status,
        reasons=reasons or [],
        path=[(0.0, 0.0, 0.0), (distance_m, 0.0, 0.0)],
    )


class PlanRouteTests(unittest.TestCase):
    def test_compact_path_keeps_only_turns(self):
        path = [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.2, 0.0, 0.0), (0.2, 1.0, 0.0)]
        self.assertEqual(_compact_path(path), [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0), (0.2, 1.0, 0.0)])

    def test_network_keeps_accessible_forest_and_issue_witness(self):
        elements = [door("A", 0.0), door("B", 1.0), door("C", 2.0)]
        candidates = [
            edge("AB", "A", "B"),
            edge("AC", "A", "C", distance_m=2.0),
            edge("BC", "B", "C", status="fail", reasons=["route_width"]),
        ]
        result = {item.edge_id: item for item in build_plan_network(elements, candidates)}
        self.assertEqual(set(result), {"AB", "AC", "BC"})
        self.assertIn("accessible", result["AB"].measurements["planNetworkRole"])
        self.assertIn("accessible", result["AC"].measurements["planNetworkRole"])
        self.assertEqual(result["BC"].measurements["planNetworkRole"], "issue")
        self.assertEqual(candidates[0].measurements, {})

    def test_network_marks_unconnected_route_door(self):
        elements = [door("A", 0.0), door("B", 1.0), door("C", 2.0)]
        result = build_plan_network(elements, [edge("AB", "A", "B")])
        marker = next(item for item in result if item.start_guid == "C")
        self.assertEqual(marker.reasons, ["unreachable"])
        self.assertTrue(marker.measurements["planMarkerOnly"])

    def test_network_keeps_stair_issue_marker(self):
        elements = [door("A", 0.0), door("B", 1.0)]
        stair = edge("STAIR", "A", "S", status="fail", reasons=["stair_block"])
        result = build_plan_network(elements, [edge("AB", "A", "B")], [stair])
        marker = next(item for item in result if item.start_guid == "A" and item.end_guid == "S")
        self.assertEqual(marker.reasons, ["stair_block"])
        self.assertTrue(marker.measurements["planMarkerOnly"])

    def test_corridor_region_uses_local_cross_section(self):
        from shapely.geometry import box

        corridor = Element("S", "IfcSpace", "S", "S", center=(3.0, 0.6, 0.0))
        region = _space_clearance_region(corridor, box(0.0, 0.0, 6.0, 1.2), [], {})
        self.assertIsNotNone(region)
        self.assertAlmostEqual(region["measured"], 1.2, delta=0.05)
        self.assertEqual(region["required"], 1.5)
        self.assertTrue(region["areas"])

    def test_wide_corridor_has_no_issue_region(self):
        from shapely.geometry import box

        corridor = Element("S", "IfcSpace", "S", "S", center=(3.0, 1.0, 0.0))
        self.assertIsNone(_space_clearance_region(corridor, box(0.0, 0.0, 6.0, 2.0), [], {}))

    def test_route_width_follows_the_path(self):
        from shapely.ops import unary_union
        from shapely.geometry import box

        area = unary_union([box(0.0, 0.0, 2.0, 2.0), box(2.0, 0.4, 4.0, 1.6), box(4.0, 0.0, 6.0, 2.0)])
        value = _route_clear_width_measurement([(0.5, 1.0, 0.0), (5.5, 1.0, 0.0)], area, [], {})
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value[0], 1.2, delta=0.05)

    def test_plan_candidate_prefers_wider_accessible_detour(self):
        from shapely.geometry import box
        from shapely.ops import unary_union

        first = door("A", 1.0)
        first.center = (1.0, 2.0, 0.0)
        first.extra["derivedDoorWidthM"] = 1.0
        second = door("B", 7.0)
        second.center = (7.0, 2.0, 0.0)
        second.extra["derivedDoorWidthM"] = 1.0
        space = Element("S", "IfcSpace", "S", "S", center=(4.0, 2.5, 0.0))
        area = unary_union(
            [
                box(0.0, 0.0, 2.5, 4.0),
                box(5.5, 0.0, 8.0, 4.0),
                box(2.0, 1.5, 6.0, 2.5),
                box(1.0, 3.0, 7.0, 5.0),
            ]
        )
        candidates = _plan_candidates(
            [first, second, space],
            {space.guid: area},
            {first.guid: [space.guid], second.guid: [space.guid]},
            [],
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "pass")
        self.assertGreaterEqual(candidates[0].measurements["routeClearWidthM"], 1.5)
        self.assertGreater(max(point[1] for point in candidates[0].path), 3.0)

    def test_simplification_removes_avoidable_grid_bends(self):
        from shapely.geometry import box

        area = box(0.0, 0.0, 6.0, 4.0)
        path = [(0.5, 1.0, 0.0), (0.5, 2.0, 0.0), (3.0, 2.0, 0.0), (3.0, 1.0, 0.0), (5.5, 1.0, 0.0)]
        simplified = _simplify_path(path, area)
        self.assertEqual(simplified, [path[0], path[-1]])
        self.assertEqual(_required_turn_path(path, area), [path[0], path[-1]])
        measurements = {"routeHasTurn": True, "routeTurningSpaceM": 0.8}
        _set_route_turning_space(measurements, path, area)
        self.assertFalse(measurements["routeHasTurn"])
        self.assertEqual(measurements["routeRequiredTurnCount"], 0)
        self.assertNotIn("routeTurningSpaceM", measurements)

    def test_required_turn_remains_when_direct_path_leaves_area(self):
        from shapely.geometry import box
        from shapely.ops import unary_union

        area = unary_union([box(0.0, 0.0, 1.2, 5.0), box(0.0, 3.8, 5.0, 5.0)])
        path = [(0.6, 0.6, 0.0), (0.6, 4.4, 0.0), (4.4, 4.4, 0.0)]
        self.assertEqual(_simplify_path(path, area), path)
        required = _required_turn_path(path, area)
        self.assertEqual(required, path)
        measurements = {}
        _set_route_turning_space(measurements, path, area)
        self.assertTrue(measurements["routeHasTurn"])
        self.assertEqual(measurements["routeRequiredTurnCount"], 1)
        self.assertLess(measurements["routeTurningSpaceM"], 1.5)


if __name__ == "__main__":
    unittest.main()
