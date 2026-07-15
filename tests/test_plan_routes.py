import unittest

from backend.model import Element, RouteEdge
from backend.plan_routes import (
    _compact_path,
    _corridor_movement_area_region,
    _door_portal_polygon,
    _door_opens_wall,
    _measurement_reasons,
    _path_inside_area,
    _plan_candidates,
    _required_turn_path,
    _route_avoids_walls,
    _route_clear_width_measurement,
    _route_turning_space_measurement,
    _set_route_turning_space,
    _simplify_path,
    _space_clearance_region,
    _space_walkable_area,
    build_plan_network,
)
from backend.routes import route_measurements


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


def oriented_door(guid, x, y, width_axis, depth_axis):
    value = door(guid, x)
    value.center = (x, y, 0.0)
    value.extra.update(
        {
            "doorWidthAxisX": width_axis[0],
            "doorWidthAxisY": width_axis[1],
            "doorDepthAxisX": depth_axis[0],
            "doorDepthAxisY": depth_axis[1],
            "doorOpeningWidthM": 1.0,
            "doorOpeningDepthM": 0.2,
            "derivedDoorWidthM": 1.0,
        }
    )
    return value


class PlanRouteTests(unittest.TestCase):
    def test_compact_path_keeps_only_turns(self):
        path = [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.2, 0.0, 0.0), (0.2, 1.0, 0.0)]
        self.assertEqual(_compact_path(path), [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0), (0.2, 1.0, 0.0)])

    def test_network_drops_redundant_issue_witness(self):
        elements = [door("A", 0.0), door("B", 1.0), door("C", 2.0)]
        candidates = [
            edge("AB", "A", "B"),
            edge("AC", "A", "C", distance_m=2.0),
            edge("BC", "B", "C", status="fail", reasons=["route_width"]),
        ]
        result = {item.edge_id: item for item in build_plan_network(elements, candidates)}
        self.assertEqual(set(result), {"AB", "AC"})
        self.assertIn("accessible", result["AB"].measurements["planNetworkRole"])
        self.assertIn("accessible", result["AC"].measurements["planNetworkRole"])
        self.assertEqual(candidates[0].measurements, {})

    def test_network_keeps_failed_edge_required_for_coverage(self):
        elements = [door("A", 0.0), door("B", 1.0), door("C", 2.0)]
        candidates = [edge("AB", "A", "B"), edge("BC", "B", "C", status="fail", reasons=["route_width"])]
        result = {item.edge_id: item for item in build_plan_network(elements, candidates)}
        self.assertEqual(set(result), {"AB", "BC"})
        self.assertEqual(result["BC"].measurements["planNetworkRole"], "physical")

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

    def test_new_route_measurements_block_low_doors_and_long_ramps(self):
        reasons = _measurement_reasons({"routeDoorHeightMinM": 2.0, "routeRampRunLengthM": 6.1})
        self.assertEqual(reasons, ["door_height", "ramp_run_length"])

    def test_turning_space_is_not_copied_from_space_width(self):
        first = door("A", 0.0)
        second = door("B", 2.0)
        space = Element(
            "S",
            "IfcSpace",
            "S",
            "S",
            extra={"derivedClearSpaceWidthM": 1.2, "turningSpaceM": 1.2},
        )
        measurements = route_measurements(
            first,
            second,
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)],
            [],
            space,
        )
        self.assertEqual(measurements["routeClearWidthM"], 1.2)
        self.assertTrue(measurements["routeHasTurn"])
        self.assertNotIn("routeTurningSpaceM", measurements)

    def test_door_portal_extends_only_across_the_wall(self):
        from shapely.geometry import box

        value = oriented_door("A", 0.0, 0.0, (0.0, 1.0), (1.0, 0.0))
        portal = _door_portal_polygon(value, {value.guid: box(-0.1, -0.5, 0.1, 0.5)}, normal_extension=0.3)
        self.assertIsNotNone(portal)
        self.assertAlmostEqual(portal.bounds[0], -0.4, delta=0.01)
        self.assertAlmostEqual(portal.bounds[1], -0.52, delta=0.01)
        self.assertAlmostEqual(portal.bounds[2], 0.4, delta=0.01)
        self.assertAlmostEqual(portal.bounds[3], 0.52, delta=0.01)

    def test_door_portal_uses_the_host_wall_midplane(self):
        from shapely.geometry import box

        value = oriented_door("A", 0.08, 0.0, (0.0, 1.0), (1.0, 0.0))
        value.extra["doorHostGuid"] = "W"
        portal = _door_portal_polygon(
            value,
            {value.guid: box(-0.02, -0.5, 0.18, 0.5), "W": box(-0.1, -2.0, 0.1, 2.0)},
            normal_extension=0.06,
        )
        self.assertAlmostEqual((portal.bounds[0] + portal.bounds[2]) / 2, 0.0, delta=0.01)
        self.assertLessEqual(portal.bounds[0], -0.15)
        self.assertGreaterEqual(portal.bounds[2], 0.15)

    def test_same_wall_doors_route_through_the_room_interior(self):
        from shapely.geometry import box

        first = oriented_door("A", 1.0, 6.0, (1.0, 0.0), (0.0, 1.0))
        second = oriented_door("B", 7.0, 6.0, (1.0, 0.0), (0.0, 1.0))
        space = Element("S", "IfcSpace", "S", "S", center=(4.0, 3.0, 0.0))
        candidates = _plan_candidates(
            [first, second, space],
            {
                first.guid: box(0.5, 5.9, 1.5, 6.1),
                second.guid: box(6.5, 5.9, 7.5, 6.1),
                space.guid: box(0.0, 0.0, 8.0, 6.0),
            },
            {first.guid: [space.guid], second.guid: [space.guid]},
            [],
        )
        self.assertEqual(len(candidates), 1)
        self.assertLessEqual(candidates[0].path[1][1], 5.35)
        self.assertLessEqual(candidates[0].path[-2][1], 5.35)

    def test_tangential_door_route_is_replaced(self):
        from shapely.geometry import box

        first = oriented_door("A", 1.0, 6.0, (1.0, 0.0), (0.0, 1.0))
        second = oriented_door("B", 0.0, 3.0, (0.0, 1.0), (1.0, 0.0))
        space = Element("S", "IfcSpace", "S", "S", center=(4.0, 3.0, 0.0))
        matched = RouteEdge(
            "OLD",
            first.guid,
            second.guid,
            4.0,
            "pass",
            [],
            path=[first.center, (0.0, 6.0, 0.0), second.center],
            via_space_guid=space.guid,
        )
        candidates = _plan_candidates(
            [first, second, space],
            {
                first.guid: box(0.5, 5.9, 1.5, 6.1),
                second.guid: box(-0.1, 2.5, 0.1, 3.5),
                space.guid: box(0.0, 0.0, 8.0, 6.0),
            },
            {first.guid: [space.guid], second.guid: [space.guid]},
            [matched],
        )
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].path[1][0], first.center[0], delta=0.05)
        self.assertLess(candidates[0].path[1][1], first.center[1] - 0.5)
        self.assertAlmostEqual(candidates[0].path[-2][1], second.center[1], delta=0.05)
        self.assertGreater(candidates[0].path[-2][0], second.center[0] + 0.5)

    def test_route_buffer_cannot_use_small_wall_tolerance(self):
        from shapely.geometry import box

        area = box(0.0, 0.0, 4.0, 4.0)
        self.assertFalse(_path_inside_area([(0.5, 0.03, 0.0), (3.5, 0.03, 0.0)], area))
        wall = Element("W", "IfcWall", "W", "W", center=(2.0, 0.1, 0.0))
        space = Element("S", "IfcSpace", "S", "S", center=(2.0, 2.0, 0.0))
        self.assertFalse(
            _route_avoids_walls(
                [(0.5, 0.1, 0.0), (3.5, 0.1, 0.0)],
                space,
                [wall],
                [],
                {wall.guid: box(0.0, 0.0, 4.0, 0.2)},
            )
        )

    def test_door_portal_does_not_open_a_perpendicular_wall(self):
        from shapely.geometry import Point, box

        value = oriented_door("D", 4.0, 3.0, (0.0, 1.0), (1.0, 0.0))
        value.extra["doorHostGuid"] = "HOST"
        value.bbox_min = (3.9, 2.5, 0.0)
        value.bbox_max = (4.1, 3.5, 2.1)
        space = Element(
            "S",
            "IfcSpace",
            "S",
            "S",
            center=(4.0, 3.0, 1.0),
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(8.0, 6.0, 3.0),
        )
        host = Element(
            "HOST",
            "IfcWall",
            "HOST",
            "HOST",
            center=(4.0, 3.0, 1.0),
            bbox_min=(3.9, 0.0, 0.0),
            bbox_max=(4.1, 6.0, 3.0),
        )
        crossing = Element(
            "CROSS",
            "IfcWall",
            "CROSS",
            "CROSS",
            center=(4.0, 3.0, 1.0),
            bbox_min=(0.0, 2.9, 0.0),
            bbox_max=(8.0, 3.1, 3.0),
        )
        area = _space_walkable_area(
            space,
            [value],
            [host, crossing],
            {
                value.guid: box(3.9, 2.5, 4.1, 3.5),
                space.guid: box(0.0, 0.0, 8.0, 6.0),
                host.guid: box(3.9, 0.0, 4.1, 6.0),
                crossing.guid: box(0.0, 2.9, 8.0, 3.1),
            },
        )
        self.assertFalse(area.covers(Point(4.0, 3.0)))

    def test_door_opens_a_nearby_parallel_wall_layer(self):
        from shapely.geometry import box

        value = oriented_door("D", 4.0, 3.0, (0.0, 1.0), (1.0, 0.0))
        value.extra["doorHostGuid"] = "HOST"
        layer = Element("LAYER", "IfcWall", "LAYER", "LAYER", center=(4.24, 3.0, 1.0))
        crossing = Element("CROSS", "IfcWall", "CROSS", "CROSS", center=(4.0, 3.0, 1.0))
        footprints = {
            value.guid: box(3.9, 2.5, 4.1, 3.5),
            layer.guid: box(4.22, 0.0, 4.24, 6.0),
            crossing.guid: box(0.0, 3.12, 8.0, 3.14),
        }
        self.assertTrue(_door_opens_wall(value, layer, footprints))
        self.assertFalse(_door_opens_wall(value, crossing, footprints))

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

    def test_long_narrow_corridor_requires_a_passing_area(self):
        from shapely.geometry import box

        corridor = Element("S", "IfcSpace", "S", "S", center=(8.0, 0.85, 0.0))
        result = _corridor_movement_area_region(
            corridor,
            box(0.0, 0.0, 16.0, 1.7),
            [((0.2, 0.85), (15.8, 0.85))],
        )
        self.assertAlmostEqual(result["measured"], 15.6, delta=0.01)
        self.assertEqual(result["gaps"][0]["required"], 15.0)
        self.assertEqual(result["gaps"][0]["movement_space_m"], 1.8)

    def test_wide_corridor_provides_continuous_passing_space(self):
        from shapely.geometry import box

        corridor = Element("S", "IfcSpace", "S", "S", center=(8.0, 1.0, 0.0))
        result = _corridor_movement_area_region(
            corridor,
            box(0.0, 0.0, 16.0, 2.0),
            [((0.9, 1.0), (15.1, 1.0))],
        )
        self.assertEqual(result["measured"], 0.0)
        self.assertEqual(result["gaps"], [])

    def test_short_narrow_corridor_does_not_need_an_intermediate_passing_area(self):
        from shapely.geometry import box

        corridor = Element("S", "IfcSpace", "S", "S", center=(7.0, 0.85, 0.0))
        result = _corridor_movement_area_region(
            corridor,
            box(0.0, 0.0, 14.0, 1.7),
            [((0.2, 0.85), (13.8, 0.85))],
        )
        self.assertLessEqual(result["measured"], 15.0)
        self.assertEqual(len(result["gaps"]), 1)

    def test_middle_passing_area_breaks_a_long_narrow_interval(self):
        from shapely.geometry import box
        from shapely.ops import unary_union

        corridor = Element("S", "IfcSpace", "S", "S", center=(10.0, 1.0, 0.0))
        area = unary_union([box(0.0, 0.15, 20.0, 1.85), box(9.0, 0.0, 11.0, 2.0)])
        result = _corridor_movement_area_region(
            corridor,
            area,
            [((0.2, 1.0), (19.8, 1.0))],
        )
        self.assertLessEqual(result["measured"], 15.0)
        self.assertEqual(len(result["gaps"]), 2)

    def test_each_disconnected_passing_area_gap_is_recorded(self):
        from shapely.geometry import box
        from shapely.ops import unary_union

        corridor = Element("S", "IfcSpace", "S", "S", center=(18.0, 0.85, 0.0))
        area = unary_union([box(0.0, 0.0, 16.0, 1.7), box(20.0, 0.0, 36.0, 1.7)])
        result = _corridor_movement_area_region(
            corridor,
            area,
            [((0.2, 0.85), (15.8, 0.85)), ((20.2, 0.85), (35.8, 0.85))],
        )
        self.assertEqual(len(result["gaps"]), 2)
        self.assertTrue(all(gap["measured"] > 15.0 for gap in result["gaps"]))
        self.assertEqual(len({gap["evidence_id"] for gap in result["gaps"]}), 2)

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
        self.assertAlmostEqual(measurements["routeTurningSpaceM"], 1.2, delta=0.02)

    def test_turning_space_measures_a_square_instead_of_a_circle(self):
        from shapely.geometry import Point

        area = Point(0.0, 0.0).buffer(0.75, quad_segs=64)
        side, _point = _route_turning_space_measurement([(0.0, 0.0, 0.0)], area)
        self.assertAlmostEqual(side, 0.75 * 2 ** 0.5, delta=0.02)

    def test_turning_space_accepts_a_rotated_required_square(self):
        from shapely.affinity import rotate
        from shapely.geometry import box

        area = rotate(box(-0.75, -0.75, 0.75, 0.75), 30.0, origin=(0.0, 0.0))
        side, _point = _route_turning_space_measurement([(0.0, 0.0, 0.0)], area)
        self.assertAlmostEqual(side, 1.5, delta=0.01)


if __name__ == "__main__":
    unittest.main()
