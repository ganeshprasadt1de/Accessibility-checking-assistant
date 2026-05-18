from __future__ import annotations

import html
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell
import ifcopenshell.geom
from rdflib import Graph, Namespace

from accessibility.model import GeometryFinding
from accessibility.route_path import boxes_intersect
from accessibility.route_path import orthogonal_route_points
from accessibility.route_path import path_segments
from accessibility.route_path import segment_envelope

try:
    import plotly.graph_objects as go
except ModuleNotFoundError:
    go = None

try:
    import open3d as o3d  # noqa: F401
    OPEN3D_AVAILABLE = True
except Exception:
    OPEN3D_AVAILABLE = False


ACC = Namespace("http://example.org/accessibility#")

VOXEL_SIZE_M = 0.30
CLEAR_WIDTH_M = 0.90
CLEAR_LENGTH_M = 1.20
CLEAR_HEIGHT_M = 2.05
MAX_OBSTACLE_BOXES = 850
MAX_ROUTE_SEGMENTS = 1200
MAX_SHOWN_VOXELS = 2200
MAX_OCCUPIED_VOXELS = 250000
MAX_OPEN3D_VOXELS = 60000

OBSTACLE_CLASSES = [
    "IfcWall",
    "IfcWallStandardCase",
    "IfcColumn",
    "IfcStair",
    "IfcStairFlight",
    "IfcRailing",
    "IfcFurnishingElement",
    "IfcBuildingElementProxy",
    "IfcCovering",
]


