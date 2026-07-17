from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import SH

from .config import NS, RULE_LIMITS
from .ifc_tools import element_uri, passing_area_gap_uri
from .model import Element, Issue, RouteEdge

ACC = Namespace(NS["acc"])


def run_shacl(data_graph: Path, shapes_graph: Path, report_ttl: Path) -> dict:
    try:
        from pyshacl import validate
    except Exception as exc:
        raise RuntimeError(f"pySHACL is not installed or could not be imported: {exc}") from exc
    conforms, report_graph, report_text = validate(
        str(data_graph),
        shacl_graph=str(shapes_graph),
        data_graph_format="turtle",
        shacl_graph_format="turtle",
        advanced=True,
        inference="rdfs",
        serialize_report_graph=True,
    )
    report = Graph()
    report.parse(data=report_graph if isinstance(report_graph, str) else report_graph.decode("utf-8"), format="turtle")
    _write_report(report, bool(conforms), report_ttl)
    result_count = str(report_text).count("Constraint Violation")
    return {
        "available": True,
        "conforms": bool(conforms),
        "source": "SHACL SPARQL constraints through pySHACL",
        "resultCount": result_count,
        "message": f"Conforms: {bool(conforms)}. Constraint violations: {result_count}. Full report is in shacl_report.ttl.",
    }


def issues_from_shacl_report(
    report_ttl: Path,
    data_graph: Path,
    elements: list[Element],
    edges: list[RouteEdge],
    plan_edges: list[RouteEdge] | None = None,
) -> list[Issue]:
    report = Graph()
    report.parse(report_ttl, format="turtle")
    data = Graph()
    data.parse(data_graph, format="turtle")

    elements_by_uri = {str(element_uri(element.guid)): element for element in elements}
    rdf_plan_edges = [edge for edge in plan_edges or [] if not edge.measurements.get("planMarkerOnly")]
    route_edges = [*edges, *rdf_plan_edges]
    edges_by_uri = {str(ACC[f"route/{edge.edge_id}"]): edge for edge in route_edges}
    gaps_by_uri = {
        str(passing_area_gap_uri(gap["evidence_id"])): (element, gap)
        for element in elements
        for gap in element.passing_area_gaps
    }
    issues: list[Issue] = []
    issue_keys = set()
    failed_route_reasons: dict[str, set[str]] = {uri: set() for uri in edges_by_uri}

    for focus_text, message, _source_shape, _source_component in _validation_results(report):
        rule_id, readable = _split_message(message)
        edge = edges_by_uri.get(focus_text)
        element = elements_by_uri.get(focus_text)
        evidence_id = None
        if focus_text in gaps_by_uri:
            element, gap = gaps_by_uri[focus_text]
            evidence_id = gap["evidence_id"]
        measured = _measured_value(data, URIRef(focus_text), rule_id)
        required, unit = _required(rule_id, data, URIRef(focus_text))

        if edge:
            failed_route_reasons[focus_text].add(_route_reason(rule_id))
            continue
        if not element:
            continue
        issue_key = element.guid, rule_id, evidence_id
        if issue_key in issue_keys:
            continue
        issue_keys.add(issue_key)
        issues.append(
            Issue(
                issue_id=f"I{len(issues) + 1:04d}",
                element_guid=element.guid,
                element_label=element.label,
                element_type=element.ifc_type,
                rule_id=rule_id,
                severity="fail",
                measured=measured,
                required=required,
                unit=unit,
                source="SHACL validation report",
                short_text=_short_text(rule_id),
                details=readable,
                evidence_id=evidence_id,
            )
        )

    for edge in edges:
        uri = str(ACC[f"route/{edge.edge_id}"])
        reasons = sorted(reason for reason in failed_route_reasons.get(uri, set()) if reason)
        edge.reasons = reasons
        edge.status = "fail" if reasons else "pass"
    for edge in rdf_plan_edges:
        uri = str(ACC[f"route/{edge.edge_id}"])
        reasons = sorted(reason for reason in failed_route_reasons.get(uri, set()) if reason)
        if set(reasons) != set(edge.reasons):
            raise RuntimeError(
                f"Plan route {edge.edge_id} disagrees with SHACL: Python={sorted(edge.reasons)}, SHACL={reasons}"
            )
        edge.status = "fail" if edge.reasons else "pass"
    return issues


def _validation_results(report: Graph) -> list[tuple[str, str, str | None, str | None]]:
    rows: list[tuple[str, str, str | None, str | None]] = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        focus = report.value(result, SH.focusNode)
        message = report.value(result, SH.resultMessage)
        source_shape = report.value(result, SH.sourceShape)
        source_component = report.value(result, SH.sourceConstraintComponent)
        if focus is not None and message is not None:
            rows.append(
                (
                    str(focus),
                    str(message),
                    source_shape.n3(report.namespace_manager) if source_shape is not None else None,
                    source_component.n3(report.namespace_manager) if source_component is not None else None,
                )
            )
    return sorted(rows, key=lambda item: (item[0], item[1]))


