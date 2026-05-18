from __future__ import annotations

import html
from dataclasses import dataclass
from hashlib import sha1

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef
from rdflib.namespace import XSD

try:
    import plotly.graph_objects as go
except ModuleNotFoundError:
    go = None


ACC = Namespace("http://example.org/accessibility#")
CHANGE = Namespace("http://example.org/accessibility-change#")

MIN_DOOR_WIDTH_M = 0.90
DEFAULT_STOREY_HEIGHT_M = 3.0


@dataclass
class ChangeOption:
    key: str
    label: str
    route_edge: str
    route_edge_node: str
    door_label: str
    door_node: str
    from_space: str
    from_space_node: str
    to_space: str
    to_space_node: str
    current_door_width_m: float
    target_door_width_m: float
    width_increase_m: float
    strategy: str
    old_footprint_m2: float
    new_footprint_m2: float
    footprint_change_m2: float
    footprint_change_percent: float
    old_volume_m3: float
    new_volume_m3: float
    volume_change_m3: float
    affected_space: str
    affected_space_area_before_m2: float
    affected_space_area_after_m2: float
    affected_space_area_change_m2: float
    affected_space_area_change_percent: float
    plot_limit_m2: float
    fits_plot: bool
    explanation: str


def failed_door_options(route_edge_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    options = []
    for row in route_edge_rows:
        width = _float(row.get("Door width m"))
        if width is None or width >= MIN_DOOR_WIDTH_M:
            continue
        options.append(
            {
                "label": f"{row.get('Door', 'Door')} | {row.get('From space', '')} -> {row.get('To space', '')}",
                "route_edge": row.get("Route edge", ""),
                "route_edge_node": row.get("Route edge node", ""),
                "door_label": row.get("Door", "Door"),
                "door_node": row.get("Door node", ""),
                "from_space": row.get("From space", ""),
                "from_space_node": row.get("From space node", ""),
                "to_space": row.get("To space", ""),
                "to_space_node": row.get("To space node", ""),
                "current_width": f"{width:.3f}",
            }
        )
    return options


def calculate_change_option(
    graph: Graph,
    route_edge_rows: list[dict[str, str]],
    selected_label: str,
    target_width_m: float,
    strategy: str,
    plot_limit_m2: float,
    storey_height_m: float = DEFAULT_STOREY_HEIGHT_M,
) -> ChangeOption | None:
    selected = None
    for row in route_edge_rows:
        label = f"{row.get('Door', 'Door')} | {row.get('From space', '')} -> {row.get('To space', '')}"
        if label == selected_label:
            selected = row
            break
    if selected is None:
        return None

    current_width = _float(selected.get("Door width m")) or 0.0
    target_width = max(target_width_m, current_width)
    width_increase = max(target_width - current_width, 0.0)
    old_footprint = _building_footprint(graph)
    route_length = _route_length(graph, selected.get("Route edge node", ""), selected.get("Route edge", ""))
    impacted_area = width_increase * route_length
    affected_space = selected.get("To space") or selected.get("From space") or "connected space"
    affected_area = _space_area(graph, selected.get("To space node", ""), affected_space)

    if strategy == "expand building outward":
        footprint_change = impacted_area
        new_footprint = old_footprint + footprint_change
        area_after = affected_area
        area_change = 0.0
    else:
        footprint_change = 0.0
        new_footprint = old_footprint
        area_change = -min(impacted_area, affected_area)
        area_after = max(affected_area + area_change, 0.0)

    volume_before = old_footprint * storey_height_m
    volume_after = new_footprint * storey_height_m
    fits_plot = new_footprint <= plot_limit_m2 if plot_limit_m2 > 0 else True
    footprint_percent = _percent(footprint_change, old_footprint)
    affected_percent = _percent(area_change, affected_area)
    key = _change_key(selected_label, target_width, strategy)

    if strategy == "expand building outward":
        explanation = (
            f"{selected.get('Door')} is widened from {current_width:.3f} m to {target_width:.3f} m. "
            f"The estimated route zone adds {footprint_change:.2f} m2 to the building footprint, "
            f"which is {footprint_percent:.2f} percent of the current footprint."
        )
        if not fits_plot:
            explanation += " The entered plot limit is too small for this outward expansion."
    else:
        explanation = (
            f"{selected.get('Door')} is widened from {current_width:.3f} m to {target_width:.3f} m while the outer footprint stays fixed. "
            f"The estimated extra route zone is taken from {affected_space}, reducing it by {abs(area_change):.2f} m2 "
            f"or {abs(affected_percent):.2f} percent."
        )

    return ChangeOption(
        key=key,
        label=f"Change option {key}",
        route_edge=selected.get("Route edge", ""),
        route_edge_node=selected.get("Route edge node", ""),
        door_label=selected.get("Door", "Door"),
        door_node=selected.get("Door node", ""),
        from_space=selected.get("From space", ""),
        from_space_node=selected.get("From space node", ""),
        to_space=selected.get("To space", ""),
        to_space_node=selected.get("To space node", ""),
        current_door_width_m=round(current_width, 4),
        target_door_width_m=round(target_width, 4),
        width_increase_m=round(width_increase, 4),
        strategy=strategy,
        old_footprint_m2=round(old_footprint, 3),
        new_footprint_m2=round(new_footprint, 3),
        footprint_change_m2=round(footprint_change, 3),
        footprint_change_percent=round(footprint_percent, 3),
        old_volume_m3=round(volume_before, 3),
        new_volume_m3=round(volume_after, 3),
        volume_change_m3=round(volume_after - volume_before, 3),
        affected_space=affected_space,
        affected_space_area_before_m2=round(affected_area, 3),
        affected_space_area_after_m2=round(area_after, 3),
        affected_space_area_change_m2=round(area_change, 3),
        affected_space_area_change_percent=round(affected_percent, 3),
        plot_limit_m2=round(plot_limit_m2, 3),
        fits_plot=fits_plot,
        explanation=explanation,
    )


def add_change_option_to_graph(graph: Graph, option: ChangeOption) -> None:
    subject = CHANGE[option.key]
    graph.add((subject, RDF.type, ACC.ChangeOption))
    graph.add((subject, RDFS.label, Literal(option.label)))
    for predicate, value in [
        (ACC.affectsRouteEdge, option.route_edge_node),
        (ACC.affectsDoor, option.door_node),
        (ACC.affectsFromSpace, option.from_space_node),
        (ACC.affectsToSpace, option.to_space_node),
    ]:
        if value:
            target = URIRef(value)
            graph.add((subject, predicate, target))
            graph.add((target, ACC.hasChangeOption, subject))
    graph.add((subject, ACC.strategy, Literal(option.strategy)))
    graph.add((subject, ACC.affectedDoorLabel, Literal(option.door_label)))
    graph.add((subject, ACC.fromSpaceLabel, Literal(option.from_space)))
    graph.add((subject, ACC.toSpaceLabel, Literal(option.to_space)))
    graph.add((subject, ACC.currentDoorWidthM, Literal(option.current_door_width_m, datatype=XSD.double)))
    graph.add((subject, ACC.targetDoorWidthM, Literal(option.target_door_width_m, datatype=XSD.double)))
    graph.add((subject, ACC.widthIncreaseM, Literal(option.width_increase_m, datatype=XSD.double)))
    graph.add((subject, ACC.oldFootprintM2, Literal(option.old_footprint_m2, datatype=XSD.double)))
    graph.add((subject, ACC.newFootprintM2, Literal(option.new_footprint_m2, datatype=XSD.double)))
    graph.add((subject, ACC.footprintChangeM2, Literal(option.footprint_change_m2, datatype=XSD.double)))
    graph.add((subject, ACC.footprintChangePercent, Literal(option.footprint_change_percent, datatype=XSD.double)))
    graph.add((subject, ACC.oldVolumeM3, Literal(option.old_volume_m3, datatype=XSD.double)))
    graph.add((subject, ACC.newVolumeM3, Literal(option.new_volume_m3, datatype=XSD.double)))
    graph.add((subject, ACC.volumeChangeM3, Literal(option.volume_change_m3, datatype=XSD.double)))
    graph.add((subject, ACC.affectedSpaceLabel, Literal(option.affected_space)))
    graph.add((subject, ACC.affectedSpaceAreaBeforeM2, Literal(option.affected_space_area_before_m2, datatype=XSD.double)))
    graph.add((subject, ACC.affectedSpaceAreaAfterM2, Literal(option.affected_space_area_after_m2, datatype=XSD.double)))
    graph.add((subject, ACC.affectedSpaceAreaChangeM2, Literal(option.affected_space_area_change_m2, datatype=XSD.double)))
    graph.add((subject, ACC.affectedSpaceAreaChangePercent, Literal(option.affected_space_area_change_percent, datatype=XSD.double)))
    graph.add((subject, ACC.plotLimitM2, Literal(option.plot_limit_m2, datatype=XSD.double)))
    graph.add((subject, ACC.fitsPlot, Literal(option.fits_plot, datatype=XSD.boolean)))
    graph.add((subject, ACC.changeExplanation, Literal(option.explanation)))


def option_rows(option: ChangeOption) -> list[dict[str, float | str | bool]]:
    return [
        {"Item": "Door clear width", "Before": option.current_door_width_m, "After": option.target_door_width_m, "Change": option.width_increase_m},
        {"Item": "Building footprint m2", "Before": option.old_footprint_m2, "After": option.new_footprint_m2, "Change": option.footprint_change_m2},
        {"Item": "Building footprint percent", "Before": 0.0, "After": option.footprint_change_percent, "Change": option.footprint_change_percent},
        {"Item": "Building volume m3", "Before": option.old_volume_m3, "After": option.new_volume_m3, "Change": option.volume_change_m3},
        {"Item": f"{option.affected_space} area m2", "Before": option.affected_space_area_before_m2, "After": option.affected_space_area_after_m2, "Change": option.affected_space_area_change_m2},
        {"Item": "Fits plot", "Before": "", "After": option.fits_plot, "Change": ""},
    ]


def change_context(option: ChangeOption | None) -> str:
    if option is None:
        return ""
    lines = [
        "Changes Impact simulation:",
        f"Door: {option.door_label}.",
        f"Route: {option.from_space} to {option.to_space}.",
        f"Strategy: {option.strategy}.",
        f"Door width changes from {option.current_door_width_m} m to {option.target_door_width_m} m.",
        f"Building footprint changes from {option.old_footprint_m2} m2 to {option.new_footprint_m2} m2.",
        f"Footprint change is {option.footprint_change_m2} m2 or {option.footprint_change_percent} percent.",
        f"Affected space: {option.affected_space}. Area changes from {option.affected_space_area_before_m2} m2 to {option.affected_space_area_after_m2} m2.",
        f"Fits entered plot limit: {option.fits_plot}.",
        option.explanation,
    ]
    return "\n".join(lines)


def make_change_impact_viewer(option: ChangeOption | None) -> str | None:
    if go is None or option is None:
        return None

    fig = go.Figure()
    base_width = 5.0
    base_depth = 3.0
    increase = max(option.width_increase_m, 0.05)

    _add_box(fig, (0, base_width, 0, base_depth, 0, 3.0), "Original route zone", "rgba(148, 163, 184, 0.22)", "Original route/corridor zone")
    if option.strategy == "expand building outward":
        _add_box(fig, (base_width, base_width + increase * 10, 0, base_depth, 0, 3.0), "Added building volume", "rgba(47, 191, 113, 0.46)", option.explanation)
    else:
        _add_box(fig, (base_width, base_width + increase * 10, 0, base_depth, 0, 3.0), "Space taken from adjacent room", "rgba(255, 145, 77, 0.52)", option.explanation)

    fig.add_trace(
        go.Scatter3d(
            x=[base_width / 2, base_width + increase * 5],
            y=[base_depth / 2, base_depth / 2],
            z=[1.05, 1.05],
            mode="lines+markers",
            line={"color": "#ffd166", "width": 8},
            marker={"size": 5, "color": "#ffd166"},
            hovertemplate=html.escape(option.explanation) + "<extra></extra>",
            name="Door widening direction",
        )
    )
    fig.update_layout(
        height=560,
        margin={"l": 0, "r": 0, "t": 20, "b": 0},
        paper_bgcolor="#0b0f17",
        plot_bgcolor="#0b0f17",
        font={"color": "#edf2f7"},
        scene={
            "xaxis": {"title": "Width", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "yaxis": {"title": "Depth", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "zaxis": {"title": "Height", "backgroundcolor": "#0b0f17", "gridcolor": "#253142"},
            "aspectmode": "data",
        },
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )
    plot = fig.to_html(include_plotlyjs=True, full_html=False)
    return f"""
<div style="font-family: Arial, sans-serif; color: #edf2f7; background: #0b0f17; padding: 14px; min-height: 620px;">
  <div style="border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 10px; background: #111827; line-height: 1.45;">
    This viewer is an impact overlay, not an edited IFC model. Grey is the original route zone. Green means outward growth. Orange means space taken from a connected room or zone.
  </div>
  {plot}
</div>
"""


def _building_footprint(graph: Graph) -> float:
    total = 0.0
    for value in graph.objects(None, ACC.footprintAreaM2):
        try:
            area = float(value)
        except (TypeError, ValueError):
            continue
        if area > 0:
            total += area
    return total or 1.0


def _space_area(graph: Graph, node_uri: str, label: str) -> float:
    if node_uri:
        value = graph.value(URIRef(node_uri), ACC.footprintAreaM2)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    query = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?area
WHERE {
  ?space rdfs:label ?label ;
         acc:footprintAreaM2 ?area .
}
"""
    for row in graph.query(query, initBindings={"label": Literal(label)}):
        try:
            return float(row.area)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _route_length(graph: Graph, route_edge_node: str, route_edge_label: str) -> float:
    if route_edge_node:
        edge = URIRef(route_edge_node)
        from_space = graph.value(edge, ACC.fromSpace)
        to_space = graph.value(edge, ACC.toSpace)
        if from_space is not None and to_space is not None:
            start = _xy(graph, from_space)
            end = _xy(graph, to_space)
            if start is not None and end is not None:
                dx = start[0] - end[0]
                dy = start[1] - end[1]
                return max((dx * dx + dy * dy) ** 0.5, 1.0)
    query = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?fx ?fy ?tx ?ty
WHERE {
  ?edge a acc:RouteEdge ;
        rdfs:label ?label ;
        acc:fromSpace ?from ;
        acc:toSpace ?to .
  ?from acc:centerX ?fx ; acc:centerY ?fy .
  ?to acc:centerX ?tx ; acc:centerY ?ty .
}
"""
    for row in graph.query(query, initBindings={"label": Literal(route_edge_label)}):
        dx = float(row.fx) - float(row.tx)
        dy = float(row.fy) - float(row.ty)
        return max((dx * dx + dy * dy) ** 0.5, 1.0)
    return 1.0


def _xy(graph: Graph, subject: URIRef) -> tuple[float, float] | None:
    x = graph.value(subject, ACC.centerX)
    y = graph.value(subject, ACC.centerY)
    if x is None or y is None:
        return None
    return float(x), float(y)


def _change_key(label: str, target_width: float, strategy: str) -> str:
    digest = sha1(f"{label}|{target_width}|{strategy}".encode("utf-8")).hexdigest()[:10]
    return f"change_{digest}"


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _percent(change: float, base: float) -> float:
    if abs(base) < 0.0001:
        return 0.0
    return change / base * 100


def _add_box(fig, bounds, name: str, color: str, text: str) -> None:
    x0, x1, y0, y1, z0, z1 = bounds
    vertices = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    fig.add_trace(
        go.Mesh3d(
            x=[item[0] for item in vertices],
            y=[item[1] for item in vertices],
            z=[item[2] for item in vertices],
            i=[item[0] for item in faces],
            j=[item[1] for item in faces],
            k=[item[2] for item in faces],
            color=color,
            flatshading=True,
            name=name,
            hovertemplate=html.escape(text) + "<extra></extra>",
        )
    )
