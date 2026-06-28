from __future__ import annotations

import hashlib
import math
import re
from dataclasses import replace
from typing import Iterable

from .models import ImageChunk, PageMetadata, TableChunk, TextChunk
from .providers import VisionProvider


MEASUREMENT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:mm|cm|m|km|in|ft|%|deg|degree|degrees|kg|g|t|kn|n|pa|kpa|mpa|db|hz|v|kv|a|w|kw|mw|l|ml)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
NORMATIVE_RE = re.compile(
    r"\b("
    r"shall|must|required|requirement|minimum|maximum|min|max|at\s+least|at\s+most|not\s+less\s+than|not\s+more\s+than|"
    r"permitted|prohibited|mandatory|compliant|noncompliant|limit|threshold|"
    r"muss|müssen|muessen|soll|sollen|darf|dürfen|duerfen|mindestens|hoechstens|höchstens|"
    r"maximal|minimal|erforderlich|zulaessig|zulässig|unzulässig|unzulässig|anforderung|grenzwert"
    r")\b",
    re.IGNORECASE,
)


def apply_ranked_vision(
    image_chunks: list[ImageChunk],
    text_chunks: list[TextChunk],
    table_chunks: list[TableChunk],
    pages: list[PageMetadata],
    vision_provider: VisionProvider,
) -> tuple[list[ImageChunk], dict]:
    scored = rank_image_chunks(image_chunks, text_chunks, table_chunks, pages, getattr(vision_provider, "max_images", None))
    selected_ids = {item["chunk_id"] for item in scored if item["selected_for_vision"]}
    scored_by_id = {item["chunk_id"]: item for item in scored}
    output: list[ImageChunk] = []
    for chunk in image_chunks:
        ranking = scored_by_id.get(chunk.chunk_id, {})
        metadata = dict(chunk.metadata)
        metadata["vision_ranking"] = ranking
        if chunk.chunk_id not in selected_ids:
            metadata["vision_attempted"] = False
            metadata["vision_execution_status"] = "skipped_by_ranker"
            output.append(replace(chunk, metadata=metadata))
            continue
        vision_text = ""
        errors = list(metadata.get("processing_errors", []))
        try:
            vision_text = _clean_text(vision_provider.describe_image(_path_or_none(chunk.image_path), chunk.page_number, chunk.image_index))
            metadata["vision_execution_status"] = getattr(vision_provider, "last_status", "called")
        except Exception as exc:
            errors.append({"stage": "vision", "error": str(exc)})
            metadata["vision_execution_status"] = "failed"
        metadata["vision_attempted"] = True
        if errors:
            metadata["processing_errors"] = errors
        output.append(replace(chunk, vision_text=vision_text, metadata=metadata))
    audit = {
        "ranker": "generic_evidence_quality_image_ranker",
        "total_images": len(image_chunks),
        "selected_images": len(selected_ids),
        "excluded_images": len(image_chunks) - len(selected_ids),
        "selection_budget": getattr(vision_provider, "max_images", None),
        "scored_images": scored,
    }
    return output, audit


def rank_image_chunks(
    image_chunks: list[ImageChunk],
    text_chunks: list[TextChunk],
    table_chunks: list[TableChunk],
    pages: list[PageMetadata],
    max_selected: int | None = None,
) -> list[dict]:
    page_text = _join_by_page(text_chunks)
    page_tables = _tables_by_page(table_chunks)
    page_meta = {page.page_number: page for page in pages}
    fingerprint_counts = _fingerprint_counts(image_chunks)
    scored = []
    for chunk in image_chunks:
        scores = _score_image(chunk, page_text.get(chunk.page_number, ""), page_tables.get(chunk.page_number, []), page_meta.get(chunk.page_number), fingerprint_counts)
        final_score = round(sum(scores.values()) / max(len(scores), 1), 4)
        scored.append(
            {
                "chunk_id": chunk.chunk_id,
                "page_number": chunk.page_number,
                "individual_scores": scores,
                "final_score": final_score,
                "selection_reason": _selection_reason(scores),
            }
        )
    scored.sort(key=lambda item: (-item["final_score"], item["page_number"], item["chunk_id"]))
    budget = _selection_budget(scored, max_selected)
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
        item["selected_for_vision"] = rank <= budget and item["final_score"] > 0.0
        item["exclusion_reason"] = "" if item["selected_for_vision"] else _exclusion_reason(item, budget)
    return scored