def _write_report(report: Graph, conforms: bool, report_ttl: Path) -> None:
    rows = _validation_results(report)
    lines = [
        "@prefix acc: <https://example.org/wheelchair-accessibility#> .",
        "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        "",
        "[] a sh:ValidationReport ;",
        f"    sh:conforms {'true' if conforms else 'false'}" + (" ." if not rows else " ;"),
    ]
    for index, (focus, message, source_shape, source_component) in enumerate(rows):
        suffix = " ." if index == len(rows) - 1 else " ;"
        result_lines = [
            "    sh:result [ a sh:ValidationResult ;",
            f"            sh:focusNode <{focus}> ;",
            f"            sh:resultMessage {Literal(message).n3()} ;",
            "            sh:resultSeverity sh:Violation ;",
        ]
        if source_component:
            result_lines.append(f"            sh:sourceConstraintComponent {source_component} ;")
        if source_shape:
            result_lines.append(f"            sh:sourceShape {source_shape} ;")
        result_lines.append(f"            sh:value <{focus}> ]{suffix}")
        lines.extend(result_lines)
    report_ttl.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _split_message(message: str) -> tuple[str, str]:
    if "|" in message:
        code, readable = message.split("|", 1)
        return code.strip(), readable.strip()
    return "shacl_violation", message.strip()


def _route_reason(rule_id: str) -> str:
    return {
        "route_door_width": "door_width",
        "route_door_height": "door_height",
        "route_width": "route_width",
        "route_turning_space": "turning_space",
        "route_wall_block": "wall_block",
        "route_unreachable": "unreachable",
        "route_ramp_slope": "ramp_slope",
        "route_ramp_width": "ramp_width",
        "route_ramp_run_length": "ramp_run_length",
    }.get(rule_id, rule_id)


def _short_text(rule_id: str) -> str:
    return {
        "door_width": "door too narrow",
        "missing_door_width": "door width missing",
        "door_height": "door too low",
        "missing_door_height": "door height missing",
        "corridor_width": "corridor too narrow",
        "corridor_slope": "corridor too steep",
        "corridor_movement_area": "passing areas spaced too far apart",
        "turning_space": "turning space too small",
        "ramp_slope": "ramp too steep",
        "ramp_width": "ramp too narrow",
        "ramp_run_length": "ramp flight too long",
        "route_door_width": "route uses narrow door",
        "route_door_height": "route uses low door",
        "route_width": "route too narrow",
        "route_turning_space": "route turn too small",
        "route_wall_block": "route intersects wall",
        "route_unreachable": "door not connected",
        "stair_block": "route intersects stair",
        "route_ramp_slope": "route ramp too steep",
        "route_ramp_width": "route ramp too narrow",
        "route_ramp_run_length": "route ramp flight too long",
    }.get(rule_id, "SHACL violation")


def _required(rule_id: str, data: Graph | None = None, focus=None) -> tuple[float | None, str]:
    if "door_width" in rule_id:
        return RULE_LIMITS.door_width_m, "m"
    if "door_height" in rule_id:
        return RULE_LIMITS.door_height_m, "m"
    if rule_id in {"corridor_width", "route_width"}:
        return RULE_LIMITS.corridor_width_m, "m"
    if rule_id == "corridor_slope":
        length = _graph_number(data, focus, ACC.derivedCorridorLengthM)
        if length is not None and length <= RULE_LIMITS.short_corridor_length_m:
            return RULE_LIMITS.short_corridor_slope_percent, "%"
        return RULE_LIMITS.corridor_slope_percent, "%"
    if rule_id == "corridor_movement_area":
        return RULE_LIMITS.corridor_movement_interval_m, "m"
    if "turning_space" in rule_id:
        return RULE_LIMITS.turning_space_m, "m"
    if "ramp_slope" in rule_id:
        return RULE_LIMITS.ramp_slope_percent, "%"
    if "ramp_width" in rule_id:
        return RULE_LIMITS.ramp_width_m, "m"
    if "ramp_run_length" in rule_id:
        return RULE_LIMITS.ramp_run_length_m, "m"
    if rule_id in {"stair_block", "route_wall_block"}:
        return 0.0, "bool"
    if rule_id == "route_unreachable":
        return 1.0, "bool"
    return None, ""


def _measured_value(data: Graph, focus, rule_id: str) -> float | None:
    predicate = {
        "door_width": ACC.derivedDoorWidthM,
        "door_height": ACC.derivedDoorHeightM,
        "corridor_width": ACC.derivedClearSpaceWidthM,
        "corridor_slope": ACC.derivedCorridorSlopePercent,
        "corridor_movement_area": ACC.passingAreaGapLengthM,
        "turning_space": ACC.turningSpaceM,
        "ramp_slope": ACC.rampSlopePercent,
        "ramp_width": ACC.rampUsableWidthM,
        "ramp_run_length": ACC.rampRunLengthM,
        "route_door_width": ACC.routeDoorWidthMinM,
        "route_door_height": ACC.routeDoorHeightMinM,
        "route_width": ACC.routeClearWidthM,
        "route_turning_space": ACC.routeTurningSpaceM,
        "route_wall_block": ACC.routeHitsWall,
        "route_unreachable": ACC.routeReachable,
        "route_ramp_slope": ACC.routeRampSlopePercent,
        "route_ramp_width": ACC.routeRampUsableWidthM,
        "route_ramp_run_length": ACC.routeRampRunLengthM,
    }.get(rule_id)
    if rule_id in {"stair_block", "route_wall_block"}:
        return 1.0
    if rule_id == "route_unreachable":
        return 0.0
    if predicate is None:
        return None
    value = data.value(focus, predicate)
    if isinstance(value, Literal):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _graph_number(data: Graph | None, focus, predicate) -> float | None:
    if data is None or focus is None:
        return None
    value = data.value(focus, predicate)
    if isinstance(value, Literal):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None
