from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from .canonical_rules import CanonicalRule
from .document_intelligence.models import ImageChunk, StructuredDocument, structured_document_from_dict
from .document_intelligence.providers import ProviderConfig
from .llm_provider import OllamaLLMProvider


DOCUMENT_CACHE_VERSION = "document-cache-v1"
EXTRACTION_CACHE_VERSION = "extraction-cache-v1"


class ProcessingCache:
    def __init__(self, cache_root: Path) -> None:
        self.cache_root = cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def document_key(self, pdf_path: Path, provider_config: ProviderConfig | None = None) -> str:
        config = provider_config or ProviderConfig()
        identity = {
            "version": DOCUMENT_CACHE_VERSION,
            "pdf_sha256": file_sha256(pdf_path),
            "pdf_size": pdf_path.stat().st_size,
            "parser": "pymupdf",
            "image_extractor": "pymupdf_image_extractor",
            "ocr_provider": "paddleocr",
            "vision_provider": f"ollama_{config.vision_model}",
            "max_vision_images": config.max_vision_images,
        }
        return _stable_digest(identity)

    def load_document(self, key: str, pdf_path: Path, image_output_dir: Path) -> StructuredDocument | None:
        entry = self.cache_root / "documents" / key
        manifest_path = entry / "manifest.json"
        document_path = entry / "document_chunks.json"
        images_dir = entry / "document_images"
        if not manifest_path.exists() or not document_path.exists() or not images_dir.exists():
            return None
        manifest = _read_json(manifest_path)
        if manifest.get("pdf_sha256") != file_sha256(pdf_path):
            return None
        if image_output_dir.exists():
            shutil.rmtree(image_output_dir)
        shutil.copytree(images_dir, image_output_dir)
        document = structured_document_from_dict(_read_json(document_path), source_pdf=str(pdf_path))
        return _with_image_output_paths(document, image_output_dir)

    def save_document(self, key: str, pdf_path: Path, document: StructuredDocument, image_output_dir: Path) -> None:
        entry = self.cache_root / "documents" / key
        temp_entry = entry.with_name(f"{entry.name}.tmp")
        if temp_entry.exists():
            shutil.rmtree(temp_entry)
        temp_entry.mkdir(parents=True, exist_ok=True)
        images_cache = temp_entry / "document_images"
        if image_output_dir.exists():
            shutil.copytree(image_output_dir, images_cache)
        else:
            images_cache.mkdir(parents=True, exist_ok=True)
        (temp_entry / "document_chunks.json").write_text(
            json.dumps(document.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            "cache_type": "document_intelligence",
            "version": DOCUMENT_CACHE_VERSION,
            "cache_key": key,
            "pdf": str(pdf_path),
            "pdf_sha256": file_sha256(pdf_path),
            "pdf_size": pdf_path.stat().st_size,
            "chunk_counts": document.to_dict().get("chunk_counts", {}),
            "metadata": document.metadata,
        }
        (temp_entry / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        _replace_directory(temp_entry, entry)

    def extraction_key(self, document_key: str, llm: OllamaLLMProvider, schema_path: Path) -> str:
        identity = {
            "version": EXTRACTION_CACHE_VERSION,
            "document_key": document_key,
            "llm_provider": llm.name,
            "canonical_schema_sha256": file_sha256(schema_path) if schema_path.exists() else "",
        }
        return _stable_digest(identity)

    def load_extraction(self, key: str, agent: Any) -> list[CanonicalRule] | None:
        entry = self.cache_root / "extractions" / key
        payload_path = entry / "extraction_cache.json"
        if not payload_path.exists():
            return None
        payload = _read_json(payload_path)
        if payload.get("version") != EXTRACTION_CACHE_VERSION:
            return None
        agent.metrics = dict(payload.get("metrics") or {})
        agent.audit_events = list(payload.get("audit_events") or [])
        agent.retrieval_audit = dict(payload.get("retrieval_audit") or {})
        agent.governance_audit = dict(payload.get("governance_audit") or {})
        agent._candidate_counter = int(payload.get("candidate_counter") or 0)
        return [CanonicalRule(**item) for item in payload.get("rules", [])]

    def save_extraction(self, key: str, agent: Any, rules: list[CanonicalRule]) -> None:
        entry = self.cache_root / "extractions" / key
        temp_entry = entry.with_name(f"{entry.name}.tmp")
        if temp_entry.exists():
            shutil.rmtree(temp_entry)
        temp_entry.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_type": "requirement_extraction",
            "version": EXTRACTION_CACHE_VERSION,
            "cache_key": key,
            "llm_provider": getattr(agent.llm, "name", ""),
            "metrics": agent.metrics,
            "audit_events": agent.audit_events,
            "retrieval_audit": agent.retrieval_audit,
            "governance_audit": agent.governance_audit,
            "candidate_counter": getattr(agent, "_candidate_counter", 0),
            "rules": [rule.to_dict() for rule in rules],
        }
        (temp_entry / "extraction_cache.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        _replace_directory(temp_entry, entry)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_digest(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _replace_directory(temp_entry: Path, entry: Path) -> None:
    if entry.exists():
        shutil.rmtree(entry)
    temp_entry.rename(entry)


def _with_image_output_paths(document: StructuredDocument, image_output_dir: Path) -> StructuredDocument:
    image_chunks: list[ImageChunk] = []
    for chunk in document.image_chunks:
        if not chunk.image_path:
            image_chunks.append(chunk)
            continue
        image_name = Path(chunk.image_path).name
        image_chunks.append(replace(chunk, image_path=str(image_output_dir / image_name)))
    return replace(document, image_chunks=image_chunks)
