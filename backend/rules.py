from __future__ import annotations

from .config import RULE_LIMITS
from .model import Element, Issue
from .short_explainer import fallback


def make_issue(
    issues: list[Issue],
    element: Element,
    rule_id: str,
    measured: float | None,
    required: float | None,
    unit: str,
    source: str,
    details: str,
) -> None:
    issues.append(
        Issue(
            issue_id=f"I{len(issues) + 1:04d}",
            element_guid=element.guid,
            element_label=element.label,
            element_type=element.ifc_type,
            rule_id=rule_id,
            severity="fail" if measured is not None else "missing",
            measured=measured,
            required=required,
            unit=unit,
            source=source,
            short_text=fallback(rule_id),
            details=details,
        )
    )


def evaluate_value_rules(elements: list[Element]) -> list[Issue]:
    issues: list[Issue] = []
    for element in elements:
        if element.ifc_type == "IfcDoor":
            width = _num(element.extra.get("derivedDoorWidthM"))
            if width is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.door_width_m, "m", "IFC model data", "Door width could not be calculated.")
            elif width < RULE_LIMITS.door_width_m:
                make_issue(issues, element, "door_width", width, RULE_LIMITS.door_width_m, "m", "IFC model geometry", "Door clear width is below the rule target.")
        elif element.ifc_type == "IfcSpace":
            clear = _num(element.extra.get("derivedClearSpaceWidthM"))
            if clear is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.corridor_width_m, "m", "IFC model data", "Space clear width could not be calculated.")
            elif _looks_like_corridor(element) and clear < RULE_LIMITS.corridor_width_m:
                make_issue(issues, element, "corridor_width", clear, RULE_LIMITS.corridor_width_m, "m", "IFC model geometry", "Corridor clear width is below the rule target.")
            turn = _num(element.extra.get("turningSpaceM"))
            if turn is not None and turn < RULE_LIMITS.turning_space_m:
                make_issue(issues, element, "turning_space", turn, RULE_LIMITS.turning_space_m, "m", "IFC model geometry", "Turning space is below the rule target.")
        elif element.ifc_type == "IfcRamp":
            slope = _num(element.extra.get("rampSlopePercent"))
            width = _num(element.extra.get("rampUsableWidthM"))
            if slope is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.ramp_slope_percent, "%", "IFC model data", "Ramp slope could not be calculated.")
            elif slope > RULE_LIMITS.ramp_slope_percent:
                make_issue(issues, element, "ramp_slope", slope, RULE_LIMITS.ramp_slope_percent, "%", "IFC model geometry", "Ramp slope is above the rule target.")
            if width is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.ramp_width_m, "m", "IFC model data", "Ramp width could not be calculated.")
            elif width < RULE_LIMITS.ramp_width_m:
                make_issue(issues, element, "ramp_width", width, RULE_LIMITS.ramp_width_m, "m", "IFC model geometry", "Ramp width is below the rule target.")
    return issues


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_corridor(element: Element) -> bool:
    text = f"{element.name} {element.label}".lower()
    return any(word in text for word in ["corridor", "flur", "hall", "gang"])
