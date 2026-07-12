import unittest

from backend.model import Element, RouteEdge
from backend.plan_routes import _compact_path, _route_clear_width_measurement, _space_clearance_region, build_plan_network


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


if __name__ == "__main__":
    unittest.main()
