import unittest
from types import SimpleNamespace

from backend.geometry import _shape_floor_slope_percent, _shape_plan_size


class GeometryTests(unittest.TestCase):
    def test_corridor_floor_slope_uses_the_lower_surface(self):
        points = [
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 0.3),
            (10.0, 2.0, 0.3),
            (0.0, 2.0, 0.0),
            (0.0, 0.0, 3.0),
            (10.0, 0.0, 3.3),
            (10.0, 2.0, 3.3),
            (0.0, 2.0, 3.0),
        ]
        shape = SimpleNamespace(
            geometry=SimpleNamespace(
                verts=tuple(value for point in points for value in point),
                faces=(0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6),
            )
        )
        self.assertAlmostEqual(_shape_floor_slope_percent(shape), 3.0, delta=0.01)
        width, length = _shape_plan_size(shape)
        self.assertAlmostEqual(width, 2.0, delta=0.01)
        self.assertAlmostEqual(length, 10.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
