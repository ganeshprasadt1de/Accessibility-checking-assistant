from __future__ import annotations

from .config import RULE_LIMITS
from .model import Element, Issue
from .short_explainer import rule_label


def make_issue(
    issues: list[Issue],
    element: Element,
    rule_id: str,
    measured: float | None,
    required: float | None,
    unit: str,
    source: str,
    details: str,
    evidence_id: str | None = None,
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
            short_text=rule_label(rule_id),
            details=details,
            evidence_id=evidence_id,
        )
    )


def evaluate_value_rules(elements: list[Element]) -> list[Issue]:
    issues: list[Issue] = []
    for element in elements:
        if element.ifc_type == "IfcDoor":
            width = _num(element.extra.get("derivedDoorWidthM"))
            height = _num(element.extra.get("derivedDoorHeightM"))
            if width is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.door_width_m, "m", "IFC model data", "Door width could not be calculated.")
            elif width < RULE_LIMITS.door_width_m:
                make_issue(issues, element, "door_width", width, RULE_LIMITS.door_width_m, "m", "IFC model geometry", "Door clear width is below the rule target.")
            if height is None:
                make_issue(issues, element, "missing_door_height", None, RULE_LIMITS.door_height_m, "m", "IFC model data", "Door clear height could not be calculated.")
            elif height < RULE_LIMITS.door_height_m:
                make_issue(issues, element, "door_height", height, RULE_LIMITS.door_height_m, "m", "IFC model geometry", "Door clear height is below the rule target.")
        elif element.ifc_type == "IfcSpace":
            if _skip_space_rules(element):
                continue
            clear = _num(element.extra.get("derivedClearSpaceWidthM"))
            if clear is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.corridor_width_m, "m", "IFC model data", "Space clear width could not be calculated.")
            elif _looks_like_corridor(element) and clear < RULE_LIMITS.corridor_width_m:
                make_issue(issues, element, "corridor_width", clear, RULE_LIMITS.corridor_width_m, "m", "IFC model geometry", "Corridor clear width is below the rule target.")
            if _looks_like_corridor(element):
                length = _num(element.extra.get("derivedCorridorLengthM"))
                slope = _num(element.extra.get("derivedCorridorSlopePercent"))
                slope_limit = (
                    RULE_LIMITS.short_corridor_slope_percent
                    if length is not None and length <= RULE_LIMITS.short_corridor_length_m
                    else RULE_LIMITS.corridor_slope_percent
                )
                if slope is not None and slope > slope_limit:
                    make_issue(issues, element, "corridor_slope", slope, slope_limit, "%", "IFC model geometry", "Corridor slope is above the rule target.")
                for evidence in element.passing_area_gaps:
                    gap = _num(evidence.get("measured"))
                    if gap is None or gap <= RULE_LIMITS.corridor_movement_interval_m:
                        continue
                    make_issue(
                        issues,
                        element,
                        "corridor_movement_area",
                        gap,
                        RULE_LIMITS.corridor_movement_interval_m,
                        "m",
                        "IFC model geometry",
                        "Passing-area spacing exceeds the rule interval.",
                        evidence.get("evidence_id"),
                    )
            turn = _num(element.extra.get("turningSpaceM"))
            if turn is not None and turn + 0.005 < RULE_LIMITS.turning_space_m:
                make_issue(issues, element, "turning_space", turn, RULE_LIMITS.turning_space_m, "m", "IFC model geometry", "Turning space is below the rule target.")
        elif element.ifc_type in {"IfcRamp", "IfcRampFlight"}:
            slope = _num(element.extra.get("rampSlopePercent"))
            width = _num(element.extra.get("rampUsableWidthM"))
            run_length = _num(element.extra.get("rampRunLengthM"))
            if slope is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.ramp_slope_percent, "%", "IFC model data", "Ramp slope could not be calculated.")
            elif slope > RULE_LIMITS.ramp_slope_percent:
                make_issue(issues, element, "ramp_slope", slope, RULE_LIMITS.ramp_slope_percent, "%", "IFC model geometry", "Ramp slope is above the rule target.")
            if width is None:
                make_issue(issues, element, "missing", None, RULE_LIMITS.ramp_width_m, "m", "IFC model data", "Ramp width could not be calculated.")
            elif width < RULE_LIMITS.ramp_width_m:
                make_issue(issues, element, "ramp_width", width, RULE_LIMITS.ramp_width_m, "m", "IFC model geometry", "Ramp width is below the rule target.")
            if run_length is not None and run_length > RULE_LIMITS.ramp_run_length_m:
                make_issue(issues, element, "ramp_run_length", run_length, RULE_LIMITS.ramp_run_length_m, "m", "IFC model geometry", "Ramp flight length is above the rule target.")
    return issues


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_corridor(element: Element) -> bool:
    text = f"{element.name} {element.label}".lower()
    return any(word in text for word in ["corridor", "flur", "hall", "gang"])


def _skip_space_rules(element: Element) -> bool:
    text = f"{element.name} {element.label}".lower()
    return any(word in text for word in ["chase", "shaft", "gfa", "volume", "vent", "heating"])
