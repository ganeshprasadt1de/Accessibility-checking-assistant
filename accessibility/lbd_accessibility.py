from __future__ import annotations

import re
from dataclasses import fields

from rdflib import Graph, Literal, RDF, RDFS, URIRef

from accessibility.model import Element


BOT_ELEMENT = URIRef("https://w3id.org/bot#Element")
BOT_SPACE = URIRef("https://w3id.org/bot#Space")
ACC_ROUTE_EDGE = URIRef("http://example.org/accessibility#RouteEdge")


KIND_ALIASES = {
    "Route edge": ["routeedge", "wheelchairroute"],
    "Door": ["door", "tuer", "tÃ¼r"],
    "Ramp": ["ramp", "rampe"],
    "Lift": ["lift", "elevator", "aufzug"],
    "Corridor": ["corridor", "flur", "gang", "circulation", "verkehr"],
    "Accessible toilet": ["toilet", "wc", "sanitary", "restroom", "bathroom", "barrierefreiwc", "rollstuhlwc"],
}


VALUE_ALIASES = {
    "clear_width_m": ["clearwidth", "overallwidth", "openingwidth", "width", "lichtebreite", "durchgangsbreite"],
    "clear_height_m": ["clearheight", "overallheight", "openingheight", "height", "lichtehoehe", "durchgangshoehe"],
    "approach_space_m": ["approachspace", "lateralapproachspace", "movementspace", "bewegungsflaeche"],
    "reveal_depth_m": ["revealdepth", "laibungstiefe"],
    "threshold_height_m": ["thresholdheight", "sillheight", "schwellenhoehe"],
    "handle_height_m": ["handleheight", "controlheight", "operatingheight", "griffhoehe", "bedienhoehe"],
    "slope_percent": ["slopepercent", "slope", "rampslope", "gradient", "steigung", "laengsneigung"],
    "usable_width_m": ["usablewidth", "clearwidth", "width", "nutzbreite", "laufbreite"],
    "length_m": ["length", "ramplength", "laenge"],
    "platform_length_m": ["platformlength", "landinglength", "podestlaenge"],
    "has_handrails": ["hashandrails", "hashandrail", "handrails", "handlauf"],
    "has_edge_protection": ["hasedgeprotection", "wheeldeflector", "raisedkerb", "radabweiser"],
    "has_cross_slope": ["hascrossslope", "crossslope", "querneigung"],
    "handrail_height_m": ["handrailheight", "railheight", "handlaufhoehe"],
    "handrail_diameter_m": ["handraildiameter", "raildiameter", "handlaufdurchmesser"],
    "handrail_extension_m": ["handrailextension", "railextension", "handlaufverlaengerung"],
    "start_area_width_m": ["startmovementareawidth", "startareawidth", "lowerlandingwidth"],
    "start_area_depth_m": ["startmovementareadepth", "startareadepth", "lowerlandingdepth"],
    "end_area_width_m": ["endmovementareawidth", "endareawidth", "upperlandingwidth"],
    "end_area_depth_m": ["endmovementareadepth", "endareadepth", "upperlandingdepth"],
    "passing_space_m": ["passingspace", "passingplace", "begegnungsflaeche"],
    "door_width_m": ["doorwidth", "liftdoorwidth", "cleardoorwidth", "aufzugtuerbreite"],
    "cabin_width_m": ["cabinwidth", "carwidth", "liftcabinwidth", "kabinenbreite"],
    "cabin_depth_m": ["cabindepth", "cardepth", "liftcabindepth", "kabinentiefe"],
    "movement_area_width_m": ["movementareawidth", "clearareawidth", "turningareawidth"],
    "movement_area_depth_m": ["movementareadepth", "clearareadepth", "turningareadepth"],
    "turning_diameter_m": ["turningdiameter", "turningcircle", "turningspace", "wendekreis"],
    "opens_inward": ["opensinward", "dooropensinward"],
    "has_washbasin": ["haswashbasin", "washbasin", "hassink"],
    "side_approach_width_m": ["sideapproachwidth"],
    "side_approach_depth_m": ["sideapproachdepth"],
    "has_emergency_call": ["hasemergencycall", "emergencycall"],
    "route_door_width_m": ["routedoorwidth", "doorwidth"],
    "route_level_change_m": ["levelchangem", "levelchange"],
    "route_pass": ["routepass"],
}


def extract_lbd_elements(graph: Graph) -> list[Element]:
    elements = []
    element_fields = {field.name for field in fields(Element)}
    for index, subject in enumerate(_candidate_subjects(graph), start=1):
        kind = _kind(graph, subject)
        if not kind:
            continue

        values = {}
        for field_name, aliases in VALUE_ALIASES.items():
            if field_name not in element_fields:
                continue
            value = _find_value(graph, subject, aliases)
            if field_name == "slope_percent" and isinstance(value, (int, float)) and 0 < value <= 1:
                value = value * 100
            values[field_name] = value
        elements.append(
            Element(
                key=_safe_key(subject, kind, index),
                name=_label(graph, subject, kind),
                kind=kind,
                source="IFCtoLBD",
                **values,
            )
        )
    return elements


