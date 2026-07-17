import json
import tempfile
import unittest
from pathlib import Path

from backend.navigation import NavigationError, NavigationPackage, build_navigation_package


def element(guid, kind, bbox, *, center=None, width=None, extra=None):
    return {
        "guid": guid,
        "ifcType": kind,
        "name": guid,
        "bboxMin": [bbox[0], bbox[1], 0],
        "bboxMax": [bbox[2], bbox[3], 2.5],
        "center": list(center or ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, 1.25)),
        "width": width,
        "extra": extra or {},
    }


def model(door_width=1.0, length=4.0, column=None):
    items = [
        element("left", "IfcSpace", (0, 0, 2, 2)),
        element("right", "IfcSpace", (2, 0, length, 2)),
        element("wall", "IfcWall", (1.95, 0, 2.05, 2)),
        element(
            "door",
            "IfcDoor",
            (1.95, 1 - door_width / 2, 2.05, 1 + door_width / 2),
            center=(2, 1, 1),
            width=door_width,
            extra={"derivedDoorWidthM": door_width},
        ),
    ]
    if column:
        items.append(element("column", "IfcColumn", column))
    return {
        "summary": {},
        "sources": {},
        "elements": items,
        "floors": [
            {
                "name": "Ground",
                "elevation": 0,
                "elementGuids": [item["guid"] for item in items],
                "spaceGuids": ["left", "right"],
                "doorGuids": ["door"],
            }
        ],
    }


