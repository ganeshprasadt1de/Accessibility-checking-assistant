from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PageMetadata:
    page_number: int
    width: float
    height: float
    rotation: int
    text_length: int
    image_count: int
    table_count: int


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    page_number: int
    text: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ImageChunk:
    chunk_id: str
    page_number: int
    image_index: int
    width: int
    height: int
    extension: str
    image_path: str | None
    bbox: tuple[float, float, float, float] | None = None
    ocr_text: str = ""
    vision_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TableChunk:
    chunk_id: str
    page_number: int
    text: str
    row_count: int
    column_count: int
    extraction_method: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ParsedPdfPage:
    page_number: int
    width: float
    height: float
    rotation: int
    text: str
    text_blocks: list[TextChunk]
    table_chunks: list[TableChunk]
    raw_page: Any


@dataclass(frozen=True)
class StructuredDocument:
    source_pdf: str
    metadata: dict[str, Any]
    pages: list[PageMetadata]
    text_chunks: list[TextChunk]
    image_chunks: list[ImageChunk]
    table_chunks: list[TableChunk]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "metadata": self.metadata,
            "pages": [asdict(page) for page in self.pages],
            "text_chunks": [asdict(chunk) for chunk in self.text_chunks],
            "image_chunks": [asdict(chunk) for chunk in self.image_chunks],
            "table_chunks": [asdict(chunk) for chunk in self.table_chunks],
            "chunk_counts": {
                "pages": len(self.pages),
                "text": len(self.text_chunks),
                "images": len(self.image_chunks),
                "tables": len(self.table_chunks),
            },
        }


def structured_document_from_dict(data: dict[str, Any], source_pdf: str | None = None) -> StructuredDocument:
    return StructuredDocument(
        source_pdf=source_pdf or str(data.get("source_pdf") or ""),
        metadata=dict(data.get("metadata") or {}),
        pages=[PageMetadata(**item) for item in data.get("pages", [])],
        text_chunks=[
            TextChunk(
                chunk_id=str(item.get("chunk_id") or ""),
                page_number=int(item.get("page_number") or 0),
                text=str(item.get("text") or ""),
                bbox=_tuple_or_none(item.get("bbox")),
            )
            for item in data.get("text_chunks", [])
        ],
        image_chunks=[
            ImageChunk(
                chunk_id=str(item.get("chunk_id") or ""),
                page_number=int(item.get("page_number") or 0),
                image_index=int(item.get("image_index") or 0),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                extension=str(item.get("extension") or ""),
                image_path=item.get("image_path"),
                bbox=_tuple_or_none(item.get("bbox")),
                ocr_text=str(item.get("ocr_text") or ""),
                vision_text=str(item.get("vision_text") or ""),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in data.get("image_chunks", [])
        ],
        table_chunks=[
            TableChunk(
                chunk_id=str(item.get("chunk_id") or ""),
                page_number=int(item.get("page_number") or 0),
                text=str(item.get("text") or ""),
                row_count=int(item.get("row_count") or 0),
                column_count=int(item.get("column_count") or 0),
                extraction_method=str(item.get("extraction_method") or ""),
                bbox=_tuple_or_none(item.get("bbox")),
            )
            for item in data.get("table_chunks", [])
        ],
    )


def _tuple_or_none(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
