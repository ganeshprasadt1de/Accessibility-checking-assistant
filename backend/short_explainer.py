from __future__ import annotations

import json
import urllib.request


OLLAMA_GENERATE_TIMEOUT_SECONDS = 90

RULE_LABELS = {
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


def rule_label(rule_id: str) -> str:
    return RULE_LABELS.get(rule_id, "access issue found")


def explain_short(facts: dict) -> str:
    return rule_label(str(facts.get("rule_id", "")))


def explain_question(question: str, context: dict, model: str = "qwen3:8b", host: str = "http://localhost:11434") -> dict:
    prompt = (
        "Explain this wheelchair route checker result in simple language for a 16 year old. "
        "Use only the facts provided. Do not invent measurements. Do not claim legal approval. "
        "First explain the SHACL accessibility result. Then give 2 to 4 short architect-focused improvement suggestions. "
        "The suggestions must name the affected elements, floors, or failed routes from the facts when possible. "
        "Do not give generic checklist advice. "
        "Base suggestions only on detectedIssueTypes, affectedElements, failedRoutes, and floorsWithFailures. "
        "Do not suggest door, corridor, ramp, or other fixes unless those issue types appear in detectedIssueTypes, except that ramps or lifts may be suggested as alternatives for stair blockers. "
        "Do not mention unaffected issue types. "
        "Keep the whole answer around 80 to 120 words.\n\n"
        f"Question: {question}\n\n"
        "Facts:\n"
        + json.dumps(context, ensure_ascii=True)
    )
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_GENERATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        raise RuntimeError(f"Ollama request timed out after {OLLAMA_GENERATE_TIMEOUT_SECONDS} seconds.") from exc
    text = str(data.get("response", "")).strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return {"answer": text, "source": f"Ollama {model}"}
