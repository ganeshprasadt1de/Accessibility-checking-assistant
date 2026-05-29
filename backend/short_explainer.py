from __future__ import annotations

import json
import urllib.error
import urllib.request


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


def explain_question(question: str, context: dict, model: str = "qwen3:8b", host: str = "http://localhost:11434") -> dict:
    fallback_text = deterministic_answer(question, context)
    prompt = (
        "Explain this wheelchair route checker result in simple language for a 16 year old. "
        "Use only the facts provided. Do not invent measurements. Do not claim legal approval. "
        "Keep it under 130 words.\n\n"
        f"Question: {question}\n\n"
        "Facts:\n"
        + json.dumps(context, ensure_ascii=True)
    )
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        if text:
            return {"answer": text, "source": f"Ollama {model}"}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass
    return {"answer": fallback_text, "source": "Prepared checker facts"}


def deterministic_answer(question: str, context: dict) -> str:
    summary = context.get("summary", {})
    floors = context.get("floors", [])
    rules = context.get("rules", {})
    issue_count = int(summary.get("issueCount") or 0)
    failed_edges = sum(int(floor.get("failedRouteEdges") or 0) for floor in floors)
    floor_text = ", ".join(
        f"{floor['name']}: {floor['doors']} doors, {floor['routeEdges']} routes, {floor['failedRouteEdges']} failed"
        for floor in floors
    )
    if issue_count == 0 and failed_edges == 0:
        result = "All generated indoor routes pass the current prototype checks."
    else:
        result = f"The checker found {issue_count} issues and {failed_edges} failed route edges."
    return (
        f"{result} The model has {summary.get('doorCount')} doors and {summary.get('routeEdgeCount')} route edges. "
        f"By floor: {floor_text}. "
        "The prototype checks door width, route width, turning space, stair blockers, and ramp width or slope. "
        f"The main rule values are {rules.get('door_width_m')} m door width, {rules.get('corridor_width_m')} m route width, "
        f"{rules.get('turning_space_m')} m turning space, {rules.get('ramp_width_m')} m ramp width, and {rules.get('ramp_slope_percent')} percent ramp slope."
    )
