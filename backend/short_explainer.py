from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


OLLAMA_GENERATE_TIMEOUT_SECONDS = 90

RULE_LABELS = {
    "door_width": "door too narrow",
    "door_height": "door too low",
    "corridor_width": "corridor too narrow",
    "corridor_slope": "corridor too steep",
    "corridor_movement_area": "passing areas spaced too far apart",
    "route_width": "route too narrow",
    "turning_space": "turning space too small",
    "stair_block": "stair blocks route",
    "ramp_slope": "ramp too steep",
    "ramp_width": "ramp too narrow",
    "ramp_run_length": "ramp flight too long",
    "missing": "missing data",
    "unreachable": "route not connected",
}

SUPPORTED_RULES = frozenset(RULE_LABELS)
RULE_ALIASES = {
    "missing_door_width": "missing",
    "missing_door_height": "missing",
    "route_door_width": "door_width",
    "route_door_height": "door_height",
    "route_turning_space": "turning_space",
    "route_ramp_slope": "ramp_slope",
    "route_ramp_width": "ramp_width",
    "route_ramp_run_length": "ramp_run_length",
}

POINT_ROUTE_EXPLANATIONS = {
    "start_not_walkable": "The selected start point is not a walkable grid cell, so a collision-free route cannot begin there.",
    "destination_not_walkable": "The destination is outside the accessible walking area or inside an obstacle. The red candidate ends at the last collision-free point before that blocked area.",
    "no_accessible_connection": "The start and destination belong to disconnected walkable regions. The red candidate stops before the first blocking boundary and does not claim to reach the destination.",
}


def rule_label(rule_id: str) -> str:
    return RULE_LABELS.get(rule_id, "access issue found")


def explain_short(facts: dict) -> str:
    return rule_label(str(facts.get("rule_id", "")))


def _clean(value: object, fallback: str = "unnamed element") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _issue_types(context: dict) -> list[str]:
    result = []
    for item in context.get("detectedIssueTypes", []):
        rule = RULE_ALIASES.get(str(item), str(item))
        if rule in SUPPORTED_RULES and rule not in result:
            result.append(rule)
    return result


def _names_for_rule(context: dict, rule: str, limit: int = 3) -> list[str]:
    names = []
    for item in context.get("affectedElements", []):
        if RULE_ALIASES.get(str(item.get("rule")), str(item.get("rule"))) != rule:
            continue
        name = _clean(item.get("name"))
        if name not in names:
            names.append(name)
    return names[:limit]


def _failed_routes_for_rule(context: dict, rule: str, limit: int = 4) -> list[dict]:
    result = []
    for route in context.get("failedRoutes", []):
        if rule not in route.get("reasons", []):
            continue
        result.append(route)
    return result[:limit]


