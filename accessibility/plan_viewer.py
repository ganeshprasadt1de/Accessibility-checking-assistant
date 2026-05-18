from __future__ import annotations

import html
from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell
import ifcopenshell.geom
from rdflib import Graph, Namespace
from shapely.geometry import LineString
from shapely.geometry import MultiPoint
from shapely.geometry import Point
from shapely.geometry import Polygon
from shapely.ops import unary_union

from accessibility.route_path import orthogonal_route_points
from accessibility.route_path import path_segments

try:
    import plotly.graph_objects as go
except ModuleNotFoundError:
    go = None


ACC = Namespace("http://example.org/accessibility#")

MAX_OBSTACLE_FOOTPRINTS = 500
MAX_ROUTE_EDGES = 260
WHEELCHAIR_CLEAR_WIDTH_M = 0.90
DOOR_OPENING_TOLERANCE_M = 0.75

OBSTACLE_CLASSES = [
    "IfcWall",
    "IfcWallStandardCase",
    "IfcCurtainWall",
    "IfcColumn",
    "IfcStair",
    "IfcStairFlight",
    "IfcRailing",
    "IfcFurnishingElement",
    "IfcBuildingElementProxy",
]


def make_2d_route_plan(uploaded_file, graph: Graph) -> tuple[str | None, dict[str, int | str]]:
    if go is None:
        return None, {"message": "Plotly is not installed."}

    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    footprints = _obstacle_footprints(model, settings)
    route_edges = _route_edges(graph)
    fig = go.Figure()

    for item in footprints[:MAX_OBSTACLE_FOOTPRINTS]:
        geometry = item["geometry"]
        polygons = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
        for polygon in polygons[:12]:
            if polygon.is_empty or not hasattr(polygon, "exterior"):
                continue
            xs, ys = polygon.exterior.xy
            fig.add_trace(
                go.Scatter(
                    x=list(xs),
                    y=list(ys),
                    mode="lines",
                    fill="toself",
                    fillcolor="rgba(148, 163, 184, 0.16)",
                    line={"color": "rgba(148, 163, 184, 0.36)", "width": 1},
                    text=f"Obstacle footprint<br>{html.escape(item['label'])}",
                    hovertemplate="%{text}<extra></extra>",
                    name="Obstacle footprint",
                    showlegend=False,
                )
            )

    for edge in route_edges[:MAX_ROUTE_EDGES]:
        def score_segment(start, end):
            route_strip = _segment_clearance_strip(start, end)
            return sum(1 for item in footprints if _hits_obstacle(route_strip, item["geometry"], edge["door"]))

        points = orthogonal_route_points(edge["start"], edge["door"], edge["end"], score_segment=score_segment)
        route_strip = _path_clearance_strip(points)
        if route_strip is not None and not route_strip.is_empty:
            strip_polygons = list(route_strip.geoms) if hasattr(route_strip, "geoms") else [route_strip]
            for strip_polygon in strip_polygons:
                if strip_polygon.is_empty or not hasattr(strip_polygon, "exterior"):
                    continue
                xs, ys = strip_polygon.exterior.xy
                fig.add_trace(
                    go.Scatter(
                        x=list(xs),
                        y=list(ys),
                        mode="lines",
                        fill="toself",
                        fillcolor="rgba(47, 191, 113, 0.07)" if edge["route_pass"] else "rgba(255, 51, 51, 0.07)",
                        line={"color": "rgba(47, 191, 113, 0.18)" if edge["route_pass"] else "rgba(255, 51, 51, 0.18)", "width": 1},
                        text=f"{html.escape(edge['label'])}<br>2D clearance strip: {WHEELCHAIR_CLEAR_WIDTH_M:.2f} m",
                        hovertemplate="%{text}<extra></extra>",
                        name="Clearance strip",
                        showlegend=False,
                    )
                )

    failed_edges = 0
    drawn_edges = 0
    arrows = []
    for edge in route_edges[:MAX_ROUTE_EDGES]:
        def score_segment(start, end):
            route_strip = _segment_clearance_strip(start, end)
            return sum(1 for item in footprints if _hits_obstacle(route_strip, item["geometry"], edge["door"]))

        points = orthogonal_route_points(edge["start"], edge["door"], edge["end"], score_segment=score_segment)
        hit_labels = _path_intersections(points, footprints, edge["door"])
        failed = bool(hit_labels) or not edge["route_pass"]
        if failed:
            failed_edges += 1
        drawn_edges += 1
        color = "#ff3333" if failed else "#2fbf71"
        text = _route_text(edge, hit_labels)
        fig.add_trace(
            go.Scatter(
                x=[point[0] for point in points],
                y=[point[1] for point in points],
                mode="lines+markers",
                line={"color": color, "width": 3, "shape": "linear"},
                marker={"size": 5, "color": color},
                text=[text for _point in points],
                hovertemplate="%{text}<extra></extra>",
                name="Failed 2D route" if failed else "Passed 2D route",
                showlegend=drawn_edges <= 2,
            )
        )
        for start, end in path_segments(points):
            arrows.append((start, end, color))

    for start, end, color in arrows[:350]:
        fig.add_annotation(
            x=end[0],
            y=end[1],
            ax=start[0],
            ay=start[1],
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            showarrow=True,
            arrowhead=3,
            arrowsize=1.1,
            arrowwidth=1.6,
            arrowcolor=color,
            opacity=0.82,
        )

    fig.update_layout(
        height=760,
        margin={"l": 0, "r": 0, "t": 20, "b": 0},
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
        font={"color": "#edf2f7"},
        xaxis={"title": "X", "gridcolor": "#253142", "zeroline": False, "scaleanchor": "y", "scaleratio": 1},
        yaxis={"title": "Y", "gridcolor": "#253142", "zeroline": False},
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )
    stats = {
        "route_edges": len(route_edges),
        "drawn_route_edges": drawn_edges,
        "failed_2d_route_edges": failed_edges,
        "obstacle_footprints": len(footprints),
        "shown_obstacle_footprints": min(len(footprints), MAX_OBSTACLE_FOOTPRINTS),
        "wheelchair_clear_width_m": f"{WHEELCHAIR_CLEAR_WIDTH_M:.2f}",
    }
    return _viewer_html(fig), stats


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


