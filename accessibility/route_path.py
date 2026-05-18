from __future__ import annotations

from collections.abc import Callable


Point3D = tuple[float, float, float]
Bounds3D = tuple[float, float, float, float, float, float]


def orthogonal_route_points(
    start: Point3D,
    door: Point3D,
    end: Point3D,
    score_segment: Callable[[Point3D, Point3D], int] | None = None,
) -> list[Point3D]:
    z = max(start[2], door[2], end[2])
    start = (start[0], start[1], z)
    door = (door[0], door[1], z)
    end = (end[0], end[1], z)
    candidates = [
        _clean_points([start, (door[0], start[1], z), door, (door[0], end[1], z), end]),
        _clean_points([start, (start[0], door[1], z), door, (end[0], door[1], z), end]),
    ]
    if score_segment is None:
        return min(candidates, key=_path_length)
    return min(candidates, key=lambda points: (_path_score(points, score_segment), _path_length(points)))


def path_segments(points: list[Point3D]) -> list[tuple[Point3D, Point3D]]:
    return [(points[index], points[index + 1]) for index in range(len(points) - 1) if _distance(points[index], points[index + 1]) > 0.05]


def segment_envelope(start: Point3D, end: Point3D, clear_width: float, clear_height: float) -> Bounds3D:
    half_width = clear_width / 2
    z0 = min(start[2], end[2])
    return (
        min(start[0], end[0]) - half_width,
        max(start[0], end[0]) + half_width,
        min(start[1], end[1]) - half_width,
        max(start[1], end[1]) + half_width,
        z0,
        z0 + clear_height,
    )


def boxes_intersect(a: Bounds3D, b: Bounds3D) -> bool:
    ax0, ax1, ay0, ay1, az0, az1 = a
    bx0, bx1, by0, by1, bz0, bz1 = b
    return ax0 <= bx1 and ax1 >= bx0 and ay0 <= by1 and ay1 >= by0 and az0 <= bz1 and az1 >= bz0


def _path_score(points: list[Point3D], score_segment: Callable[[Point3D, Point3D], int]) -> int:
    return sum(score_segment(start, end) for start, end in path_segments(points))


def _path_length(points: list[Point3D]) -> float:
    return sum(_distance(start, end) for start, end in path_segments(points))


def _distance(a: Point3D, b: Point3D) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _clean_points(points: list[Point3D]) -> list[Point3D]:
    cleaned = []
    for point in points:
        if cleaned and _distance(cleaned[-1], point) <= 0.05:
            continue
        cleaned.append(point)
    return cleaned
