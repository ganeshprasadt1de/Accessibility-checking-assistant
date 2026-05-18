from functools import lru_cache

from rdflib import Graph, Literal, RDFS
from pyshacl import validate

from accessibility.config import SH
from accessibility.lbd_accessibility import lbd_kind, lbd_value
from accessibility.model import Issue
from accessibility.rules import load_shapes_graph


def check_graph(graph: Graph) -> tuple[bool, list[Issue], str]:
    conforms, result_graph, result_text = validate(
        data_graph=graph,
        shacl_graph=_shapes_graph(),
        inference="none",
        abort_on_first=False,
        allow_infos=True,
        allow_warnings=True,
    )

    issues = _read_issues(graph, result_graph)
    return conforms, issues, result_text


@lru_cache(maxsize=1)
def _shapes_graph() -> Graph:
    return load_shapes_graph()


def _read_issues(data_graph: Graph, result_graph: Graph) -> list[Issue]:
    issues = []
    for result in result_graph.subjects(predicate=SH.resultSeverity):
        focus = result_graph.value(result, SH.focusNode)
        message = result_graph.value(result, SH.resultMessage)
        message_text = str(message or "Accessibility rule was not satisfied.")
        rule = _rule_from_message(message_text)

        element_name = _label(data_graph, focus)
        element_kind = lbd_kind(data_graph, focus)
        value = lbd_value(data_graph, focus, rule)
        required = _required_text(rule)

        issues.append(
            Issue(
                element_key=_short_name(focus),
                element_name=element_name,
                element_kind=element_kind,
                rule=rule,
                message=message_text,
                value=value,
                required=required,
                explanation="",
            )
        )
    return issues


def _label(graph: Graph, node) -> str:
    label = graph.value(node, RDFS.label)
    return str(label) if isinstance(label, Literal) else _short_name(node)


def _rule_from_message(message: str) -> str:
    if ":" in message:
        return message.split(":", 1)[0].strip()
    return "Accessibility rule"


def _required_text(rule: str) -> str:
    required = {
        "Door clear width": "at least 0.90 m",
        "Door clear height": "at least 2.05 m",
        "Door approach space": "at least 0.50 m at the lock side",
        "Door reveal depth": "at most 0.26 m where a handle must be reached",
        "Door threshold height": "at most 0.02 m",
        "Door handle height": "between 0.85 m and 1.05 m",
        "Ramp slope": "at most 6 percent",
        "Ramp usable width": "at least 1.20 m",
        "Ramp run length": "at most 6.00 m without a platform",
        "Ramp platform length": "at least 1.50 m",
        "Ramp handrails": "true",
        "Ramp edge protection": "true",
        "Ramp cross slope": "false or not present",
        "Ramp handrail height": "between 0.85 m and 0.90 m",
        "Ramp handrail diameter": "between 0.03 m and 0.045 m",
        "Ramp handrail extension": "at least 0.30 m",
        "Ramp start area width": "at least 1.50 m",
        "Ramp start area depth": "at least 1.50 m",
        "Ramp end area width": "at least 1.50 m",
        "Ramp end area depth": "at least 1.50 m",
        "Lift door width": "at least 0.90 m",
        "Lift cabin size": "at least 1.10 m wide and 1.40 m deep",
        "Corridor clear width": "at least 1.20 m",
        "Passing space": "at least 1.80 m",
        "Accessible toilet movement width": "at least 1.50 m",
        "Accessible toilet movement depth": "at least 1.50 m",
        "Accessible toilet turning space": "at least 1.50 m diameter",
        "Accessible toilet door direction": "door should not open into the movement area",
        "Accessible toilet washbasin": "reachable washbasin present",
        "Accessible toilet side approach width": "at least 0.90 m",
        "Accessible toilet side approach depth": "at least 0.70 m",
        "Accessible toilet emergency call": "emergency call present",
        "Route topology": "at least one route door boundary",
        "Route door width": "at least 0.90 m",
        "Route level change": "at most 0.02 m",
        "Route pass result": "all accessible route edge checks must pass",
    }
    return required.get(rule, "rule value")


def _short_name(uri) -> str:
    text = str(uri)
    if "#" in text:
        return text.rsplit("#", 1)[1]
    if "/" in text:
        return text.rsplit("/", 1)[1]
    return text


