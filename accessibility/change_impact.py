from __future__ import annotations

import html
from dataclasses import dataclass
from hashlib import sha1

from rdflib import Graph, Literal, Namespace, RDF, RDFS
from rdflib.namespace import XSD

from accessibility.model import GeometryFinding, Issue

try:
    import plotly.graph_objects as go
except ModuleNotFoundError:
    go = None


ACC = Namespace("http://example.org/accessibility#")
CHANGE = Namespace("http://example.org/accessibility-change#")

DEFAULT_STOREY_HEIGHT_M = 3.0

NUMERIC_RULE_TARGETS = {
    "Door clear width": ("at least", 0.90, "m"),
    "Route door width": ("at least", 0.90, "m"),
    "Door clear height": ("at least", 2.05, "m"),
    "Door threshold height": ("at most", 0.02, "m"),
    "Door handle height": ("range", 0.95, "m"),
    "Door reveal depth": ("at most", 0.26, "m"),
    "Ramp slope": ("at most", 6.0, "percent"),
    "Ramp usable width": ("at least", 1.20, "m"),
    "Ramp run length": ("at most", 6.0, "m"),
    "Ramp platform length": ("at least", 1.50, "m"),
    "Ramp handrail height": ("range", 0.875, "m"),
    "Ramp handrail diameter": ("range", 0.0375, "m"),
    "Ramp handrail extension": ("at least", 0.30, "m"),
    "Ramp start area width": ("at least", 1.50, "m"),
    "Ramp start area depth": ("at least", 1.50, "m"),
    "Ramp end area width": ("at least", 1.50, "m"),
    "Ramp end area depth": ("at least", 1.50, "m"),
    "Lift door width": ("at least", 0.90, "m"),
    "Corridor clear width": ("at least", 1.20, "m"),
    "Passing space": ("at least", 1.80, "m"),
    "Accessible toilet movement width": ("at least", 1.50, "m"),
    "Accessible toilet movement depth": ("at least", 1.50, "m"),
    "Accessible toilet turning space": ("at least", 1.50, "m"),
    "Accessible toilet side approach width": ("at least", 0.90, "m"),
    "Accessible toilet side approach depth": ("at least", 0.70, "m"),
    "Route level change": ("at most", 0.02, "m"),
}

BOOLEAN_RULE_FIXES = {
    "Ramp handrails": "Add continuous handrails on both sides of the ramp.",
    "Ramp edge protection": "Add edge protection so wheelchair wheels cannot leave the ramp edge.",
    "Ramp cross slope": "Remove cross slope or keep it within the allowed route condition.",
    "Accessible toilet door direction": "Make the toilet door open outward or slide so it does not block the wheelchair movement area.",
    "Accessible toilet washbasin": "Add a reachable washbasin inside the accessible toilet layout.",
    "Accessible toilet emergency call": "Add an emergency call device reachable from the toilet position.",
    "Route topology": "Add a usable door boundary or connection so the route graph can connect the spaces.",
    "Route pass result": "Fix the failed route edge checks such as door width, level change, or missing route data.",
}


@dataclass
class ImpactOption:
    key: str
    label: str
    source: str
    element: str
    rule: str
    check: str
    current_value: str
    required_value: str
    action: str
    strategy: str
    old_footprint_m2: float
    new_footprint_m2: float
    footprint_change_m2: float
    footprint_change_percent: float
    old_volume_m3: float
    new_volume_m3: float
    volume_change_m3: float
    affected_zone: str
    affected_zone_area_before_m2: float
    affected_zone_area_after_m2: float
    affected_zone_area_change_m2: float
    plot_limit_m2: float
    fits_plot: bool
    explanation: str


def impact_options(issues: list[Issue], clearance_findings: list[GeometryFinding]) -> list[dict[str, str]]:
    options = []
    seen = set()
    for finding in clearance_findings:
        if finding.result != "failed":
            continue
        key = f"3d|{finding.element}|{finding.reason}"
        if key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "label": f"3D clearance volume | {finding.element}",
                "source": "Detailed 3D clearance",
                "element": finding.element,
                "rule": "3D clearance volume",
                "check": finding.check,
                "current_value": "clearance volume intersects obstacle boxes",
                "required_value": "0.90 m wide and 2.05 m high clear volume",
                "reason": finding.reason,
                "fix": finding.fix,
            }
        )

    for issue in issues:
        if issue.rule not in NUMERIC_RULE_TARGETS and issue.rule not in BOOLEAN_RULE_FIXES:
            continue
        key = f"issue|{issue.element_key}|{issue.rule}|{issue.value}"
        if key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "label": f"{issue.rule} | {issue.element_name}",
                "source": "SHACL and SPARQL",
                "element": issue.element_name,
                "rule": issue.rule,
                "check": issue.rule,
                "current_value": issue.value,
                "required_value": issue.required,
                "reason": issue.message,
                "fix": _fix_for_issue(issue),
            }
        )
    return options


