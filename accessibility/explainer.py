import re

import requests

from accessibility.config import OLLAMA_MODEL, OLLAMA_URL
from accessibility.issue_sections import fix_text, issue_section
from accessibility.model import Issue


def explain_issue(issue: Issue, use_llm: bool) -> str:
    simple = _simple_explanation(issue)
    if not use_llm:
        return simple

    prompt = _prompt(issue)
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=30,
        )
        response.raise_for_status()
        text = _clean_llm_text(response.json().get("response", ""))
        return text or simple
    except requests.RequestException:
        return simple + " Local LLM explanation was skipped because Ollama is not running."


def _simple_explanation(issue: Issue) -> str:
    if issue.value == "missing":
        return (
            f"{issue.element_name} is missing the value needed for the {issue.rule} check. "
            f"The required value is {issue.required}."
        )
    return (
        f"{issue.element_name} does not pass the {issue.rule} check. "
        f"The current value is {issue.value}. The required value is {issue.required}."
    )


def _prompt(issue: Issue) -> str:
    return f"""
Explain this accessibility issue in simple language.

Element: {issue.element_name}
Element type: {issue.element_kind}
Rule: {issue.rule}
Current value: {issue.value}
Required value: {issue.required}

Give one short explanation and one practical recommendation.
Do not invent legal details.
"""


def answer_question(
    issues: list[Issue],
    question: str,
    use_llm: bool,
    extra_context: str = "",
) -> str:
    if not issues and not extra_context:
        return "No accessibility issue was found for the selected checks."

    context = _issue_context(issues)
    if extra_context:
        if context:
            context = context + "\n\nAdditional checked data:\n" + extra_context
        else:
            context = extra_context
    if _asks_for_count(question):
        return _count_answer(issues)

    if not use_llm:
        return (
            "The assistant can answer with the checked issue list. "
            "For a full language-model answer, start Ollama and select the local LLM option.\n\n"
            + context
        )

    prompt = f"""
/no_think
Answer the user's question using only the checked project data below.

Checked project data:
{context}

Question:
{question}

Keep the answer short and practical.
Do not invent rules that are not listed in the issues.
If the user asks how one design change affects connected elements, explain the fixed-building-size tradeoff from the design-impact data.
Do not use LaTeX, boxed notation, or hidden reasoning.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=30,
        )
        response.raise_for_status()
        text = _clean_llm_text(response.json().get("response", ""))
        return text or context
    except requests.RequestException:
        return (
            "Ollama is not running, so the local LLM answer was skipped. "
            "Here are the checked issues:\n\n"
            + context
        )


def _issue_context(issues: list[Issue]) -> str:
    rows = []
    for issue in issues:
        rows.append(
            f"{issue_section(issue)} | {issue.element_name}: {issue.rule}. "
            f"Current value: {issue.value}. Required value: {issue.required}. "
            f"Fix: {fix_text(issue)}"
        )
    return "\n".join(rows)


def _asks_for_count(question: str) -> bool:
    text = question.lower()
    count_words = ["how many", "total", "number of", "count"]
    issue_words = ["issue", "problem", "violation"]
    return any(word in text for word in count_words) and any(word in text for word in issue_words)


def _count_answer(issues: list[Issue]) -> str:
    lines = [f"There are {len(issues)} accessibility issues in the current check."]
    by_rule: dict[str, int] = {}
    for issue in issues:
        by_rule[issue.rule] = by_rule.get(issue.rule, 0) + 1

    lines.append("")
    lines.append("Issue count by rule:")
    for rule, count in sorted(by_rule.items()):
        lines.append(f"- {rule}: {count}")
    return "\n".join(lines)


def _clean_llm_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"(?is)^.*?done thinking\\.?\\s*", "", cleaned)
    cleaned = cleaned.replace("\\boxed{", "").replace("}", "")
    cleaned = cleaned.replace("\\boxed", "")
    cleaned = re.sub(r"(?im)^answer:\\s*", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