def _allowed_actions(context: dict) -> list[dict]:
    """Build recommendations from checked facts, not from model imagination."""
    actions = []
    for rule in _issue_types(context):
        names = _names_for_rule(context, rule)
        routes = _failed_routes_for_rule(context, rule)
        name_text = ", ".join(names)
        route = routes[0] if routes else {}
        edge = _clean(route.get("edgeId"), "the failed route")
        floor = _clean(route.get("floor"), "the affected floor")
        start = _clean(route.get("from"), "its start door")
        end = _clean(route.get("to"), "its end door")

        if rule == "stair_block":
            text = f"Reroute {edge} on {floor} from {start} to {end} so its polyline does not intersect the recorded stair geometry."
        elif rule == "door_width":
            target = name_text or start
            text = f"Increase the clear opening at {target}, then regenerate the route measurements and rerun SHACL."
        elif rule == "door_height":
            target = name_text or start
            text = f"Increase the clear opening height at {target}, then regenerate the route measurements and rerun SHACL."
        elif rule == "corridor_width":
            target = name_text or floor
            text = f"Increase the measured clear width at {target}, then regenerate the package and rerun SHACL."
        elif rule == "corridor_slope":
            target = name_text or floor
            text = f"Reduce the measured corridor slope at {target}, then regenerate the package and rerun SHACL."
        elif rule == "corridor_movement_area":
            target = name_text or floor
            text = f"Provide a 1.80 m by 1.80 m passing area within each 15.00 m interval at {target}, then regenerate the package and rerun SHACL."
        elif rule == "route_width":
            text = f"Increase the measured clear width available along {edge} on {floor}, then regenerate the route measurement and rerun SHACL."
        elif rule == "turning_space":
            text = f"Increase the clear turning area on {edge} on {floor}, then recompute the route before checking it again."
        elif rule == "ramp_slope":
            target = name_text or edge
            text = f"Reduce the measured slope of {target}, then regenerate its ramp measurement and rerun SHACL."
        elif rule == "ramp_width":
            target = name_text or edge
            text = f"Increase the measured usable width of {target}, then regenerate its ramp measurement and rerun SHACL."
        elif rule == "ramp_run_length":
            target = name_text or edge
            text = f"Reduce the uninterrupted ramp flight length at {target}, then regenerate its ramp measurement and rerun SHACL."
        elif rule == "missing":
            target = name_text or "the affected IFC elements"
            text = f"Add or repair the missing IFC geometry or property data for {target}, then preprocess the model again."
        elif rule == "unreachable":
            text = f"Check the IFC space-boundary and door relationships for {edge} on {floor}, then rebuild the door graph."
        else:
            continue
        actions.append({"id": f"action_{len(actions) + 1}", "rule": rule, "text": text})
    return actions


def _summary(context: dict) -> str:
    types = _issue_types(context)
    if not types:
        return "The supplied SHACL facts contain no detected accessibility issue type."
    raw_counts = context.get("issueCountsByType", {})
    counts = {rule: 0 for rule in types}
    for raw_rule, value in raw_counts.items():
        rule = RULE_ALIASES.get(str(raw_rule), str(raw_rule))
        if rule in counts:
            counts[rule] += int(value or 0)
    parts = []
    for rule in types:
        count = int(counts.get(rule, 0) or 0)
        label = rule_label(rule)
        parts.append(f"{count} {label} issue" + ("s" if count != 1 else ""))
    floors = [_clean(item.get("name"), "unnamed floor") for item in context.get("floorsWithFailures", [])]
    floor_text = f" Affected floors: {', '.join(dict.fromkeys(floors))}." if floors else ""
    return "The SHACL check found " + ", ".join(parts) + "." + floor_text


def _response_schema(action_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "evidenceReview": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "issueType": {"type": "string"},
                        "evidence": {"type": "string", "maxLength": 160},
                        "selectedActionId": {"type": "string", "enum": action_ids},
                    },
                    "required": ["issueType", "evidence", "selectedActionId"],
                },
            },
            "selectedActionIds": {
                "type": "array",
                "items": {"type": "string", "enum": action_ids},
                "maxItems": 4,
            },
        },
        "required": ["evidenceReview", "selectedActionIds"],
    }


def _select_actions(model_data: dict, actions: list[dict]) -> list[dict]:
    allowed = {item["id"]: item for item in actions}
    selected = []
    for action_id in model_data.get("selectedActionIds", []):
        item = allowed.get(str(action_id))
        if item and item not in selected:
            selected.append(item)
    return selected[:4] or actions[:4]


def _is_general_improvement_question(question: str) -> bool:
    text = _clean(question, "").lower()
    broad_terms = ("fix the building", "fix this building", "ways i can fix", "ways to fix", "all issues", "all problems", "improve the building")
    return any(term in text for term in broad_terms)


def _validate_evidence_review(model_data: dict, context: dict, actions: list[dict]) -> None:
    rules = set(_issue_types(context))
    actions_by_id = {item["id"]: item for item in actions}
    reviews = model_data.get("evidenceReview")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("Ollama did not return an evidence review.")
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Ollama returned an invalid evidence review item.")
        rule = str(review.get("issueType", ""))
        action = actions_by_id.get(str(review.get("selectedActionId", "")))
        if rule not in rules or not action or action["rule"] != rule:
            raise ValueError("Ollama selected an action that is not supported by the SHACL facts.")