def make_voxel_clearance_viewer(uploaded_file, graph: Graph) -> tuple[str | None, dict[str, int | float | str], list[GeometryFinding]]:
    if go is None:
        return None, {"message": "Plotly is not installed."}, [
            _finding("Model data", "IFC model", "Voxel route clearance", "not checked", "Plotly is not installed.", "Install Plotly.")
        ]

    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    obstacle_boxes = _obstacle_boxes(model, settings)
    route_edges = _route_edges(graph)
    route_segments = _right_angle_segments(route_edges, obstacle_boxes)
    occupied_voxels = _occupied_voxels(obstacle_boxes)
    open3d_voxel_count = _open3d_voxel_count(occupied_voxels)

    fig = go.Figure()
    _add_voxel_points(fig, occupied_voxels)
    failed_segments = 0
    checked_segments = 0
    collision_voxels_total = 0
    findings: list[GeometryFinding] = []

    for segment in route_segments[:MAX_ROUTE_SEGMENTS]:
        checked_segments += 1
        envelope = segment_envelope(segment["start"], segment["end"], CLEAR_WIDTH_M, CLEAR_HEIGHT_M)
        test_voxels = _box_voxels(envelope)
        collisions = test_voxels.intersection(occupied_voxels)
        passed = not collisions and segment["route_pass"]
        if not passed:
            failed_segments += 1
            collision_voxels_total += len(collisions)
            findings.append(
                _finding(
                    "Mobility",
                    segment["label"],
                    "Voxel route clearance",
                    "failed",
                    _failure_reason(segment, len(collisions)),
                    "Remove the obstacle, widen the route, or use another accessible route.",
                )
            )
        _add_route_segment(fig, segment, passed, len(collisions))

    _add_wheelchair_trace(fig, route_segments)

    if checked_segments and failed_segments == 0:
        findings.append(
            _finding(
                "Mobility",
                "IFC model",
                "Voxel route clearance",
                "passed",
                "The wheelchair clearance volume did not collide with occupied route voxels.",
                "Keep the route clear and review detailed design documents before construction.",
            )
        )
    if not route_segments:
        findings.append(
            _finding(
                "Model data",
                "IFC model",
                "Voxel route clearance",
                "not checked",
                "No route segments were available.",
                "Export spaces, doors, and space boundaries so accessible route edges can be built.",
            )
        )

    fig.update_layout(
        height=940,
        margin={"l": 0, "r": 0, "t": 20, "b": 0},
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
        font={"color": "#edf2f7"},
        scene={
            "xaxis": {"title": "X", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "yaxis": {"title": "Y", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "zaxis": {"title": "Z", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "aspectmode": "data",
            "camera": {"eye": {"x": 2.2, "y": -2.4, "z": 1.8}, "up": {"x": 0, "y": 0, "z": 1}},
        },
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )

    stats = {
        "open3d_available": str(OPEN3D_AVAILABLE).lower(),
        "voxel_engine": "Open3D detected" if OPEN3D_AVAILABLE else "internal Python grid",
        "voxel_size_m": VOXEL_SIZE_M,
        "occupied_voxels": len(occupied_voxels),
        "shown_voxels": min(len(occupied_voxels), MAX_SHOWN_VOXELS),
        "open3d_voxel_cells": open3d_voxel_count,
        "open3d_checked_voxels": min(len(occupied_voxels), MAX_OPEN3D_VOXELS) if OPEN3D_AVAILABLE else 0,
        "route_segments": len(route_segments),
        "checked_route_segments": checked_segments,
        "failed_route_segments": failed_segments,
        "collision_voxels": collision_voxels_total,
        "clearance_width_m": CLEAR_WIDTH_M,
        "clearance_length_m": CLEAR_LENGTH_M,
        "clearance_height_m": CLEAR_HEIGHT_M,
    }
    return _viewer_html(fig), stats, findings


def _open3d_voxel_count(voxels: set[tuple[int, int, int]]) -> int:
    if not OPEN3D_AVAILABLE or not voxels:
        return 0
    try:
        import numpy as np

        shown = list(voxels)[:MAX_OPEN3D_VOXELS]
        points = np.array(
            [[(ix + 0.5) * VOXEL_SIZE_M, (iy + 0.5) * VOXEL_SIZE_M, (iz + 0.5) * VOXEL_SIZE_M] for ix, iy, iz in shown],
            dtype=float,
        )
        cloud = o3d.geometry.PointCloud()
        cloud.points = o3d.utility.Vector3dVector(points)
        voxel_grid = o3d.geometry.VoxelGrid.create_from_point_cloud(cloud, voxel_size=VOXEL_SIZE_M)
        return len(voxel_grid.get_voxels())
    except Exception:
        return 0


def _route_edges(graph: Graph) -> list[dict[str, object]]:
    query = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?edge ?label ?pass ?fx ?fy ?fz ?tx ?ty ?tz ?dx ?dy ?dz
WHERE {
  ?edge a acc:RouteEdge ;
        rdfs:label ?label ;
        acc:routePass ?pass ;
        acc:fromSpace ?from ;
        acc:toSpace ?to .
  ?from acc:centerX ?fx ; acc:centerY ?fy ; acc:centerZ ?fz .
  ?to acc:centerX ?tx ; acc:centerY ?ty ; acc:centerZ ?tz .
  OPTIONAL { ?edge acc:doorCenterX ?dx ; acc:doorCenterY ?dy ; acc:doorCenterZ ?dz . }
}
"""
    route_edges = []
    for row in graph.query(query):
        fx, fy, fz = float(row.fx), float(row.fy), float(row.fz)
        tx, ty, tz = float(row.tx), float(row.ty), float(row.tz)
        if row.dx is None or row.dy is None or row.dz is None:
            dx, dy, dz = (fx + tx) / 2, (fy + ty) / 2, (fz + tz) / 2
        else:
            dx, dy, dz = float(row.dx), float(row.dy), float(row.dz)
        route_edges.append(
            {
                "label": str(row.label),
                "start": (fx, fy, fz),
                "door": (dx, dy, dz),
                "end": (tx, ty, tz),
                "route_pass": str(row["pass"]).lower() == "true",
            }
        )
    return route_edges


def _right_angle_segments(route_edges: list[dict[str, object]], obstacle_boxes: list[dict[str, object]]) -> list[dict[str, object]]:
    segments = []
    for edge in route_edges:
        def score_segment(start, end):
            envelope = segment_envelope(start, end, CLEAR_WIDTH_M, CLEAR_HEIGHT_M)
            return sum(1 for box in obstacle_boxes if boxes_intersect(envelope, box["bounds"]))

        points = orthogonal_route_points(edge["start"], edge["door"], edge["end"], score_segment=score_segment)
        for start, end in path_segments(points):
            segments.append({"label": edge["label"], "start": start, "end": end, "route_pass": edge["route_pass"]})
    return segments


def _obstacle_boxes(model, settings) -> list[dict[str, object]]:
    boxes = []
    seen = set()
    for class_name in OBSTACLE_CLASSES:
        try:
            elements = model.by_type(class_name)
        except RuntimeError:
            continue
        for element in elements:
            if element.id() in seen:
                continue
            seen.add(element.id())
            bounds = _element_bounds(settings, element)
            if bounds is None or _too_small(bounds):
                continue
            boxes.append({"label": _label(element), "bounds": bounds})
            if len(boxes) >= MAX_OBSTACLE_BOXES:
                return boxes
    return boxes


def _element_bounds(settings, element) -> tuple[float, float, float, float, float, float] | None:
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    verts = list(shape.geometry.verts)
    if len(verts) < 9:
        return None
    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def _too_small(bounds) -> bool:
    x0, x1, y0, y1, z0, z1 = bounds
    return (x1 - x0) < 0.03 or (y1 - y0) < 0.03 or (z1 - z0) < 0.03


def _occupied_voxels(boxes: list[dict[str, object]]) -> set[tuple[int, int, int]]:
    voxels = set()
    for item in boxes:
        voxels.update(_box_voxels(item["bounds"]))
        if len(voxels) >= MAX_OCCUPIED_VOXELS:
            return set(list(voxels)[:MAX_OCCUPIED_VOXELS])
    return voxels


def _box_voxels(bounds) -> set[tuple[int, int, int]]:
    x0, x1, y0, y1, z0, z1 = bounds
    ix0, ix1 = math.floor(x0 / VOXEL_SIZE_M), math.ceil(x1 / VOXEL_SIZE_M)
    iy0, iy1 = math.floor(y0 / VOXEL_SIZE_M), math.ceil(y1 / VOXEL_SIZE_M)
    iz0, iz1 = math.floor(z0 / VOXEL_SIZE_M), math.ceil(z1 / VOXEL_SIZE_M)
    voxels = set()
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            for iz in range(iz0, iz1 + 1):
                voxels.add((ix, iy, iz))
    return voxels


def _add_voxel_points(fig, voxels: set[tuple[int, int, int]]) -> None:
    shown = list(voxels)[:MAX_SHOWN_VOXELS]
    if not shown:
        return
    fig.add_trace(
        go.Scatter3d(
            x=[(ix + 0.5) * VOXEL_SIZE_M for ix, _iy, _iz in shown],
            y=[(iy + 0.5) * VOXEL_SIZE_M for _ix, iy, _iz in shown],
            z=[(iz + 0.5) * VOXEL_SIZE_M for _ix, _iy, iz in shown],
            mode="markers",
            marker={"size": 2, "color": "rgba(148, 163, 184, 0.38)"},
            name="Occupied voxels",
            hovertemplate="Occupied voxel<extra></extra>",
        )
    )


def _add_route_segment(fig, segment: dict[str, object], passed: bool, collision_count: int) -> None:
    start = segment["start"]
    end = segment["end"]
    color = "#2fbf71" if passed else "#ff3333"
    z = max(start[2], end[2]) + 0.14
    text = (
        f"{html.escape(str(segment['label']))}<br>"
        f"Voxel clearance: {'passed' if passed else 'failed'}.<br>"
        f"Collision voxels: {collision_count}."
    )
    fig.add_trace(
        go.Scatter3d(
            x=[start[0], end[0]],
            y=[start[1], end[1]],
            z=[z, z],
            mode="lines",
            line={"color": color, "width": 8},
            hovertemplate=text + "<extra></extra>",
            name="Passed voxel route" if passed else "Failed voxel route",
            showlegend=False,
        )
    )


def _add_wheelchair_trace(fig, route_segments: list[dict[str, object]]) -> None:
    if not route_segments:
        return
    segment = route_segments[0]
    start = segment["start"]
    end = segment["end"]
    direction = math.atan2(end[1] - start[1], end[0] - start[0])
    x, y, z = start[0], start[1], max(start[2], end[2]) + 0.08
    parts = _wheelchair_parts(x, y, z, direction)
    for part in parts:
        fig.add_trace(part)
    fig.add_trace(
        go.Scatter3d(
            x=[start[0], end[0]],
            y=[start[1], end[1]],
            z=[z + 0.45, z + 0.45],
            mode="markers+lines",
            marker={"size": [8, 4], "color": ["#ffd166", "#8ecae6"]},
            line={"color": "#ffd166", "width": 3, "dash": "dot"},
            name="Wheelchair simulation path",
            hovertemplate="The visible wheelchair/person marker follows the route. The pass/fail result comes from the rectangular clearance volume tested against occupied voxels.<extra></extra>",
        )
    )


def _wheelchair_parts(x: float, y: float, z: float, angle: float):
    def p(dx, dy, dz):
        ca, sa = math.cos(angle), math.sin(angle)
        return x + dx * ca - dy * sa, y + dx * sa + dy * ca, z + dz

    left_wheel = _circle_points(p(0, -0.34, 0.36), 0.28, angle)
    right_wheel = _circle_points(p(0, 0.34, 0.36), 0.28, angle)
    seat = [p(-0.18, -0.32, 0.62), p(0.34, -0.32, 0.62), p(0.34, 0.32, 0.62), p(-0.18, 0.32, 0.62)]
    head = p(0.05, 0, 1.35)
    body = [p(0.05, 0, 1.18), p(0.0, 0, 0.78)]
    return [
        go.Scatter3d(x=[item[0] for item in left_wheel], y=[item[1] for item in left_wheel], z=[item[2] for item in left_wheel], mode="lines", line={"color": "#f8fafc", "width": 5}, name="Wheelchair/person marker", showlegend=True),
        go.Scatter3d(x=[item[0] for item in right_wheel], y=[item[1] for item in right_wheel], z=[item[2] for item in right_wheel], mode="lines", line={"color": "#f8fafc", "width": 5}, name="Wheelchair wheels", showlegend=False),
        go.Scatter3d(x=[item[0] for item in seat + [seat[0]]], y=[item[1] for item in seat + [seat[0]]], z=[item[2] for item in seat + [seat[0]]], mode="lines", line={"color": "#8ecae6", "width": 6}, name="Wheelchair seat", showlegend=False),
        go.Scatter3d(x=[item[0] for item in body], y=[item[1] for item in body], z=[item[2] for item in body], mode="lines+markers", marker={"size": [9, 6], "color": ["#ffd166", "#ffd166"]}, line={"color": "#ffd166", "width": 6}, name="Person", showlegend=False),
        go.Scatter3d(x=[head[0]], y=[head[1]], z=[head[2]], mode="markers", marker={"size": 10, "color": "#ffd166"}, name="Person head", showlegend=False),
    ]


def _circle_points(center: tuple[float, float, float], radius: float, angle: float) -> list[tuple[float, float, float]]:
    points = []
    ca, sa = math.cos(angle), math.sin(angle)
    cx, cy, cz = center
    for index in range(32):
        theta = 2 * math.pi * index / 31
        dx = math.cos(theta) * radius
        dz = math.sin(theta) * radius
        points.append((cx + dx * ca, cy + dx * sa, cz + dz))
    return points


def _failure_reason(segment: dict[str, object], collision_count: int) -> str:
    reasons = []
    if not segment["route_pass"]:
        reasons.append("The stored route edge already failed the door width or level-change rule.")
    if collision_count:
        reasons.append(f"The clearance volume collided with {collision_count} occupied voxel cells.")
    return " ".join(reasons) or "The voxel clearance check failed."


def _viewer_html(fig) -> str:
    plot = fig.to_html(include_plotlyjs=True, full_html=False)
    engine_text = "Open3D is available." if OPEN3D_AVAILABLE else "Open3D is not installed; the app used the same voxel-grid method in pure Python."
    return f"""
<div style="font-family: Arial, sans-serif; color: #edf2f7; background: #0b0f17; padding: 14px; min-height: 980px;">
  <div style="border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 10px; background: #111827; line-height: 1.45;">
    This model checks a wheelchair-sized clearance volume against occupied 3D voxels. The visible wheelchair and person explain the route movement; the pass/fail result comes from the clearance volume, not from the decorative marker. {engine_text}
  </div>
  {plot}
</div>
"""


def _label(element) -> str:
    return str(getattr(element, "LongName", None) or getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))


def _finding(category: str, element: str, check: str, result: str, reason: str, fix: str) -> GeometryFinding:
    return GeometryFinding(category=category, element=element, check=check, result=result, reason=reason, fix=fix)
