from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from .models import ImageChunk, ParsedPdfPage
from .providers import OCRProvider, VisionProvider


class ImageExtractor(Protocol):
    name: str

    def extract(
        self,
        doc: Any,
        page: ParsedPdfPage,
        output_dir: Path | None,
        ocr_provider: OCRProvider,
        vision_provider: VisionProvider,
    ) -> list[ImageChunk]:
        ...


class PyMuPDFImageExtractor:
    name = "pymupdf_image_extractor"
    render_zoom = 2.0

    def extract(
        self,
        doc: Any,
        page: ParsedPdfPage,
        output_dir: Path | None,
        ocr_provider: OCRProvider,
        vision_provider: VisionProvider,
    ) -> list[ImageChunk]:
        chunks: list[ImageChunk] = []
        seen_xrefs: set[int] = set()
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
        image_index = 0
        for image in page.raw_page.get_images(full=True):
            image_index += 1
            xref = int(image[0])
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            extracted = doc.extract_image(xref)
            extension = str(extracted.get("ext") or "bin")
            width = int(extracted.get("width") or image[2] or 0)
            height = int(extracted.get("height") or image[3] or 0)
            image_path: Path | None = None
            if output_dir and extracted.get("image"):
                image_path = output_dir / f"page-{page.page_number:03d}-image-{image_index:03d}.{extension}"
                image_path.write_bytes(extracted["image"])
            errors: list[dict[str, str]] = []
            ocr_text = ""
            try:
                ocr_text = _clean_text(ocr_provider.extract_text(image_path, page.page_number, image_index))
            except Exception as exc:
                errors.append({"stage": "ocr", "error": str(exc)})
            metadata = {
                "xref": xref,
                "image_extractor": self.name,
                "ocr_provider": ocr_provider.name,
                "vision_provider": vision_provider.name,
                "ocr_attempted": True,
                "vision_attempted": False,
                "ocr_execution_status": getattr(ocr_provider, "last_status", "called"),
                "vision_execution_status": "deferred_for_ranking",
            }
            if errors:
                metadata["processing_errors"] = errors
            chunks.append(
                ImageChunk(
                    chunk_id=f"p{page.page_number:03d}-image-{image_index:03d}",
                    page_number=page.page_number,
                    image_index=image_index,
                    width=width,
                    height=height,
                    extension=extension,
                    image_path=str(image_path) if image_path else None,
                    bbox=_image_bbox(page.raw_page, xref),
                    ocr_text=ocr_text,
                    vision_text="",
                    metadata=metadata,
                )
            )
        chunks.extend(
            self._render_page_evidence(
                page=page,
                output_dir=output_dir,
                ocr_provider=ocr_provider,
                vision_provider=vision_provider,
                start_index=image_index + 1,
            )
        )
        return chunks

    def _render_page_evidence(
        self,
        page: ParsedPdfPage,
        output_dir: Path | None,
        ocr_provider: OCRProvider,
        vision_provider: VisionProvider,
        start_index: int,
    ) -> list[ImageChunk]:
        chunks: list[ImageChunk] = []
        if output_dir is None:
            return chunks
        render_specs = [("page_render", None)]
        render_specs.extend(("table_region", table.bbox) for table in page.table_chunks if table.bbox)
        seen_regions: set[tuple[str, tuple[float, float, float, float] | None]] = set()
        image_index = start_index
        for source_type, bbox in render_specs:
            region_key = (source_type, bbox)
            if region_key in seen_regions:
                continue
            seen_regions.add(region_key)
            rendered = _render_page_region(page.raw_page, output_dir, page.page_number, image_index, source_type, bbox, self.render_zoom)
            if rendered is None:
                continue
            image_path, width, height = rendered
            errors: list[dict[str, str]] = []
            ocr_text = ""
            try:
                ocr_text = _clean_text(ocr_provider.extract_text(image_path, page.page_number, image_index))
            except Exception as exc:
                errors.append({"stage": "ocr", "error": str(exc)})
            metadata = {
                "image_source": source_type,
                "image_extractor": self.name,
                "ocr_provider": ocr_provider.name,
                "vision_provider": vision_provider.name,
                "ocr_attempted": True,
                "vision_attempted": False,
                "ocr_execution_status": getattr(ocr_provider, "last_status", "called"),
                "vision_execution_status": "deferred_for_ranking",
            }
            if errors:
                metadata["processing_errors"] = errors
            chunks.append(
                ImageChunk(
                    chunk_id=f"p{page.page_number:03d}-{source_type}-{image_index:03d}",
                    page_number=page.page_number,
                    image_index=image_index,
                    width=width,
                    height=height,
                    extension="png",
                    image_path=str(image_path),
                    bbox=bbox,
                    ocr_text=ocr_text,
                    vision_text="",
                    metadata=metadata,
                )
            )
            image_index += 1
        return chunks


def _render_page_region(
    raw_page: Any,
    output_dir: Path,
    page_number: int,
    image_index: int,
    source_type: str,
    bbox: tuple[float, float, float, float] | None,
    zoom: float,
) -> tuple[Path, int, int] | None:
    try:
        import fitz
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF is required for rendered page evidence: {exc}") from exc
    clip = fitz.Rect(*bbox) if bbox else None
    matrix = fitz.Matrix(zoom, zoom)
    try:
        pixmap = raw_page.get_pixmap(matrix=matrix, alpha=False, clip=clip)
    except Exception:
        return None
    image_path = output_dir / f"page-{page_number:03d}-{source_type}-{image_index:03d}.png"
    pixmap.save(image_path)
    width = int(pixmap.width)
    height = int(pixmap.height)
    del pixmap
    return image_path, width, height


def _image_bbox(page: Any, xref: int) -> tuple[float, float, float, float] | None:
    try:
        rects = page.get_image_rects(xref)
    except Exception:
        return None
    if not rects:
        return None
    rect = rects[0]
    return (round(float(rect.x0), 4), round(float(rect.y0), 4), round(float(rect.x1), 4), round(float(rect.y1), 4))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
