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


ACC = Namespace("http://example.org/accessibility#")

MIN_CLEAR_WIDTH_M = 0.90
MIN_CLEAR_HEIGHT_M = 2.05
CLEAR_LENGTH_M = 1.20
MAX_OBSTACLE_BOXES = 650
MAX_ROUTE_BOXES = 1200
MAX_ANIMATION_STEPS = 1200
ANIMATION_STEP_M = 1.60

OBSTACLE_CLASSES = [
    "IfcWall",
    "IfcWallStandardCase",
    "IfcColumn",
    "IfcStair",
    "IfcStairFlight",
    "IfcRamp",
    "IfcRampFlight",
    "IfcRailing",
    "IfcFurnishingElement",
    "IfcBuildingElementProxy",
]


def make_3d_clearance_viewer(uploaded_file, graph: Graph) -> tuple[str | None, dict[str, int | str], list[GeometryFinding]]:
    if go is None:
        return None, {"message": "Plotly is not installed."}, [
            _finding("Model data", "IFC model", "3D clearance", "not checked", "Plotly is not installed.", "Install Plotly.")
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
    findings: list[GeometryFinding] = []

    if not route_edges:
        findings.append(
            _finding(
                "Model data",
                "IFC model",
                "3D clearance",
                "not checked",
                "No route edges were available in the enriched RDF graph.",
                "Export IfcSpace, IfcDoor, and IfcRelSpaceBoundary data so route edges can be built.",
            )
        )
    if not obstacle_boxes:
        findings.append(
            _finding(
                "Model data",
                "IFC model",
                "3D clearance",
                "limited",
                "No wall, column, stair, railing, furnishing, or proxy obstacle meshes were read.",
                "Export obstacle geometry as IFC building elements.",
            )
        )

    fig = go.Figure()
    failed_boxes = 0
    collisions_total = 0

    obstacle_mesh = _empty_box_mesh()
    for box in obstacle_boxes[:MAX_OBSTACLE_BOXES]:
        _append_box_mesh(obstacle_mesh, box["bounds"])
    _add_box_mesh_trace(fig, obstacle_mesh, "Obstacle geometry", "rgba(145, 160, 180, 0.12)", "Checked obstacle bounding boxes")


    route_segments = _right_angle_segments(route_edges, obstacle_boxes)

    checked_segments = []
    for index, segment in enumerate(route_segments[:MAX_ROUTE_BOXES]):
        envelope = segment_envelope(segment["start"], segment["end"], MIN_CLEAR_WIDTH_M, MIN_CLEAR_HEIGHT_M)
        collisions = _colliding_obstacles(envelope, obstacle_boxes)
        collision_names = [item["label"] for item in collisions[:6]]
        passed = not collisions and segment["route_pass"]
        checked_segments.append({**segment, "passed": passed, "collision_names": collision_names, "summary": _route_summary(segment, passed, collision_names)})
        if passed:
            color = "rgba(47, 191, 113, 0.34)"
            name = "Passed 3D clearance envelope"
        else:
            color = "rgba(255, 51, 51, 0.42)"
            name = "Failed 3D clearance envelope"
            failed_boxes += 1
            collisions_total += len(collisions)
            findings.append(
                _finding(
                    "Mobility",
                    segment["label"],
                    "3D clearance",
                    "failed",
                    _failure_reason(segment, collision_names),
                    "Keep the route width and height clear, remove obstacles, or change the accessible route.",
                )
            )
        _add_box(fig, envelope, name, color, _route_text(segment, collision_names), showlegend=(index == 0 or (not passed and failed_boxes == 1)))

    animated_steps = _add_clearance_animation(fig, checked_segments)

    if route_segments and failed_boxes == 0:
        findings.append(
            _finding(
                "Mobility",
                "IFC model",
                "3D clearance",
                "passed",
                "The clearance envelopes did not intersect the checked obstacle boxes.",
                "Review the viewer result and continue with detailed model checks if needed.",
            )
        )

    fig.update_layout(
        height=900,
        margin={"l": 0, "r": 0, "t": 20, "b": 0},
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
        font={"color": "#edf2f7"},
        scene={
            "xaxis": {"title": "X", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "yaxis": {"title": "Y", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "zaxis": {"title": "Z", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "aspectmode": "data",
            "camera": {"eye": {"x": 2.2, "y": -2.45, "z": 1.8}, "up": {"x": 0, "y": 0, "z": 1}},
            "uirevision": "keep-view",
        },
        uirevision="keep-view",
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )

    stats = {
        "route_edges": len(route_edges),
        "route_segments": len(route_segments),
        "checked_route_segments": min(len(route_segments), MAX_ROUTE_BOXES),
        "obstacle_boxes": len(obstacle_boxes),
        "shown_obstacle_boxes": min(len(obstacle_boxes), MAX_OBSTACLE_BOXES),
        "failed_clearance_segments": failed_boxes,
        "obstacle_intersections": collisions_total,
        "clearance_width_m": MIN_CLEAR_WIDTH_M,
        "clearance_length_m": CLEAR_LENGTH_M,
        "clearance_height_m": MIN_CLEAR_HEIGHT_M,
        "animated_route_steps": animated_steps,
    }
    return _viewer_html(fig), stats, findings


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
        route_pass = str(row["pass"]).lower() == "true"
        route_edges.append({"label": str(row.label), "start": (fx, fy, fz), "door": (dx, dy, dz), "end": (tx, ty, tz), "route_pass": route_pass})
    return route_edges


def _right_angle_segments(route_edges: list[dict[str, object]], obstacle_boxes: list[dict[str, object]]) -> list[dict[str, object]]:
    segments = []
    for edge in route_edges:
        def score_segment(start, end):
            envelope = segment_envelope(start, end, MIN_CLEAR_WIDTH_M, MIN_CLEAR_HEIGHT_M)
            return len(_colliding_obstacles(envelope, obstacle_boxes))

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
            if bounds is None or _is_too_flat(bounds):
                continue
            boxes.append({"label": _label(element), "bounds": bounds})
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


def _colliding_obstacles(envelope, obstacle_boxes) -> list[dict[str, object]]:
    return [box for box in obstacle_boxes if boxes_intersect(envelope, box["bounds"])]


def _is_too_flat(bounds) -> bool:
    x0, x1, y0, y1, z0, z1 = bounds
    return (x1 - x0) < 0.03 or (y1 - y0) < 0.03 or (z1 - z0) < 0.03


def _add_box(fig, bounds, name: str, color: str, text: str, showlegend: bool) -> None:
    mesh = _empty_box_mesh()
    _append_box_mesh(mesh, bounds)
    _add_box_mesh_trace(fig, mesh, name, color, text, showlegend)


def _empty_box_mesh() -> dict[str, list]:
    return {"x": [], "y": [], "z": [], "i": [], "j": [], "k": []}


def _append_box_mesh(mesh: dict[str, list], bounds) -> None:
    x0, x1, y0, y1, z0, z1 = bounds
    offset = len(mesh["x"])
    vertices = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    mesh["x"].extend(item[0] for item in vertices)
    mesh["y"].extend(item[1] for item in vertices)
    mesh["z"].extend(item[2] for item in vertices)
    mesh["i"].extend(item[0] + offset for item in faces)
    mesh["j"].extend(item[1] + offset for item in faces)
    mesh["k"].extend(item[2] + offset for item in faces)


def _add_box_mesh_trace(fig, mesh: dict[str, list], name: str, color: str, text: str, showlegend: bool = True) -> None:
    if not mesh["x"]:
        return
    fig.add_trace(
        go.Mesh3d(
            x=mesh["x"],
            y=mesh["y"],
            z=mesh["z"],
            i=mesh["i"],
            j=mesh["j"],
            k=mesh["k"],
            color=color,
            flatshading=True,
            hovertemplate=html.escape(text) + "<extra></extra>",
            name=name,
            showlegend=showlegend,
        )
    )


def _add_route_line(fig, segment: dict[str, object], color: str) -> None:
    start = segment["start"]
    end = segment["end"]
    z = max(start[2], end[2]) + 0.08
    fig.add_trace(
        go.Scatter3d(
            x=[start[0], end[0]],
            y=[start[1], end[1]],
            z=[z, z],
            mode="lines",
            line={"color": color, "width": 6},
            hovertemplate=html.escape(str(segment["label"])) + "<extra></extra>",
            name="3D route centerline",
            showlegend=False,
        )
    )


def _add_clearance_animation(fig, segments: list[dict[str, object]]) -> int:
    samples = _motion_samples(segments)
    if not samples:
        return 0

    first = samples[0]
    mesh = _mesh_from_bounds(_moving_clearance_bounds(first["point"], first["segment"]))
    box_index = len(fig.data)
    fig.add_trace(
        go.Mesh3d(
            x=mesh["x"],
            y=mesh["y"],
            z=mesh["z"],
            i=mesh["i"],
            j=mesh["j"],
            k=mesh["k"],
            color=_motion_color(first["passed"], 0.62),
            flatshading=True,
            name="Moving wheelchair clearance volume",
            hovertemplate=_motion_text(first) + "<extra></extra>",
        )
    )

    passed_progress_index = len(fig.data)
    fig.add_trace(
        go.Scatter3d(
            x=[first["point"][0]],
            y=[first["point"][1]],
            z=[first["point"][2] + 0.18],
            mode="lines+markers",
            line={"color": "#ffd166", "width": 6},
            marker={"size": 5, "color": "#ffd166"},
            name="Mapped route so far",
            hovertemplate="Yellow path already checked<extra></extra>",
        )
    )
    failed_progress_index = len(fig.data)
    fig.add_trace(
        go.Scatter3d(
            x=[],
            y=[],
            z=[],
            mode="lines+markers",
            line={"color": "#ff3333", "width": 8},
            marker={"size": 6, "color": "#ff3333"},
            name="Failed mapped route",
            hovertemplate="Red path failed the clearance check<extra></extra>",
        )
    )

    passed_x: list[float | None] = []
    passed_y: list[float | None] = []
    passed_z: list[float | None] = []
    failed_x: list[float | None] = []
    failed_y: list[float | None] = []
    failed_z: list[float | None] = []
    frames = []
    for index, sample in enumerate(samples):
        point = sample["point"]
        if sample["passed"]:
            passed_x.append(point[0])
            passed_y.append(point[1])
            passed_z.append(point[2] + 0.18)
            failed_x.append(None)
            failed_y.append(None)
            failed_z.append(None)
        else:
            passed_x.append(None)
            passed_y.append(None)
            passed_z.append(None)
            failed_x.append(point[0])
            failed_y.append(point[1])
            failed_z.append(point[2] + 0.18)
        frame_mesh = _mesh_from_bounds(_moving_clearance_bounds(point, sample["segment"]))
        frames.append(
            go.Frame(
                name=str(index),
                traces=[box_index, passed_progress_index, failed_progress_index],
                data=[
                    go.Mesh3d(
                        x=frame_mesh["x"],
                        y=frame_mesh["y"],
                        z=frame_mesh["z"],
                        i=frame_mesh["i"],
                        j=frame_mesh["j"],
                        k=frame_mesh["k"],
                        color=_motion_color(sample["passed"], 0.62),
                        hovertemplate=_motion_text(sample) + "<extra></extra>",
                    ),
                    go.Scatter3d(x=list(passed_x), y=list(passed_y), z=list(passed_z)),
                    go.Scatter3d(x=list(failed_x), y=list(failed_y), z=list(failed_z), hovertemplate=_motion_text(sample) + "<extra></extra>"),
                ],
            )
        )

    _set_animation(fig, frames)
    return len(samples)


def _motion_samples(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    if segments and len(segments) + 1 <= MAX_ANIMATION_STEPS:
        samples = []
        first = segments[0]
        samples.append(
            {
                "point": (first["start"][0], first["start"][1], max(first["start"][2], first["end"][2])),
                "segment": first,
                "passed": bool(first.get("passed")),
                "label": str(first.get("label", "Route segment")),
                "collision_names": first.get("collision_names", []),
                "summary": str(first.get("summary", "")),
            }
        )
        for segment in segments:
            end = segment["end"]
            samples.append(
                {
                    "point": (end[0], end[1], max(segment["start"][2], end[2])),
                    "segment": segment,
                    "passed": bool(segment.get("passed")),
                    "label": str(segment.get("label", "Route segment")),
                    "collision_names": segment.get("collision_names", []),
                    "summary": str(segment.get("summary", "")),
                }
            )
        return samples

    samples = []
    for segment in segments:
        start = segment["start"]
        end = segment["end"]
        length = math.dist(start, end)
        steps = max(1, math.ceil(length / ANIMATION_STEP_M))
        for step in range(steps + 1):
            t = step / steps
            point = (
                start[0] + (end[0] - start[0]) * t,
                start[1] + (end[1] - start[1]) * t,
                max(start[2], end[2]),
            )
            samples.append(
                {
                    "point": point,
                    "segment": segment,
                    "passed": bool(segment.get("passed")),
                    "label": str(segment.get("label", "Route segment")),
                    "collision_names": segment.get("collision_names", []),
                    "summary": str(segment.get("summary", "")),
                }
            )
            if len(samples) >= MAX_ANIMATION_STEPS:
                return samples
    return samples


def _moving_clearance_bounds(point, segment: dict[str, object]):
    x, y, z = point
    start = segment["start"]
    end = segment["end"]
    along_x = abs(end[0] - start[0]) >= abs(end[1] - start[1])
    if along_x:
        half_x = CLEAR_LENGTH_M / 2
        half_y = MIN_CLEAR_WIDTH_M / 2
    else:
        half_x = MIN_CLEAR_WIDTH_M / 2
        half_y = CLEAR_LENGTH_M / 2
    return x - half_x, x + half_x, y - half_y, y + half_y, z, z + MIN_CLEAR_HEIGHT_M


def _mesh_from_bounds(bounds) -> dict[str, list]:
    mesh = _empty_box_mesh()
    _append_box_mesh(mesh, bounds)
    return mesh


def _motion_color(passed: bool, alpha: float) -> str:
    return f"rgba(47, 191, 113, {alpha})" if passed else f"rgba(255, 51, 51, {alpha})"


def _motion_text(sample: dict[str, object]) -> str:
    summary = sample.get("summary") or _route_summary(sample.get("segment", {}), bool(sample.get("passed")), sample.get("collision_names") or [])
    return f"{html.escape(str(sample['label']))}<br>{html.escape(str(summary))}"


def _route_summary(segment: dict[str, object], passed: bool, collisions: list[str]) -> str:
    if passed:
        return "Wheelchair clearance passed here."
    if collisions:
        shown = ", ".join(collisions[:3])
        return f"Wheelchair clearance hits obstacle geometry: {shown}."
    if not segment.get("route_pass"):
        return "This route fails because a door width or level change is not acceptable."
    return "Wheelchair clearance needs review here."


def _set_animation(fig, frames) -> None:
    if not frames:
        return
    fig.frames = frames
    steps = [
        {
            "args": [[frame.name], {"frame": {"duration": 0, "redraw": True}, "mode": "immediate", "transition": {"duration": 0}}],
            "label": str(index),
            "method": "animate",
        }
        for index, frame in enumerate(frames[:: max(1, len(frames) // 12)])
    ]
    fig.update_layout(
        updatemenus=[
            {
                "type": "buttons",
                "showactive": False,
                "x": 0.02,
                "y": 1.12,
                "buttons": [
                    {
                        "label": "Play clearance mapping",
                        "method": "animate",
                        "args": [None, {"frame": {"duration": 180, "redraw": True}, "fromcurrent": True, "transition": {"duration": 0}}],
                    },
                    {
                        "label": "Pause",
                        "method": "animate",
                        "args": [[None], {"frame": {"duration": 0, "redraw": False}, "mode": "immediate", "transition": {"duration": 0}}],
                    },
                ],
            }
        ],
        sliders=[
            {
                "active": 0,
                "x": 0.02,
                "y": 1.06,
                "len": 0.74,
                "currentvalue": {"prefix": "Clearance step "},
                "steps": steps,
            }
        ],
    )


def _route_text(segment: dict[str, object], collisions: list[str]) -> str:
    if not collisions and segment["route_pass"]:
        return f"{segment['label']}<br>3D clearance envelope passed."
    collision_text = ", ".join(collisions) if collisions else "no obstacle intersection found"
    return (
        f"{segment['label']}<br>"
        f"3D clearance envelope failed or route edge failed.<br>"
        f"Obstacle intersections: {html.escape(collision_text)}"
    )


def _failure_reason(segment: dict[str, object], collisions: list[str]) -> str:
    reasons = []
    if not segment["route_pass"]:
        reasons.append("The stored route edge already failed the door width or level-change rule.")
    if collisions:
        reasons.append("The 3D clearance envelope intersects " + ", ".join(collisions[:4]) + ".")
    if not reasons:
        reasons.append("The route segment failed the 3D clearance check.")
    return " ".join(reasons)


def _viewer_html(fig) -> str:
    plot = fig.to_html(include_plotlyjs=True, full_html=False, post_script=_loop_animation_script())
    return f"""
<div style="font-family: Arial, sans-serif; color: #edf2f7; background: #0b0f17; padding: 14px; min-height: 920px;">
  <div style="border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 10px; background: #111827; line-height: 1.45;">
    This model shows the moving 3D clearance volume. Press Play clearance mapping to watch the wheelchair-sized volume map the route. Yellow shows the route already checked. Red appears where the checked route fails. The animation loops until Pause is pressed.
    The check uses IfcOpenShell mesh bounding boxes and does not invent missing building dimensions.
  </div>
  {plot}
</div>
"""


def _loop_animation_script() -> str:
    return """
const gd = document.getElementById('{plot_id}');
if (gd && !gd.dataset.loopReady) {
  gd.dataset.loopReady = 'true';
  gd.dataset.loopAnimation = 'false';
  gd.dataset.camera = '';
  gd.on('plotly_relayout', (eventData) => {
    if (eventData['scene.camera']) {
      gd.dataset.camera = JSON.stringify(eventData['scene.camera']);
    }
  });
  const keepCamera = () => {
    if (gd.dataset.camera) {
      Plotly.relayout(gd, {'scene.camera': JSON.parse(gd.dataset.camera)});
    }
  };
  const replay = () => {
    keepCamera();
    if (gd.dataset.loopAnimation === 'true') {
      Plotly.animate(gd, null, {
        frame: {duration: 180, redraw: true},
        transition: {duration: 0},
        fromcurrent: false,
        mode: 'immediate'
      });
    }
  };
  gd.addEventListener('click', (event) => {
    const text = (event.target && event.target.textContent || '').trim().toLowerCase();
    if (text.includes('play')) {
      gd.dataset.loopAnimation = 'true';
    }
    if (text.includes('pause')) {
      gd.dataset.loopAnimation = 'false';
    }
  }, true);
  gd.on('plotly_animated', replay);
}
"""


def _label(element) -> str:
    return str(getattr(element, "LongName", None) or getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))


def _finding(category: str, element: str, check: str, result: str, reason: str, fix: str) -> GeometryFinding:
    return GeometryFinding(category=category, element=element, check=check, result=result, reason=reason, fix=fix)



