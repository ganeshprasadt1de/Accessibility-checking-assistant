from __future__ import annotations


FALLBACKS = {
    "door_width": "door too narrow",
    "corridor_width": "corridor too narrow",
    "route_width": "route too narrow",
    "turning_space": "turning space too small",
    "stair_block": "stair blocks route",
    "ramp_slope": "ramp too steep",
    "ramp_width": "ramp too narrow",
    "missing": "data is missing",
    "unreachable": "route not connected",
}


def fallback(rule_id: str) -> str:
    return FALLBACKS.get(rule_id, "access issue found")


def explain_short(facts: dict) -> str:
    return fallback(str(facts.get("rule_id", "")))