def _score_image(
    chunk: ImageChunk,
    text: str,
    tables: list[TableChunk],
    page: PageMetadata | None,
    fingerprint_counts: dict[str, int],
) -> dict[str, float]:
    ocr = chunk.ocr_text or ""
    combined = " ".join(part for part in [ocr, text] if part)
    page_area = float(page.width * page.height) if page else 0.0
    bbox_area = _bbox_area(chunk.bbox)
    image_area = float(max(chunk.width, 0) * max(chunk.height, 0))
    salience_area = bbox_area if bbox_area > 0 else image_area
    page_ratio = salience_area / page_area if page_area else 0.0
    ocr_len = len(ocr)
    scores = {
        "measurement_density": _clip(len(MEASUREMENT_RE.findall(combined)) / 6.0),
        "numeric_density": _clip(len(NUMBER_RE.findall(combined)) / 16.0),
        "normative_language": _clip(len(NORMATIVE_RE.findall(combined)) / 5.0),
        "ocr_text_quality": _clip(ocr_len / 180.0),
        "page_context_density": _clip((len(MEASUREMENT_RE.findall(text)) + len(NORMATIVE_RE.findall(text))) / 12.0),
        "table_context": _clip((len(tables) + sum(1 for table in tables if MEASUREMENT_RE.search(table.text))) / 4.0),
        "visual_salience": _visual_salience(page_ratio),
        "uniqueness": _uniqueness(chunk, fingerprint_counts),
    }
    return {name: round(value, 4) for name, value in scores.items()}


def _selection_budget(scored: list[dict], max_selected: int | None) -> int:
    if not scored:
        return 0
    positive = sum(1 for item in scored if item["final_score"] > 0.0)
    configured = max_selected if max_selected is not None else 30
    return max(0, min(configured, positive))


def _selection_reason(scores: dict[str, float]) -> str:
    strongest = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:3]
    return ", ".join(f"{name}={value:.2f}" for name, value in strongest)


def _exclusion_reason(item: dict, budget: int) -> str:
    if item["final_score"] <= 0.0:
        return "zero evidence-quality score"
    if item["rank"] > budget:
        return f"rank {item['rank']} below selection budget {budget}"
    return "not selected"


def _join_by_page(chunks: Iterable[TextChunk]) -> dict[int, str]:
    output: dict[int, list[str]] = {}
    for chunk in chunks:
        output.setdefault(chunk.page_number, []).append(chunk.text)
    return {page: " ".join(values) for page, values in output.items()}


def _tables_by_page(chunks: Iterable[TableChunk]) -> dict[int, list[TableChunk]]:
    output: dict[int, list[TableChunk]] = {}
    for chunk in chunks:
        output.setdefault(chunk.page_number, []).append(chunk)
    return output


def _fingerprint_counts(chunks: Iterable[ImageChunk]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        key = _fingerprint(chunk)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _fingerprint(chunk: ImageChunk) -> str:
    seed = f"{chunk.width}x{chunk.height}:{(chunk.ocr_text or '')[:80]}"
    return hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()


def _uniqueness(chunk: ImageChunk, counts: dict[str, int]) -> float:
    count = counts.get(_fingerprint(chunk), 1)
    return round(1.0 / math.sqrt(max(count, 1)), 4)


def _bbox_area(bbox: tuple[float, float, float, float] | None) -> float:
    if not bbox:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _visual_salience(page_ratio: float) -> float:
    if page_ratio <= 0:
        return 0.0
    if page_ratio < 0.005:
        return 0.15
    if page_ratio <= 0.65:
        return _clip(page_ratio / 0.25)
    return 0.55


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _path_or_none(value: str | None):
    if not value:
        return None
    from pathlib import Path

    return Path(value)
