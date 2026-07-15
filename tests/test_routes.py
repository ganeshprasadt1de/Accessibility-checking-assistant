import unittest
from pathlib import Path
from unittest.mock import patch

from backend.model import Element
from backend.routes import _space_boundary_route_edges


class RouteTests(unittest.TestCase):
    @patch("backend.routes._stair_approach_route_edges", return_value=[])
    @patch("backend.routes._path_through_space", side_effect=RuntimeError("No collision-free route."))
    @patch("backend.routes._build_occupancy_grid", return_value={})
    @patch("backend.routes.obstacle_elements", return_value=[])
    @patch("backend.routes._door_to_spaces")
    @patch("ifcopenshell.open")
    def test_unroutable_door_pair_is_recorded_and_skipped(
        self,
        open_model,
        door_to_spaces,
        _obstacles,
        _grid,
        _path,
        _stairs,
    ):
        open_model.return_value = object()
        door_to_spaces.return_value = {"A": ["S"], "B": ["S"]}
        elements = [
            Element("A", "IfcDoor", "A", "A", center=(0.0, 0.0, 0.0)),
            Element("B", "IfcDoor", "B", "B", center=(1.0, 0.0, 0.0)),
            Element("S", "IfcSpace", "S", "Space S", center=(0.5, 0.5, 0.0)),
        ]
        skipped = []

        edges = _space_boundary_route_edges(Path("model.ifc"), elements, skipped)

        self.assertEqual(edges, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["startGuid"], "A")
        self.assertEqual(skipped[0]["endGuid"], "B")
        self.assertEqual(skipped[0]["spaceGuid"], "S")


if __name__ == "__main__":
    unittest.main()
