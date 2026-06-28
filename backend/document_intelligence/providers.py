from __future__ import annotations

import json
import base64
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Protocol


class OCRProvider(Protocol):
    name: str

    def extract_text(self, image_path: Path | None, page_number: int, image_index: int) -> str:
        ...


class VisionProvider(Protocol):
    name: str

    def describe_image(self, image_path: Path | None, page_number: int, image_index: int) -> str:
        ...


class PaddleOCRProvider:
    name = "paddleocr"

    def __init__(self) -> None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("FLAGS_minloglevel", "2")
        self._ocr = _paddle_ocr_model()
        self.last_status = "not_called"

    def extract_text(self, image_path: Path | None, page_number: int, image_index: int) -> str:
        if image_path is None:
            self.last_status = "no_image"
            return ""
        with _suppress_native_stdio(), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = self._ocr.predict(str(image_path))
        self.last_status = "executed"
        return _paddle_ocr_text(result)


def _paddle_ocr_text(result: object) -> str:
    texts: list[str] = []
    for item in result or []:
        data = item if isinstance(item, dict) else getattr(item, "json", lambda: {})()
        if not isinstance(data, dict):
            continue
        for text in data.get("rec_texts", []):
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return " ".join(texts)


_PADDLE_OCR_MODEL = None
_PADDLE_OCR_LOCK = threading.Lock()
_STDIO_SUPPRESSION_LOCK = threading.Lock()


def _paddle_ocr_model():
    global _PADDLE_OCR_MODEL
    with _PADDLE_OCR_LOCK:
        if _PADDLE_OCR_MODEL is None:
            with _suppress_native_stdio(), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                from paddleocr import PaddleOCR

                _PADDLE_OCR_MODEL = PaddleOCR(
                    lang="german",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
    return _PADDLE_OCR_MODEL


@contextmanager
def _suppress_native_stdio():
    with _STDIO_SUPPRESSION_LOCK:
        stdout_fd = stderr_fd = null_fd = None
        try:
            stdout_fd = os.dup(1)
            stderr_fd = os.dup(2)
            null_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
            yield
        finally:
            if stdout_fd is not None:
                os.dup2(stdout_fd, 1)
                os.close(stdout_fd)
            if stderr_fd is not None:
                os.dup2(stderr_fd, 2)
                os.close(stderr_fd)
            if null_fd is not None:
                os.close(null_fd)


class OllamaVisionProvider:
    def __init__(self, model: str = "qwen3-vl:8b", host: str = "http://127.0.0.1:11434", max_images: int = 30) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.max_images = max_images
        self.max_attempts = 2
        self.max_image_bytes = 8 * 1024 * 1024
        self.calls = 0
        self.last_status = "not_called"

    @property
    def name(self) -> str:
        return f"ollama_{self.model}"

    def describe_image(self, image_path: Path | None, page_number: int, image_index: int) -> str:
        if image_path is None:
            self.last_status = "no_image"
            return ""
        if self.calls >= self.max_images:
            self.last_status = "skipped_max_images"
            return ""
        image_size = image_path.stat().st_size
        if image_size > self.max_image_bytes:
            self.last_status = "skipped_image_too_large"
            raise RuntimeError(f"Vision image is too large for local request: {image_size} bytes")
        self.calls += 1
        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": (
                "Describe this technical-standard PDF image as structured observations. "
                "Focus on visible measurements, symbols, tables, diagrams, labels, limits, ranges, and requirement wording. "
                "Do not invent missing text. Return concise JSON-like text."
            ),
            "images": [image_base64],
            "stream": False,
        }
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                f"{self.host}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    data = json.loads(response.read().decode("utf-8"))
                text = str(data.get("response", "")).strip()
                if text:
                    self.last_status = "executed" if attempt == 1 else f"executed_after_retry_{attempt}"
                    return text
                last_error = "empty response"
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {exc.code}: {body}"
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)
        self.last_status = "failed"
        raise RuntimeError(f"Ollama vision provider returned no usable response after {self.max_attempts} attempts: {last_error}")


@dataclass(frozen=True)
class ProviderConfig:
    vision_model: str = "qwen3-vl:8b"
    max_vision_images: int = 30


class ProviderRegistry:
    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig()

    def ocr(self) -> OCRProvider:
        return PaddleOCRProvider()

    def vision(self) -> VisionProvider:
        return OllamaVisionProvider(self.config.vision_model, max_images=self.config.max_vision_images)