def _obstacle_footprints(model, settings) -> list[dict[str, object]]:
    footprints = []
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
            geometry = _footprint_geometry(settings, element)
            if geometry is None or geometry.is_empty:
                continue
            x0, y0, x1, y1 = geometry.bounds
            if abs(x1 - x0) < 0.03 or abs(y1 - y0) < 0.03:
                continue
            footprints.append({"label": _label(element), "bounds": (x0, y0, x1, y1), "geometry": geometry})
    return footprints


def _footprint_geometry(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    verts = list(shape.geometry.verts)
    if len(verts) < 9:
        return None
    points = [(verts[index], verts[index + 1]) for index in range(0, len(verts), 3)]
    faces = list(getattr(shape.geometry, "faces", []) or [])
    triangles = []
    for index in range(0, len(faces), 3):
        try:
            triangle_points = [points[faces[index]], points[faces[index + 1]], points[faces[index + 2]]]
        except IndexError:
            continue
        polygon = Polygon(triangle_points)
        if polygon.is_valid and polygon.area > 0.0001:
            triangles.append(polygon)
    if triangles:
        merged = unary_union(triangles)
        return merged.buffer(0)
    return MultiPoint(points).convex_hull


def _segment_clearance_strip(start, end):
    return LineString([(start[0], start[1]), (end[0], end[1])]).buffer(
        WHEELCHAIR_CLEAR_WIDTH_M / 2,
        cap_style=3,
        join_style=2,
    )


def _path_clearance_strip(points):
    strips = [_segment_clearance_strip(start, end) for start, end in path_segments(points)]
    if not strips:
        return None
    return unary_union(strips).buffer(0)


def _path_intersections(points, footprints, door) -> list[str]:
    labels = []
    for start, end in path_segments(points):
        route_strip = _segment_clearance_strip(start, end)
        for item in footprints:
            if _hits_obstacle(route_strip, item["geometry"], door):
                labels.append(item["label"])
    return sorted(set(labels))


def _hits_obstacle(route_strip, obstacle, door) -> bool:
    if not route_strip.intersects(obstacle):
        return False
    intersection = route_strip.intersection(obstacle)
    if intersection.is_empty:
        return False
    door_clearance = Point(door[0], door[1]).buffer(DOOR_OPENING_TOLERANCE_M)
    outside_door = intersection.difference(door_clearance)
    return not outside_door.is_empty and outside_door.area > 0.0001


def _route_text(edge, hit_labels: list[str]) -> str:
    if not hit_labels and edge["route_pass"]:
        return f"{html.escape(edge['label'])}<br>2D clearance strip passed."
    reasons = []
    if hit_labels:
        shown = ", ".join(html.escape(label) for label in hit_labels[:5])
        extra = "" if len(hit_labels) <= 5 else f", and {len(hit_labels) - 5} more"
        reasons.append(f"clearance strip intersects {len(hit_labels)} obstacle footprints: {shown}{extra}")
    if not edge["route_pass"]:
        reasons.append("stored route edge failed width or level-change checks")
    return f"{html.escape(edge['label'])}<br>2D accessible route needs review: {'; '.join(reasons)}."


def _viewer_html(fig) -> str:
    plot = fig.to_html(include_plotlyjs=True, full_html=False)
    return f"""
<div style="font-family: Arial, sans-serif; color: #edf2f7; background: #0b0f17; padding: 14px; min-height: 800px;">
  <div style="border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 10px; background: #111827; line-height: 1.45;">
    This 2D plan uses Shapely obstacle footprints and a 0.90 m clearance strip along each right-angle route. Wall intersections at the route door opening are allowed. Green paths passed. Red paths need review.
  </div>
  {plot}
</div>
"""


def _label(element) -> str:
    return str(getattr(element, "LongName", None) or getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))