def explain_question(question: str, context: dict, model: str = "qwen3:8b", host: str = "http://localhost:11434") -> dict:
    actions = _allowed_actions(context)
    if not actions:
        summary = _summary(context)
        return {"answer": summary, "source": "SHACL validation report", "blocks": [{"type": "paragraph", "text": summary}]}

    action_ids = [item["id"] for item in actions]
    prompt = (
        "You are selecting grounded recommendations for a wheelchair route checker. "
        "Reason through the supplied evidence before selecting actions. Return JSON only. "
        "For every selected action, add an evidenceReview item that links one detected issueType to one allowed action ID. "
        "Never create a new action, building element, floor, measurement, route, cause, or legal claim. "
        "A stair blocker permits only rerouting around the recorded stair geometry. It does not permit suggestions about lifts, elevators, ramps, or slopes. "
        "Ramp advice is allowed only for ramp_slope, ramp_width, or ramp_run_length facts. Door advice is allowed only for door_width or door_height. "
        "Select the actions that directly answer the question. Select no more than four.\n\n"
        f"Question: {_clean(question, 'Explain the checker result.')}\n\n"
        "Detected facts:\n" + json.dumps(context, ensure_ascii=True, separators=(",", ":")) + "\n\n"
        "Allowed actions:\n" + json.dumps(actions, ensure_ascii=True, separators=(",", ":"))
    )
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": _response_schema(action_ids),
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 700},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_GENERATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Ollama request failed or timed out after {OLLAMA_GENERATE_TIMEOUT_SECONDS} seconds.") from exc

    raw = str(data.get("response", "")).strip()
    used_fallback = False
    try:
        model_data = json.loads(raw)
        _validate_evidence_review(model_data, context, actions)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        model_data = {"selectedActionIds": [item["id"] for item in actions[:4]]}
        used_fallback = True

    selected = _select_actions(model_data, actions)
    if _is_general_improvement_question(question):
        for item in actions:
            if item not in selected:
                selected.append(item)
            if len(selected) >= 4:
                break
    summary = _summary(context)
    blocks = [
        {"type": "paragraph", "text": summary},
        {"type": "heading", "text": "Recommended changes"},
        {"type": "list", "items": [item["text"] for item in selected]},
    ]
    answer = summary + "\n\nRecommended changes\n\n" + "\n".join(f"- {item['text']}" for item in selected)
    source = "SHACL-grounded fallback; incomplete Ollama output was rejected" if used_fallback else f"Ollama {model}, grounded by SHACL facts"
    return {"answer": answer, "source": source, "blocks": blocks, "groundedFallback": used_fallback}


def explain_point_route(reason: str, model: str = "qwen3:8b", host: str = "http://localhost:11434") -> dict:
    text = POINT_ROUTE_EXPLANATIONS.get(reason)
    if not text:
        raise ValueError("The point-route reason is not supported.")
    schema = {
        "type": "object",
        "properties": {"acceptedReason": {"type": "string", "enum": [reason]}},
        "required": ["acceptedReason"],
    }
    prompt = (
        "Review one deterministic wheelchair-routing result. Return JSON only. "
        "Accept the supplied reason only when the fixed explanation states the same cause. "
        "Do not add a building element, measurement, route, remedy, law, or alternative cause.\n\n"
        f"Reason ID: {reason}\nFixed explanation: {text}"
    )
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 80},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_GENERATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        reviewed = json.loads(str(data.get("response", "")))
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Ollama did not return a valid grounded point-route review.") from exc
    if reviewed.get("acceptedReason") != reason:
        raise RuntimeError("Ollama returned a reason that does not match the deterministic route result.")
    return {"text": text, "source": f"Ollama {model}, restricted to deterministic point-route facts", "reason": reason, "llmReviewed": True}
