from __future__ import annotations

import html
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell
import ifcopenshell.geom
from rdflib import Graph, Namespace, RDFS

from accessibility.model import Issue
from accessibility.route_path import orthogonal_route_points
from accessibility.route_path import path_segments

try:
    import plotly.graph_objects as go
except ModuleNotFoundError:
    go = None


ACC = Namespace("http://example.org/accessibility#")
PROPS = Namespace("http://lbd.arch.rwth-aachen.de/props#")

VIEWER_CLASSES = [
    "IfcWall",
    "IfcWallStandardCase",
    "IfcSlab",
    "IfcDoor",
    "IfcWindow",
    "IfcColumn",
    "IfcStair",
    "IfcStairFlight",
    "IfcRamp",
    "IfcRampFlight",
    "IfcRailing",
    "IfcCovering",
]


def make_interactive_model_viewer(uploaded_file, graph: Graph, issues: list[Issue]) -> tuple[str | None, dict[str, int | str]]:
    if go is None:
        return None, {"message": "Plotly is not installed."}

    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)

    issue_by_subject, issue_by_global_id = _issue_maps(graph, issues)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    normal_mesh = _empty_mesh()
    violation_mesh = _empty_mesh()
    violation_points = []
    elements_read = 0
    faces_read = 0
    max_faces = 160000

    for element in _viewer_elements(model):
        mesh = _mesh(settings, element)
        if mesh is None:
            continue
        element_faces = len(mesh["i"])
        if faces_read + element_faces > max_faces:
            break

        global_id = getattr(element, "GlobalId", "")
        issue_text = issue_by_global_id.get(global_id)
        target_mesh = violation_mesh if issue_text else normal_mesh
        _append_mesh(target_mesh, mesh)
        if issue_text:
            center = _center(mesh)
            violation_points.append(
                {
                    "x": center[0],
                    "y": center[1],
                    "z": center[2],
                    "label": _label(element),
                    "issue": issue_text,
                }
            )
        elements_read += 1
        faces_read += element_faces

    route_traces, route_stats = _route_edge_traces(graph, issue_by_subject)
    fig = go.Figure()
    _add_mesh_trace(fig, normal_mesh, "Model geometry", "rgba(170, 182, 196, 0.38)")
    _add_mesh_trace(fig, violation_mesh, "Violation geometry", "rgba(230, 50, 50, 0.78)")
    for trace in route_traces:
        fig.add_trace(trace)
    if violation_points:
        fig.add_trace(
            go.Scatter3d(
                x=[point["x"] for point in violation_points],
                y=[point["y"] for point in violation_points],
                z=[point["z"] for point in violation_points],
                mode="markers",
                marker={"size": 5, "color": "#ff1f1f"},
                text=[point["label"] for point in violation_points],
                customdata=[point["issue"] for point in violation_points],
                hovertemplate="<b>%{text}</b><br>%{customdata}<extra></extra>",
                name="Click violations",
            )
        )

    fig.update_layout(
        height=960,
        margin={"l": 0, "r": 0, "t": 20, "b": 0},
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
        font={"color": "#edf2f7"},
        scene={
            "xaxis": {"title": "X", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "yaxis": {"title": "Y", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "zaxis": {"title": "Z", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "aspectmode": "data",
            "camera": {
                "eye": {"x": 2.15, "y": -2.35, "z": 1.85},
                "center": {"x": 0, "y": 0, "z": -0.08},
                "up": {"x": 0, "y": 0, "z": 1},
            },
        },
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )

    stats = {
        "model_elements": elements_read,
        "model_faces": faces_read,
        "violation_points": len(violation_points),
        "route_edges": route_stats["route_edges"],
        "failed_route_edges": route_stats["failed_route_edges"],
    }
    return _viewer_html(fig), stats


def _issue_maps(graph: Graph, issues: list[Issue]) -> tuple[dict[str, str], dict[str, str]]:
    subject_by_local = {_local_name(subject): subject for subject in graph.subjects()}
    issue_by_subject: dict[str, str] = {}
    issue_by_global_id: dict[str, str] = {}
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        grouped.setdefault(issue.element_key, []).append(
            f"{issue.rule}: current value {issue.value}, required {issue.required}. {issue.explanation or issue.message}"
        )
    for local_key, issue_lines in grouped.items():
        subject = subject_by_local.get(local_key)
        text = "<br>".join(html.escape(line) for line in issue_lines[:6])
        issue_by_subject[local_key] = text
        if subject is None:
            continue
        global_id = graph.value(subject, PROPS.globalIdIfcRoot_attribute_simple)
        if global_id is not None:
            issue_by_global_id[str(global_id)] = text
    return issue_by_subject, issue_by_global_id


def _viewer_elements(model):
    seen = set()
    for class_name in VIEWER_CLASSES:
        try:
            elements = model.by_type(class_name)
        except RuntimeError:
            continue
        for element in elements:
            if element.id() in seen:
                continue
            seen.add(element.id())
            yield element


def _mesh(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    verts = list(shape.geometry.verts)
    faces = list(shape.geometry.faces)
    if len(verts) < 9 or len(faces) < 3:
        return None
    return {
        "x": verts[0::3],
        "y": verts[1::3],
        "z": verts[2::3],
        "i": faces[0::3],
        "j": faces[1::3],
        "k": faces[2::3],
    }


def _empty_mesh():
    return {"x": [], "y": [], "z": [], "i": [], "j": [], "k": []}


def _append_mesh(target, source) -> None:
    offset = len(target["x"])
    target["x"].extend(source["x"])
    target["y"].extend(source["y"])
    target["z"].extend(source["z"])
    target["i"].extend(index + offset for index in source["i"])
    target["j"].extend(index + offset for index in source["j"])
    target["k"].extend(index + offset for index in source["k"])


def _center(mesh) -> tuple[float, float, float]:
    return (
        (max(mesh["x"]) + min(mesh["x"])) / 2,
        (max(mesh["y"]) + min(mesh["y"])) / 2,
        (max(mesh["z"]) + min(mesh["z"])) / 2,
    )


def _add_mesh_trace(fig, mesh, name: str, color: str) -> None:
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
            opacity=1.0,
            name=name,
            hoverinfo="skip",
        )
    )


def _route_edge_traces(graph: Graph, issue_by_subject: dict[str, str]):
    traces = []
    pass_arrows = _empty_arrows()
    fail_arrows = _empty_arrows()
    route_edges = 0
    failed_route_edges = 0
    pass_legend_added = False
    fail_legend_added = False

    query = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?edge ?label ?pass ?doorWidth ?levelChange ?stepFree ?fx ?fy ?fz ?tx ?ty ?tz ?dx ?dy ?dz ?doorLabel
WHERE {
  ?edge a acc:RouteEdge ;
        rdfs:label ?label ;
        acc:routePass ?pass ;
        acc:fromSpace ?from ;
        acc:toSpace ?to ;
        acc:routeDoor ?door ;
        acc:routeDoorWidthM ?doorWidth ;
        acc:levelChangeM ?levelChange ;
        acc:stepFree ?stepFree .
  ?from acc:centerX ?fx ; acc:centerY ?fy ; acc:centerZ ?fz .
  ?to acc:centerX ?tx ; acc:centerY ?ty ; acc:centerZ ?tz .
  OPTIONAL { ?edge acc:doorCenterX ?dx ; acc:doorCenterY ?dy ; acc:doorCenterZ ?dz . }
  OPTIONAL { ?door rdfs:label ?doorLabel . }
}
"""
    for row in graph.query(query):
        route_edges += 1
        local = _local_name(row.edge)
        failed = str(row["pass"]) != "true"
        color = "#ff3333" if failed else "#2fbf71"
        route_name = "Failed route edges" if failed else "Passed route edges"
        if failed:
            failed_route_edges += 1
        issue = _route_issue_text(row, issue_by_subject.get(local))
        text = f"<b>{html.escape(str(row.label))}</b><br>{issue}"

        fx, fy, fz = float(row.fx), float(row.fy), float(row.fz)
        tx, ty, tz = float(row.tx), float(row.ty), float(row.tz)
        if row.dx is None or row.dy is None or row.dz is None:
            dx, dy, dz = (fx + tx) / 2, (fy + ty) / 2, (fz + tz) / 2
        else:
            dx, dy, dz = float(row.dx), float(row.dy), float(row.dz)

        # Lift the path slightly above the floor so it remains visible inside the model.
        route_z = max(fz, tz, dz) + 0.12
        points = orthogonal_route_points((fx, fy, route_z), (dx, dy, route_z), (tx, ty, route_z))
        x = [point[0] for point in points]
        y = [point[1] for point in points]
        z = [point[2] for point in points]
        showlegend = False
        if failed and not fail_legend_added:
            showlegend = True
            fail_legend_added = True
        if not failed and not pass_legend_added:
            showlegend = True
            pass_legend_added = True
        traces.append(_line_trace(x, y, z, [text for _point in points], color, route_name, failed, showlegend))
        arrow_target = fail_arrows if failed else pass_arrows
        for start, end in path_segments(points):
            _add_arrow(arrow_target, start[0], start[1], start[2], end[0], end[1], end[2], text)

    if pass_arrows["x"]:
        traces.append(_arrow_trace(pass_arrows, "#2fbf71", "Passed route direction"))
    if fail_arrows["x"]:
        traces.append(_arrow_trace(fail_arrows, "#ff3333", "Failed route direction"))
    return traces, {"route_edges": route_edges, "failed_route_edges": failed_route_edges}


def _route_issue_text(row, issue_text: str | None) -> str:
    door = html.escape(str(row.doorLabel or "route door"))
    width = float(row.doorWidth)
    level = float(row.levelChange)
    step_free = str(row.stepFree)
    if str(row["pass"]) == "true":
        return (
            f"Status: passed.<br>"
            f"Door: {door}.<br>"
            f"Door width: {width:.3f} m. Level change: {level:.3f} m. Step-free: {step_free}."
        )

    reasons = []
    if width < 0:
        reasons.append("door width is missing")
    elif width < 0.90:
        reasons.append(f"door width is {width:.3f} m, but it should be at least 0.90 m")
    if step_free != "true":
        reasons.append(f"level change is {level:.3f} m, so the route is not step-free")
    if not reasons and issue_text:
        reasons.append(issue_text)
    if not reasons:
        reasons.append("this route failed one of the stored route checks")
    return (
        f"Status: failed.<br>"
        f"Door: {door}.<br>"
        f"Reason: {'; '.join(html.escape(reason) for reason in reasons)}.<br>"
        f"Fix: widen the door, remove the level change, or add a compliant ramp/lift route."
    )


def _empty_arrows():
    return {"x": [], "y": [], "z": [], "u": [], "v": [], "w": [], "text": []}


def _add_arrow(target, start_x, start_y, start_z, end_x, end_y, end_z, text: str) -> None:
    vx = end_x - start_x
    vy = end_y - start_y
    vz = end_z - start_z
    length = math.sqrt(vx * vx + vy * vy + vz * vz)
    if length <= 0.001:
        return
    scale = min(max(length * 0.18, 0.25), 0.75)
    target["x"].append(start_x + vx * 0.72)
    target["y"].append(start_y + vy * 0.72)
    target["z"].append(start_z + vz * 0.72)
    target["u"].append((vx / length) * scale)
    target["v"].append((vy / length) * scale)
    target["w"].append((vz / length) * scale)
    target["text"].append(text)


def _arrow_trace(arrows, color: str, name: str):
    return go.Cone(
        x=arrows["x"],
        y=arrows["y"],
        z=arrows["z"],
        u=arrows["u"],
        v=arrows["v"],
        w=arrows["w"],
        anchor="tip",
        sizemode="absolute",
        sizeref=0.45,
        colorscale=[[0, color], [1, color]],
        showscale=False,
        text=arrows["text"],
        customdata=arrows["text"],
        hovertemplate="%{text}<extra></extra>",
        name=name,
    )


def _line_trace(x, y, z, text, color: str, name: str, failed: bool, showlegend: bool):
    return go.Scatter3d(
        x=x,
        y=y,
        z=z,
        mode="lines",
        line={"color": color, "width": 7},
        text=text,
        customdata=text,
        hovertemplate="%{text}<extra></extra>",
        name=name,
        legendgroup="failed-routes" if failed else "passed-routes",
        showlegend=showlegend,
    )


def _viewer_html(fig) -> str:
    plot = fig.to_html(include_plotlyjs=True, full_html=False, div_id="ifc-model-viewer")
    return f"""
<div style="font-family: Arial, sans-serif; color: #edf2f7; background: #0b0f17; padding: 14px; min-height: 1020px;">
  <div id="issue-panel" style="border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 10px; background: #111827; min-height: 72px; line-height: 1.45;">
    Click a violation marker, route line, or route arrow to read the check result here.
  </div>
  {plot}
</div>
<script>
const plot = document.getElementById('ifc-model-viewer');
const panel = document.getElementById('issue-panel');
let selectedRoute = null;
const routeBaseStyles = {{}};
plot.data.forEach(function(trace, index) {{
  if (trace.type === 'scatter3d' && trace.mode === 'lines' && trace.line) {{
    routeBaseStyles[index] = {{
      color: trace.line.color || '#2fbf71',
      width: trace.line.width || 7
    }};
  }}
}});

plot.on('plotly_click', function(data) {{
  const point = data.points && data.points[0];
  if (!point) return;
  const text = point.customdata || point.text || 'No issue text is attached to this object.';
  panel.innerHTML = text;

  const trace = plot.data[point.curveNumber];
  if (!trace || trace.type !== 'scatter3d' || trace.mode !== 'lines') return;

  if (selectedRoute !== null && plot.data[selectedRoute]) {{
    const selectedStyle = routeBaseStyles[selectedRoute];
    if (selectedStyle) {{
      Plotly.restyle(plot, {{'line.width': [selectedStyle.width], 'line.color': [selectedStyle.color]}}, [selectedRoute]);
    }}
  }}
  Plotly.restyle(plot, {{'line.width': [13], 'line.color': ['#ffd166']}}, [point.curveNumber]);
  selectedRoute = point.curveNumber;
}});
</script>
"""


def _label(element) -> str:
    return str(getattr(element, "Name", None) or getattr(element, "GlobalId", "IFC element"))


def _local_name(uri) -> str:
    text = str(uri)
    if "#" in text:
        return text.rsplit("#", 1)[1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[1]
    return text
