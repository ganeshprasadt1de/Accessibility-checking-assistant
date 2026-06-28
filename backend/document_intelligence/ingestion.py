from __future__ import annotations

import json
from pathlib import Path

from .image_ranking import apply_ranked_vision
from .image_extraction import ImageExtractor, PyMuPDFImageExtractor
from .models import PageMetadata, StructuredDocument
from .pdf_parser import PDFParser, PyMuPDFParser
from .providers import ProviderConfig, ProviderRegistry


class DocumentIntelligenceService:
    def __init__(
        self,
        pdf_parser: PDFParser | None = None,
        image_extractor: ImageExtractor | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.pdf_parser = pdf_parser or PyMuPDFParser()
        self.image_extractor = image_extractor or PyMuPDFImageExtractor()
        self.providers = providers or ProviderRegistry()

    def ingest(self, pdf_path: Path, image_output_dir: Path | None = None) -> StructuredDocument:
        metadata, parsed_pages, doc = self.pdf_parser.parse(pdf_path)
        text_chunks = []
        table_chunks = []
        image_chunks = []
        pages = []
        try:
            ocr_provider = self.providers.ocr()
            vision_provider = self.providers.vision()
            metadata.update(
                {
                    "document_intelligence_service": self.__class__.__name__,
                    "image_extractor": self.image_extractor.name,
                    "ocr_provider": ocr_provider.name,
                    "vision_provider": vision_provider.name,
                }
            )
            for parsed_page in parsed_pages:
                page_images = self.image_extractor.extract(doc, parsed_page, image_output_dir, ocr_provider, vision_provider)
                text_chunks.extend(parsed_page.text_blocks)
                table_chunks.extend(parsed_page.table_chunks)
                image_chunks.extend(page_images)
                pages.append(
                    PageMetadata(
                        page_number=parsed_page.page_number,
                        width=parsed_page.width,
                        height=parsed_page.height,
                        rotation=parsed_page.rotation,
                        text_length=len(parsed_page.text),
                        image_count=len(page_images),
                        table_count=len(parsed_page.table_chunks),
                    )
                )
            image_chunks, vision_audit = apply_ranked_vision(image_chunks, text_chunks, table_chunks, pages, vision_provider)
            metadata["vision_selection"] = vision_audit
        finally:
            close = getattr(doc, "close", None)
            if callable(close):
                close()

        return StructuredDocument(
            source_pdf=str(pdf_path),
            metadata=metadata,
            pages=pages,
            text_chunks=text_chunks,
            image_chunks=image_chunks,
            table_chunks=table_chunks,
        )


def ingest_pdf(
    pdf_path: Path,
    image_output_dir: Path | None = None,
    provider_config: ProviderConfig | None = None,
    pdf_parser: PDFParser | None = None,
    image_extractor: ImageExtractor | None = None,
) -> StructuredDocument:
    providers = ProviderRegistry(provider_config)
    return DocumentIntelligenceService(pdf_parser=pdf_parser, image_extractor=image_extractor, providers=providers).ingest(pdf_path, image_output_dir)


def save_structured_document(document: StructuredDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
