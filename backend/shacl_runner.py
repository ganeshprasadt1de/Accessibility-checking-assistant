from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF
from rdflib.namespace import SH

from .config import NS, RULE_LIMITS
from .ifc_tools import element_uri
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
    report_ttl.write_bytes(report_graph if isinstance(report_graph, bytes) else str(report_graph).encode("utf-8"))
    result_count = str(report_text).count("Constraint Violation")
    return {
        "available": True,
        "conforms": bool(conforms),
        "source": "SHACL SPARQL constraints through pySHACL",
        "resultCount": result_count,
        "message": f"Conforms: {bool(conforms)}. Constraint violations: {result_count}. Full report is in shacl_report.ttl.",
    }


def issues_from_shacl_report(report_ttl: Path, data_graph: Path, elements: list[Element], edges: list[RouteEdge]) -> list[Issue]:
    report = Graph()
    report.parse(report_ttl, format="turtle")
    data = Graph()
    data.parse(data_graph, format="turtle")

    elements_by_uri = {str(element_uri(element.guid)): element for element in elements}
    edges_by_uri = {str(ACC[f"route/{edge.edge_id}"]): edge for edge in edges}
    element_by_guid = {element.guid: element for element in elements}
    issues: list[Issue] = []
    failed_route_reasons: dict[str, set[str]] = {edge.edge_id: set() for edge in edges}

    for result in report.subjects(RDF.type, SH.ValidationResult):
        focus = report.value(result, SH.focusNode)
        message_node = report.value(result, SH.resultMessage)
        if focus is None or message_node is None:
            continue
        message = str(message_node)
        rule_id, readable = _split_message(message)
        focus_text = str(focus)
        edge = edges_by_uri.get(focus_text)
        element = elements_by_uri.get(focus_text)
        measured = _measured_value(data, focus, rule_id)
        required, unit = _required(rule_id)

        if edge:
            failed_route_reasons[edge.edge_id].add(_route_reason(rule_id))
            element = element_by_guid.get(edge.via_space_guid or edge.start_guid)
        if not element:
            continue
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
            )
        )

    for edge in edges:
        reasons = sorted(reason for reason in failed_route_reasons.get(edge.edge_id, set()) if reason)
        edge.reasons = reasons
        edge.status = "fail" if reasons else "pass"
    return issues


def _split_message(message: str) -> tuple[str, str]:
    if "|" in message:
        code, readable = message.split("|", 1)
        return code.strip(), readable.strip()
    return "shacl_violation", message.strip()


def _route_reason(rule_id: str) -> str:
    return {
        "route_door_width": "door_width",
        "route_width": "route_width",
        "route_turning_space": "turning_space",
        "route_ramp_slope": "ramp_slope",
        "route_ramp_width": "ramp_width",
    }.get(rule_id, rule_id)


def _short_text(rule_id: str) -> str:
    return {
        "door_width": "door too narrow",
        "missing_door_width": "door width missing",
        "corridor_width": "corridor too narrow",
        "turning_space": "turning space too small",
        "ramp_slope": "ramp too steep",
        "ramp_width": "ramp too narrow",
        "route_door_width": "route uses narrow door",
        "route_width": "route too narrow",
        "route_turning_space": "route turn too small",
        "stair_block": "route intersects stair",
        "route_ramp_slope": "route ramp too steep",
        "route_ramp_width": "route ramp too narrow",
    }.get(rule_id, "SHACL violation")


def _required(rule_id: str) -> tuple[float | None, str]:
    if "door_width" in rule_id:
        return RULE_LIMITS.door_width_m, "m"
    if rule_id in {"corridor_width", "route_width"}:
        return RULE_LIMITS.corridor_width_m, "m"
    if "turning_space" in rule_id:
        return RULE_LIMITS.turning_space_m, "m"
    if "ramp_slope" in rule_id:
        return RULE_LIMITS.ramp_slope_percent, "%"
    if "ramp_width" in rule_id:
        return RULE_LIMITS.ramp_width_m, "m"
    if rule_id == "stair_block":
        return 0.0, "bool"
    return None, ""


def _measured_value(data: Graph, focus, rule_id: str) -> float | None:
    predicate = {
        "door_width": ACC.derivedDoorWidthM,
        "corridor_width": ACC.derivedClearSpaceWidthM,
        "turning_space": ACC.turningSpaceM,
        "ramp_slope": ACC.rampSlopePercent,
        "ramp_width": ACC.rampUsableWidthM,
        "route_door_width": ACC.routeDoorWidthMinM,
        "route_width": ACC.routeClearWidthM,
        "route_turning_space": ACC.routeTurningSpaceM,
        "route_ramp_slope": ACC.routeRampSlopePercent,
        "route_ramp_width": ACC.routeRampUsableWidthM,
    }.get(rule_id)
    if rule_id == "stair_block":
        return 1.0
    if predicate is None:
        return None
    value = data.value(focus, predicate)
    if isinstance(value, Literal):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None