def lbd_value(graph: Graph, node, rule: str) -> str:
    field_name = RULE_TO_FIELD.get(rule)
    if not field_name:
        return "missing"
    value = _find_value(graph, node, VALUE_ALIASES.get(field_name, []))
    if field_name == "slope_percent" and isinstance(value, (int, float)) and 0 < value <= 1:
        value = value * 100
    return "missing" if value is None else str(value)


def lbd_kind(graph: Graph, node) -> str:
    return _kind(graph, node) or "Element"


RULE_TO_FIELD = {
    "Door clear width": "clear_width_m",
    "Door clear height": "clear_height_m",
    "Door approach space": "approach_space_m",
    "Door reveal depth": "reveal_depth_m",
    "Door threshold height": "threshold_height_m",
    "Door handle height": "handle_height_m",
    "Ramp slope": "slope_percent",
    "Ramp usable width": "usable_width_m",
    "Ramp run length": "length_m",
    "Ramp platform length": "platform_length_m",
    "Ramp handrails": "has_handrails",
    "Ramp edge protection": "has_edge_protection",
    "Ramp cross slope": "has_cross_slope",
    "Ramp handrail height": "handrail_height_m",
    "Ramp handrail diameter": "handrail_diameter_m",
    "Ramp handrail extension": "handrail_extension_m",
    "Ramp start area width": "start_area_width_m",
    "Ramp start area depth": "start_area_depth_m",
    "Ramp end area width": "end_area_width_m",
    "Ramp end area depth": "end_area_depth_m",
    "Lift door width": "door_width_m",
    "Lift cabin size": "cabin_width_m",
    "Corridor clear width": "clear_width_m",
    "Passing space": "passing_space_m",
    "Accessible toilet movement width": "movement_area_width_m",
    "Accessible toilet movement depth": "movement_area_depth_m",
    "Accessible toilet turning space": "turning_diameter_m",
    "Accessible toilet door direction": "opens_inward",
    "Accessible toilet washbasin": "has_washbasin",
    "Accessible toilet side approach width": "side_approach_width_m",
    "Accessible toilet side approach depth": "side_approach_depth_m",
    "Accessible toilet emergency call": "has_emergency_call",
    "Route topology": "has_route_door",
    "Route door width": "route_door_width_m",
    "Route level change": "route_level_change_m",
    "Route pass result": "route_pass",
}


def _candidate_subjects(graph: Graph) -> list[URIRef]:
    subjects = set(graph.subjects(RDF.type, BOT_ELEMENT))
    subjects.update(graph.subjects(RDF.type, BOT_SPACE))
    subjects.update(graph.subjects(RDF.type, ACC_ROUTE_EDGE))
    return sorted(subjects, key=str)


def _kind(graph: Graph, subject) -> str | None:
    text_parts = [_label(graph, subject, "")]
    text_parts.extend(_local_name(value) for value in graph.objects(subject, RDF.type))
    text = _normalize(" ".join(text_parts))

    for kind, aliases in KIND_ALIASES.items():
        if any(alias in text for alias in aliases):
            return kind
    return None


def _label(graph: Graph, subject, fallback: str) -> str:
    label = graph.value(subject, RDFS.label)
    if isinstance(label, Literal) and str(label).strip():
        return str(label)
    text = str(subject)
    if "#" in text:
        return text.rsplit("#", 1)[1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[1]
    return fallback or text


def _find_value(graph: Graph, subject, aliases: list[str]):
    aliases = [_normalize(alias) for alias in aliases]
    for predicate, obj in graph.predicate_objects(subject):
        local = _normalize(_local_name(predicate))
        if any(alias in local for alias in aliases):
            value = _literal_value(obj)
            if isinstance(value, bool) and not _expects_boolean(aliases):
                continue
            if value is not None:
                return value
    return None


def _expects_boolean(aliases: list[str]) -> bool:
    return any(alias.startswith(("has", "is", "opens")) for alias in aliases)


def _literal_value(value):
    if not isinstance(value, Literal):
        return None
    if isinstance(value.value, bool):
        return value.value
    if isinstance(value.value, (int, float)):
        return _as_percent_if_needed(float(value.value), value)
    text = str(value).strip()
    lower = text.lower()
    if lower in {"true", "yes", "1", "ja"}:
        return True
    if lower in {"false", "no", "0", "nein"}:
        return False
    try:
        return _as_percent_if_needed(float(text.replace(",", ".")), value)
    except ValueError:
        return text


def _as_percent_if_needed(number: float, literal: Literal) -> float:
    label = _normalize(str(literal))
    if 0 < number <= 1 and ("slope" in label or "gradient" in label or "steigung" in label):
        return number * 100
    return number


def _safe_key(subject, kind: str, index: int) -> str:
    text = _local_name(subject)
    safe = re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_")
    return safe or f"{kind}_{index}"


def _local_name(uri) -> str:
    text = str(uri)
    if "#" in text:
        return text.rsplit("#", 1)[1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[1]
    return text


def _normalize(text: str) -> str:
    replacements = {
        "Ã¤": "ae",
        "Ã¶": "oe",
        "Ã¼": "ue",
        "ÃŸ": "ss",
        "Ã£Â¤": "ae",
        "Ã£Â¶": "oe",
        "Ã£Â¼": "ue",
        "Ã£Ã¿": "ss",
    }
    text = text.lower()
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", text)