def calculate_impact_option(
    graph: Graph,
    candidate: dict[str, str],
    strategy: str,
    plot_limit_m2: float,
    storey_height_m: float = DEFAULT_STOREY_HEIGHT_M,
) -> ImpactOption:
    old_footprint = _building_footprint(graph)
    old_volume = old_footprint * storey_height_m
    current = _float(candidate.get("current_value"))
    target_info = NUMERIC_RULE_TARGETS.get(candidate.get("rule", ""))
    target = target_info[1] if target_info else None
    unit = target_info[2] if target_info else ""
    change = _change_amount(current, target, target_info[0] if target_info else "")
    affected_zone = _affected_zone(candidate)
    affected_area = _estimated_affected_area(graph, candidate)
    footprint_change = _estimated_footprint_change(candidate, change, affected_area)

    if strategy == "expand building outward":
        new_footprint = old_footprint + footprint_change
        area_after = affected_area
        area_change = 0.0
    else:
        new_footprint = old_footprint
        area_change = -min(footprint_change, affected_area)
        area_after = max(affected_area + area_change, 0.0)
        footprint_change = 0.0

    new_volume = new_footprint * storey_height_m
    fits_plot = new_footprint <= plot_limit_m2 if plot_limit_m2 > 0 else True
    rule = candidate.get("rule", "Accessibility check")
    element = candidate.get("element", "building element")
    key = _change_key(f"{rule}|{element}|{candidate.get('reason', '')}", target or 0.0, strategy)
    action = _impact_action(candidate, current, target, unit)
    explanation = _impact_explanation(
        candidate,
        action,
        strategy,
        footprint_change,
        old_footprint,
        affected_zone,
        area_change,
        fits_plot,
    )

    return ImpactOption(
        key=key,
        label=f"Impact option {key}",
        source=candidate.get("source", ""),
        element=element,
        rule=rule,
        check=candidate.get("check", rule),
        current_value=candidate.get("current_value", "missing"),
        required_value=candidate.get("required_value", "rule value"),
        action=action,
        strategy=strategy,
        old_footprint_m2=round(old_footprint, 3),
        new_footprint_m2=round(new_footprint, 3),
        footprint_change_m2=round(new_footprint - old_footprint, 3),
        footprint_change_percent=round(_percent(new_footprint - old_footprint, old_footprint), 3),
        old_volume_m3=round(old_volume, 3),
        new_volume_m3=round(new_volume, 3),
        volume_change_m3=round(new_volume - old_volume, 3),
        affected_zone=affected_zone,
        affected_zone_area_before_m2=round(affected_area, 3),
        affected_zone_area_after_m2=round(area_after, 3),
        affected_zone_area_change_m2=round(area_change, 3),
        plot_limit_m2=round(plot_limit_m2, 3),
        fits_plot=fits_plot,
        explanation=explanation,
    )


def add_impact_option_to_graph(graph: Graph, option: ImpactOption) -> None:
    subject = CHANGE[option.key]
    graph.add((subject, RDF.type, ACC.ChangeOption))
    graph.add((subject, RDFS.label, Literal(option.label)))
    graph.add((subject, ACC.sourceCheck, Literal(option.source)))
    graph.add((subject, ACC.affectedElementLabel, Literal(option.element)))
    graph.add((subject, ACC.affectedRule, Literal(option.rule)))
    graph.add((subject, ACC.currentValue, Literal(option.current_value)))
    graph.add((subject, ACC.requiredValue, Literal(option.required_value)))
    graph.add((subject, ACC.recommendedAction, Literal(option.action)))
    graph.add((subject, ACC.strategy, Literal(option.strategy)))
    graph.add((subject, ACC.oldFootprintM2, Literal(option.old_footprint_m2, datatype=XSD.double)))
    graph.add((subject, ACC.newFootprintM2, Literal(option.new_footprint_m2, datatype=XSD.double)))
    graph.add((subject, ACC.footprintChangeM2, Literal(option.footprint_change_m2, datatype=XSD.double)))
    graph.add((subject, ACC.footprintChangePercent, Literal(option.footprint_change_percent, datatype=XSD.double)))
    graph.add((subject, ACC.oldVolumeM3, Literal(option.old_volume_m3, datatype=XSD.double)))
    graph.add((subject, ACC.newVolumeM3, Literal(option.new_volume_m3, datatype=XSD.double)))
    graph.add((subject, ACC.volumeChangeM3, Literal(option.volume_change_m3, datatype=XSD.double)))
    graph.add((subject, ACC.affectedZoneLabel, Literal(option.affected_zone)))
    graph.add((subject, ACC.affectedZoneAreaBeforeM2, Literal(option.affected_zone_area_before_m2, datatype=XSD.double)))
    graph.add((subject, ACC.affectedZoneAreaAfterM2, Literal(option.affected_zone_area_after_m2, datatype=XSD.double)))
    graph.add((subject, ACC.affectedZoneAreaChangeM2, Literal(option.affected_zone_area_change_m2, datatype=XSD.double)))
    graph.add((subject, ACC.plotLimitM2, Literal(option.plot_limit_m2, datatype=XSD.double)))
    graph.add((subject, ACC.fitsPlot, Literal(option.fits_plot, datatype=XSD.boolean)))
    graph.add((subject, ACC.changeExplanation, Literal(option.explanation)))