class NavigationTests(unittest.TestCase):
    def package(self, data):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        app_data = root / "app_data.json"
        app_data.write_text(json.dumps(data), encoding="utf-8")
        build_navigation_package(app_data, root)
        return temporary, NavigationPackage(root)

    def test_accessible_door_connects_exact_clicked_points(self):
        temporary, navigation = self.package(model(door_width=1.0))
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.8, 1.0], [3.2, 1.0])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["path"][0][:2], [0.8, 1.0])
        self.assertEqual(result["path"][-1][:2], [3.2, 1.0])
        self.assertTrue(result["audit"]["orthogonal"])
        self.assertTrue(result["audit"]["collisionFree"])
        self.assertTrue(result["audit"]["endpointsExact"])
        self.assertEqual(result["routeWidthM"], 1.50)

    def test_exact_aligned_points_do_not_keep_grid_centre_detour(self):
        temporary, navigation = self.package(model(door_width=1.0))
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.80, 1.0], [0.82, 1.0])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["path"], [[0.8, 1.0, 0.0], [0.82, 1.0, 0.0]])
        self.assertAlmostEqual(result["distanceM"], 0.02)
        self.assertTrue(result["audit"]["endpointsExact"])
        self.assertTrue(result["audit"]["orthogonal"])
        self.assertTrue(result["audit"]["collisionFree"])

    def test_narrow_door_is_not_carved_through_the_wall(self):
        temporary, navigation = self.package(model(door_width=0.80))
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.8, 1.0], [3.2, 1.0])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "no_accessible_connection")
        self.assertGreaterEqual(len(result["path"]), 2)
        self.assertTrue(result["audit"]["orthogonal"])
        self.assertTrue(result["audit"]["collisionFree"])
        self.assertFalse(result["audit"]["reachesDestination"])
        self.assertNotEqual(result["path"][-1][:2], [3.2, 1.0])

    def test_start_inside_clearance_envelope_is_rejected(self):
        temporary, navigation = self.package(model(column=(0.9, 0.9, 1.1, 1.1)))
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.7, 1.0], [3.2, 1.0])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "start_not_walkable")

    def test_route_crosses_multiple_streamed_tiles(self):
        data = model(door_width=1.2, length=12.0)
        data["elements"][1]["bboxMax"][0] = 12.0
        temporary, navigation = self.package(data)
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.8, 1.0], [11.2, 1.0])
        self.assertEqual(result["status"], "pass")
        self.assertGreaterEqual(result["streamedTiles"], 3)

    def test_accessible_route_is_exactly_reversible(self):
        items = [
            element("space", "IfcSpace", (0, 0, 10, 6)),
            element("column", "IfcColumn", (4, 1, 6, 4)),
        ]
        data = {
            "summary": {},
            "sources": {},
            "elements": items,
            "floors": [
                {
                    "name": "Ground",
                    "elevation": 0,
                    "elementGuids": [item["guid"] for item in items],
                    "spaceGuids": ["space"],
                    "doorGuids": [],
                }
            ],
        }
        temporary, navigation = self.package(data)
        self.addCleanup(temporary.cleanup)
        forward = navigation.route("Ground", [1.0, 2.0], [9.0, 2.0])
        reverse = navigation.route("Ground", [9.0, 2.0], [1.0, 2.0])
        self.assertEqual(forward["status"], "pass")
        self.assertEqual(reverse["status"], "pass")
        self.assertEqual(forward["distanceM"], reverse["distanceM"])
        self.assertEqual(forward["path"], list(reversed(reverse["path"])))

    def test_space_below_accessible_route_width_is_not_walkable(self):
        data = model(door_width=1.0)
        data["elements"][1]["extra"]["derivedClearSpaceWidthM"] = 1.20
        data["elements"].append(
            element("overlap", "IfcSpace", (0, 0, 4, 2), extra={"derivedClearSpaceWidthM": 2.0})
        )
        data["floors"][0]["elementGuids"].append("overlap")
        temporary, navigation = self.package(data)
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.8, 1.0], [3.2, 1.0])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "destination_not_walkable")
        self.assertTrue(result["audit"]["collisionFree"])
        self.assertFalse(result["audit"]["reachesDestination"])

    def test_route_referenced_stair_is_blocked_even_when_floor_index_omits_it(self):
        data = model(door_width=1.0)
        stair = element("stair", "IfcStairFlight", (2.8, 0.5, 3.4, 1.5))
        data["elements"].append(stair)
        data["routeEdges"] = [{"edgeId": "E-stair", "startGuid": "door", "endGuid": "stair"}]
        data["floors"][0]["routeEdgeIds"] = ["E-stair"]
        temporary, navigation = self.package(data)
        self.addCleanup(temporary.cleanup)
        result = navigation.route("Ground", [0.8, 1.0], [3.1, 1.0])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "destination_not_walkable")
        self.assertTrue(result["audit"]["collisionFree"])
        self.assertFalse(result["audit"]["reachesDestination"])

    def test_blocked_candidate_routes_around_wall_to_nearest_reachable_boundary(self):
        items = [
            element("space", "IfcSpace", (0, 0, 6, 4)),
            element("wall", "IfcWall", (2.8, 0, 3.2, 2.0)),
        ]
        data = {
            "summary": {},
            "sources": {},
            "elements": items,
            "floors": [
                {
                    "name": "Ground",
                    "elevation": 0,
                    "elementGuids": [item["guid"] for item in items],
                    "spaceGuids": ["space"],
                    "doorGuids": [],
                }
            ],
        }
        temporary, navigation = self.package(data)
        self.addCleanup(temporary.cleanup)
        reachable = navigation.route("Ground", [1.0, 1.0], [5.4, 1.0])
        blocked = navigation.route("Ground", [1.0, 1.0], [6.5, 1.0])
        self.assertEqual(reachable["status"], "pass")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "destination_not_walkable")
        self.assertGreater(blocked["path"][-1][0], reachable["end"][0])
        self.assertTrue(blocked["audit"]["orthogonal"])
        self.assertTrue(blocked["audit"]["collisionFree"])
        self.assertFalse(blocked["audit"]["reachesDestination"])

    def test_corrupt_tile_is_detected_instead_of_used(self):
        temporary, navigation = self.package(model())
        self.addCleanup(temporary.cleanup)
        floor = navigation.index["floors"]["Ground"]
        tile = next(iter(floor["tileIndex"].values()))
        (navigation.root / tile["file"]).write_bytes(b"broken")
        navigation.tile_cache.clear()
        with self.assertRaisesRegex(NavigationError, "integrity"):
            navigation.route("Ground", [0.8, 1.0], [3.2, 1.0])


if __name__ == "__main__":
    unittest.main()
