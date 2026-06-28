from __future__ import annotations

import re
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Protocol

from .models import ParsedPdfPage, TableChunk, TextChunk


class PDFParser(Protocol):
    name: str

    def parse(self, pdf_path: Path) -> tuple[dict[str, Any], list[ParsedPdfPage], Any]:
        ...


class PyMuPDFParser:
    name = "pymupdf"

    def parse(self, pdf_path: Path) -> tuple[dict[str, Any], list[ParsedPdfPage], Any]:
        try:
            import fitz
        except Exception as exc:
            raise RuntimeError(f"PyMuPDF is required for PDF parsing: {exc}") from exc

        doc = fitz.open(pdf_path)
        metadata = {key: value for key, value in (doc.metadata or {}).items() if value}
        metadata.update({"page_count": doc.page_count, "pdf_parser": self.name})
        pages: list[ParsedPdfPage] = []
        for page_index, page in enumerate(doc, start=1):
            page_text = _clean_text(page.get_text("text"))
            rect = page.rect
            pages.append(
                ParsedPdfPage(
                    page_number=page_index,
                    width=round(float(rect.width), 4),
                    height=round(float(rect.height), 4),
                    rotation=int(page.rotation or 0),
                    text=page_text,
                    text_blocks=_text_chunks(page, page_index),
                    table_chunks=_table_chunks(page, page_index, page_text),
                    raw_page=page,
                )
            )
        return metadata, pages, doc


def _text_chunks(page: Any, page_number: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    for index, block in enumerate(page.get_text("blocks"), start=1):
        if len(block) < 5:
            continue
        text = _clean_text(str(block[4]))
        if not text:
            continue
        bbox = tuple(round(float(value), 4) for value in block[:4])
        chunks.append(TextChunk(chunk_id=f"p{page_number:03d}-text-{index:03d}", page_number=page_number, text=text, bbox=bbox))
    if not chunks and page.get_text("text").strip():
        chunks.append(TextChunk(chunk_id=f"p{page_number:03d}-text-001", page_number=page_number, text=_clean_text(page.get_text("text"))))
    return chunks


def _table_chunks(page: Any, page_number: int, page_text: str) -> list[TableChunk]:
    chunks = _pymupdf_table_chunks(page, page_number)
    if chunks:
        return chunks
    return _heuristic_table_chunks(page_number, page_text)


def _pymupdf_table_chunks(page: Any, page_number: int) -> list[TableChunk]:
    if not hasattr(page, "find_tables"):
        return []
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            tables = page.find_tables()
    except Exception:
        return []
    chunks: list[TableChunk] = []
    for index, table in enumerate(getattr(tables, "tables", []) or [], start=1):
        rows = table.extract() or []
        text = "\n".join(" | ".join(_clean_text(str(cell or "")) for cell in row) for row in rows)
        if not text.strip():
            continue
        bbox = tuple(round(float(value), 4) for value in table.bbox) if getattr(table, "bbox", None) else None
        column_count = max((len(row) for row in rows), default=0)
        chunks.append(
            TableChunk(
                chunk_id=f"p{page_number:03d}-table-{index:03d}",
                page_number=page_number,
                text=text,
                row_count=len(rows),
                column_count=column_count,
                extraction_method="pymupdf_find_tables",
                bbox=bbox,
            )
        )
    return chunks


def _heuristic_table_chunks(page_number: int, page_text: str) -> list[TableChunk]:
    lines = [line.strip() for line in re.split(r"(?<=[.;])\s+|\n+", page_text) if line.strip()]
    table_like = [line for line in lines if len(re.findall(r"\d+(?:,\d+)?\s*(?:cm|m|%)", line.lower())) >= 2]
    if not table_like:
        return []
    return [
        TableChunk(
            chunk_id=f"p{page_number:03d}-table-001",
            page_number=page_number,
            text="\n".join(table_like),
            row_count=len(table_like),
            column_count=0,
            extraction_method="measurement_line_heuristic",
        )
    ]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