def impact_rows(option: ImpactOption) -> list[dict[str, float | str | bool]]:
    return [
        {"Item": "Checked element", "Before": option.element, "After": option.rule, "Change": option.source},
        {"Item": "Current value", "Before": option.current_value, "After": option.required_value, "Change": option.action},
        {"Item": "Building footprint m2", "Before": option.old_footprint_m2, "After": option.new_footprint_m2, "Change": option.footprint_change_m2},
        {"Item": "Building footprint percent", "Before": 0.0, "After": option.footprint_change_percent, "Change": option.footprint_change_percent},
        {"Item": "Building volume m3", "Before": option.old_volume_m3, "After": option.new_volume_m3, "Change": option.volume_change_m3},
        {"Item": f"{option.affected_zone} area m2", "Before": option.affected_zone_area_before_m2, "After": option.affected_zone_area_after_m2, "Change": option.affected_zone_area_change_m2},
        {"Item": "Fits plot", "Before": "", "After": option.fits_plot, "Change": ""},
    ]


def impact_context(option: ImpactOption | None) -> str:
    if option is None:
        return ""
    return "\n".join(
        [
            "Changes Impact simulation:",
            f"Element: {option.element}.",
            f"Rule: {option.rule}.",
            f"Source check: {option.source}.",
            f"Current value: {option.current_value}.",
            f"Required value: {option.required_value}.",
            f"Recommended action: {option.action}.",
            f"Strategy: {option.strategy}.",
            f"Building footprint changes from {option.old_footprint_m2} m2 to {option.new_footprint_m2} m2.",
            f"Footprint change is {option.footprint_change_m2} m2 or {option.footprint_change_percent} percent.",
            f"Affected zone: {option.affected_zone}. Area changes from {option.affected_zone_area_before_m2} m2 to {option.affected_zone_area_after_m2} m2.",
            f"Fits entered plot limit: {option.fits_plot}.",
            option.explanation,
        ]
    )


def make_change_impact_viewer(option: ImpactOption | None) -> str | None:
    if go is None or option is None:
        return None
    return _make_general_impact_viewer(option)


def _make_general_impact_viewer(option: ImpactOption) -> str | None:
    fig = go.Figure()
    base_width = 5.0
    base_depth = 3.0
    growth = max(abs(option.footprint_change_m2) / max(base_depth, 1.0), 0.08)
    compression = max(abs(option.affected_zone_area_change_m2) / max(base_depth, 1.0), 0.08)

    _add_box(fig, (0, base_width, 0, base_depth, 0, 3.0), "Current checked zone", "rgba(148, 163, 184, 0.22)", "Current route, corridor, room, ramp, lift, toilet, or clearance zone")
    if option.strategy == "expand building outward":
        _add_box(fig, (base_width, base_width + growth, 0, base_depth, 0, 3.0), "Added zone", "rgba(47, 191, 113, 0.46)", option.explanation)
    else:
        _add_box(fig, (base_width, base_width + compression, 0, base_depth, 0, 3.0), "Zone taken from connected space", "rgba(255, 145, 77, 0.52)", option.explanation)

    fig.add_trace(
        go.Scatter3d(
            x=[base_width / 2, base_width + max(growth, compression) / 2],
            y=[base_depth / 2, base_depth / 2],
            z=[1.05, 1.05],
            mode="lines+markers",
            line={"color": "#ffd166", "width": 8},
            marker={"size": 5, "color": "#ffd166"},
            hovertemplate=html.escape(option.explanation) + "<extra></extra>",
            name="Change direction",
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
    This viewer is an impact overlay, not an edited IFC model. It uses the selected detected violation. For 3D clearance findings, the impact refers to the wheelchair-sized volume that collided with obstacle boxes.
  </div>
  {plot}
</div>
"""


def _fix_for_issue(issue: Issue) -> str:
    if issue.rule in BOOLEAN_RULE_FIXES:
        return BOOLEAN_RULE_FIXES[issue.rule]
    target_info = NUMERIC_RULE_TARGETS.get(issue.rule)
    if not target_info:
        return issue.message
    mode, target, unit = target_info
    if mode == "at least":
        return f"Increase {issue.rule.lower()} to at least {target:g} {unit}."
    if mode == "at most":
        return f"Reduce {issue.rule.lower()} to at most {target:g} {unit}."
    return f"Adjust {issue.rule.lower()} to the required range around {target:g} {unit}."


def _change_amount(current: float | None, target: float | None, mode: str) -> float:
    if current is None or target is None:
        return 0.0
    if mode == "at least":
        return max(target - current, 0.0)
    if mode == "at most":
        return max(current - target, 0.0)
    return abs(current - target)


def _estimated_footprint_change(candidate: dict[str, str], change: float, affected_area: float) -> float:
    rule = candidate.get("rule", "")
    if rule == "3D clearance volume":
        return max(0.90 * 1.20, 1.08)
    if "height" in rule.lower() or "slope" in rule.lower() or "threshold" in rule.lower() or "handle" in rule.lower():
        return 0.0
    if change > 0:
        return max(change * max(affected_area ** 0.5, 1.0), 0.0)
    if rule in BOOLEAN_RULE_FIXES:
        return 0.0
    return 0.0


def _estimated_affected_area(graph: Graph, candidate: dict[str, str]) -> float:
    element = candidate.get("element", "")
    query = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?area
WHERE {
  ?item rdfs:label ?label ;
        acc:footprintAreaM2 ?area .
}
"""
    for row in graph.query(query, initBindings={"label": Literal(element)}):
        try:
            return max(float(row.area), 0.0)
        except (TypeError, ValueError):
            pass
    if candidate.get("rule") == "3D clearance volume":
        return 0.90 * 1.20
    return 1.0


def _affected_zone(candidate: dict[str, str]) -> str:
    rule = candidate.get("rule", "")
    if "corridor" in rule.lower() or "route" in rule.lower() or rule == "3D clearance volume":
        return "accessible route zone"
    if "toilet" in rule.lower():
        return "toilet movement zone"
    if "ramp" in rule.lower():
        return "ramp zone"
    if "lift" in rule.lower():
        return "lift zone"
    if "door" in rule.lower():
        return "door approach zone"
    return "connected building zone"


def _impact_action(candidate: dict[str, str], current: float | None, target: float | None, unit: str) -> str:
    rule = candidate.get("rule", "")
    if rule == "3D clearance volume":
        return "Clear the wheelchair volume, remove the obstacle, widen the route zone, or choose another accessible route."
    if rule in BOOLEAN_RULE_FIXES:
        return BOOLEAN_RULE_FIXES[rule]
    if current is None or target is None:
        return f"Provide the missing value and meet {candidate.get('required_value', 'the required value')}."
    if target >= current:
        return f"Increase from {current:g} {unit} to {target:g} {unit}."
    return f"Reduce from {current:g} {unit} to {target:g} {unit}."


def _impact_explanation(
    candidate: dict[str, str],
    action: str,
    strategy: str,
    footprint_change: float,
    old_footprint: float,
    affected_zone: str,
    area_change: float,
    fits_plot: bool,
) -> str:
    percent = _percent(footprint_change, old_footprint)
    source = candidate.get("source", "check")
    rule = candidate.get("rule", "accessibility rule")
    element = candidate.get("element", "building element")
    if strategy == "expand building outward":
        plot_text = "The entered plot limit accepts this option." if fits_plot else "The entered plot limit does not accept this outward growth."
        return (
            f"{source} found a {rule} problem at {element}. "
            f"Recommended action: {action} "
            f"With outward expansion, the estimated added footprint is {footprint_change:.2f} m2, "
            f"which is {percent:.2f} percent of the current footprint. {plot_text}"
        )
    return (
        f"{source} found a {rule} problem at {element}. "
        f"Recommended action: {action} "
        f"With the outer building fixed, the estimated adjustment is taken from {affected_zone}. "
        f"The affected zone changes by {area_change:.2f} m2."
    )


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
