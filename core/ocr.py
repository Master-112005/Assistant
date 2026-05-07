"""
OCR engine abstraction with real capture, preprocessing, and structured text output.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from PIL import Image

from core import settings, state
from core.image_utils import preprocess_for_ocr
from core.logger import get_logger
from core.paths import SCREENSHOTS_DIR
from core.screen import CaptureBounds, ScreenCapture

logger = get_logger(__name__)

_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
_NORMALIZE_RE = re.compile(r"[^\w]+", flags=re.UNICODE)


@dataclass
class OCRWord:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    line_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": list(self.bbox),
            "line_index": self.line_index,
        }


@dataclass
class OCRLine:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    words: list[OCRWord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": list(self.bbox),
            "words": [word.to_dict() for word in self.words],
        }


@dataclass
class OCRMatch:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    source: str
    line_text: str = ""
    words: list[OCRWord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": list(self.bbox),
            "source": self.source,
            "line_text": self.line_text,
            "words": [word.to_dict() for word in self.words],
        }


@dataclass
class OCRResult:
    full_text: str
    words: list[OCRWord]
    lines: list[OCRLine]
    engine: str
    processing_time: float
    capture_mode: str = ""
    capture_bounds: tuple[int, int, int, int] | None = None
    screenshot_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_text": self.full_text,
            "words": [word.to_dict() for word in self.words],
            "lines": [line.to_dict() for line in self.lines],
            "engine": self.engine,
            "processing_time": self.processing_time,
            "capture_mode": self.capture_mode,
            "capture_bounds": list(self.capture_bounds) if self.capture_bounds else None,
            "screenshot_path": self.screenshot_path,
        }


class _OCRBackend(Protocol):
    engine_name: str

    def initialize(self) -> None:
        ...

    def read(self, image: Image.Image) -> list[OCRWord]:
        ...


class _EasyOCRBackend:
    engine_name = "easyocr"

    def __init__(self) -> None:
        self._reader = None

    def initialize(self) -> None:
        easyocr = importlib.import_module("easyocr")
        self._reader = easyocr.Reader(["en"], gpu=False, verbose=False)

    def read(self, image: Image.Image) -> list[OCRWord]:
        if self._reader is None:
            raise RuntimeError("EasyOCR backend is not initialized.")
        np = importlib.import_module("numpy")
        results = self._reader.readtext(np.array(image), detail=1, paragraph=False)
        words: list[OCRWord] = []
        for box, text, confidence in results:
            cleaned = _clean_text(text)
            if not cleaned:
                continue
            words.append(
                OCRWord(
                    text=cleaned,
                    confidence=float(confidence),
                    bbox=_bbox_from_polygon(box),
                )
            )
        return words


class _PytesseractBackend:
    engine_name = "pytesseract"

    def __init__(self) -> None:
        self._module = None

    def initialize(self) -> None:
        module = importlib.import_module("pytesseract")
        executable = _resolve_tesseract_executable()
        if executable:
            module.pytesseract.tesseract_cmd = executable
        try:
            module.get_tesseract_version()
        except Exception as exc:  # pragma: no cover - external dependency
            raise RuntimeError("Tesseract OCR is not installed or not on PATH.") from exc
        self._module = module

    def read(self, image: Image.Image) -> list[OCRWord]:
        if self._module is None:
            raise RuntimeError("pytesseract backend is not initialized.")
        data = self._module.image_to_data(
            image,
            output_type=self._module.Output.DICT,
            config="--oem 3 --psm 11",
        )
        words: list[OCRWord] = []
        total = len(data.get("text", []))
        for index in range(total):
            text = _clean_text(data["text"][index])
            confidence = _parse_tesseract_confidence(data["conf"][index])
            width = int(data["width"][index] or 0)
            height = int(data["height"][index] or 0)
            if not text or width <= 0 or height <= 0:
                continue
            words.append(
                OCRWord(
                    text=text,
                    confidence=confidence,
                    bbox=(
                        int(data["left"][index] or 0),
                        int(data["top"][index] or 0),
                        int(data["left"][index] or 0) + width,
                        int(data["top"][index] or 0) + height,
                    ),
                )
            )
        return words


class OCREngine:
    """Shared OCR service used by commands and app-specific skills."""

    def __init__(
        self,
        *,
        capture: ScreenCapture | None = None,
        preferred_engine: str | None = None,
        backends: Sequence[_OCRBackend] | None = None,
    ) -> None:
        self._capture = capture or ScreenCapture()
        self._preferred_engine = str(preferred_engine or "").strip().lower()
        self._backends = list(backends) if backends is not None else None
        self._backend: _OCRBackend | None = None
        self._lock = threading.RLock()
        self._last_result: OCRResult | None = None
        self.last_error: str = ""

    def initialize(self) -> bool:
        with self._lock:
            if self._backend is not None:
                state.ocr_ready = True
                return True

            candidates = self._build_backends()
            if not candidates:
                self.last_error = "No OCR backend is configured."
                state.ocr_ready = False
                logger.error("OCR initialization failed: %s", self.last_error)
                return False

            for backend in candidates:
                try:
                    backend.initialize()
                    self._backend = backend
                    self.last_error = ""
                    state.ocr_ready = True
                    state.last_ocr_engine = backend.engine_name
                    logger.info("OCR initialized")
                    logger.info("OCR engine: %s", backend.engine_name)
                    return True
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.warning("OCR backend unavailable: %s | %s", backend.engine_name, exc)

            state.ocr_ready = False
            logger.error("OCR initialization failed: %s", self.last_error)
            return False

    def is_ready(self) -> bool:
        return self._backend is not None

    def get_status(self) -> dict[str, Any]:
        preferred = self._requested_engine()
        availability = self.available_backends()
        return {
            "enabled": bool(settings.get("ocr_enabled")),
            "preferred_engine": preferred,
            "active_engine": self._backend.engine_name if self._backend else "",
            "ready": bool(self._backend),
            "available_backends": availability,
            "capture_mode": settings.get("ocr_capture_mode"),
            "last_error": self.last_error,
        }

    def read_image(
        self,
        image: Image.Image,
        *,
        capture_mode: str = "image",
        capture_bounds: CaptureBounds | None = None,
    ) -> OCRResult:
        start = time.perf_counter()
        if image is None:
            return self._empty_result(start, capture_mode=capture_mode, capture_bounds=capture_bounds)
        if not settings.get("ocr_enabled"):
            self.last_error = "OCR is disabled in settings."
            return self._empty_result(start, capture_mode=capture_mode, capture_bounds=capture_bounds)
        if not self.initialize():
            return self._empty_result(start, capture_mode=capture_mode, capture_bounds=capture_bounds)

        logger.info("Capture mode used: %s", capture_mode)
        logger.info("OCR started")

        screenshot_path = self._save_debug_screenshot(image, capture_mode=capture_mode)
        processed = preprocess_for_ocr(
            image,
            enabled=bool(settings.get("ocr_preprocess")),
        )
        scale_x = processed.width / max(1, image.width)
        scale_y = processed.height / max(1, image.height)

        try:
            raw_words = self._backend.read(processed) if self._backend is not None else []
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("OCR read failed: %s", exc)
            return self._empty_result(
                start,
                capture_mode=capture_mode,
                capture_bounds=capture_bounds,
                screenshot_path=screenshot_path,
            )

        min_confidence = float(settings.get("ocr_min_confidence") or 0.40)
        mapped_words = self._map_words_to_capture(raw_words, scale_x=scale_x, scale_y=scale_y, capture_bounds=capture_bounds)
        words = [word for word in mapped_words if word.confidence >= min_confidence]
        lines = _group_words_into_lines(words)
        full_text = " ".join(line.text for line in lines if line.text).strip()
        processing_time = time.perf_counter() - start

        result = OCRResult(
            full_text=full_text,
            words=words,
            lines=lines,
            engine=self._backend.engine_name if self._backend else "",
            processing_time=processing_time,
            capture_mode=capture_mode,
            capture_bounds=capture_bounds.as_bbox() if capture_bounds else None,
            screenshot_path=screenshot_path,
        )
        self._last_result = result
        self.last_error = ""
        state.ocr_ready = bool(self._backend)
        state.last_ocr_text = result.full_text
        state.last_ocr_engine = result.engine
        state.last_screenshot_path = screenshot_path
        state.last_text_matches = []
        logger.info("OCR completed")
        logger.info("OCR words detected: %d", len(result.words))
        logger.info("Processing time: %.2f sec", processing_time)
        return result

    def read_active_window(self) -> OCRResult:
        start = time.perf_counter()
        try:
            bounds = self._capture.get_active_window_bounds()
            if bounds is None:
                self.last_error = "The active window bounds could not be detected."
                logger.error("Active-window capture failed: %s", self.last_error)
                return self._empty_result(start, capture_mode="active_window")
            image = self._capture.capture_active_window()
            return self.read_image(image, capture_mode="active_window", capture_bounds=bounds)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Active-window capture failed: %s", exc)
            return self._empty_result(start, capture_mode="active_window")

    def read_fullscreen(self) -> OCRResult:
        start = time.perf_counter()
        try:
            bounds = self._capture.get_virtual_screen_bounds()
            image = self._capture.capture_fullscreen()
            return self.read_image(image, capture_mode="fullscreen", capture_bounds=bounds)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Fullscreen OCR failed: %s", exc)
            return self._empty_result(start, capture_mode="fullscreen")

    def read_region(self, x: int, y: int, w: int, h: int) -> OCRResult:
        start = time.perf_counter()
        bounds = CaptureBounds(left=int(x), top=int(y), width=max(1, int(w)), height=max(1, int(h)))
        try:
            image = self._capture.capture_region(bounds.left, bounds.top, bounds.width, bounds.height)
            return self.read_image(image, capture_mode="region", capture_bounds=bounds)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Region OCR failed: %s", exc)
            return self._empty_result(start, capture_mode="region", capture_bounds=bounds)

    def find_text(
        self,
        target: str,
        *,
        result: OCRResult | None = None,
        capture_mode: str | None = None,
    ) -> list[OCRMatch]:
        query = _normalize_text(target)
        if not query:
            state.last_text_matches = []
            return []

        active_result = result or self._read_from_capture_mode(capture_mode or str(settings.get("ocr_capture_mode") or "active_window"))
        matches: list[OCRMatch] = []

        if active_result.words:
            matches.extend(_match_words(query, active_result.words, active_result.lines))
        if active_result.lines:
            matches.extend(_match_lines(query, active_result.lines))

        deduped: list[OCRMatch] = []
        seen: set[tuple[str, tuple[int, int, int, int], str]] = set()
        for match in matches:
            key = (_normalize_text(match.text), match.bbox, match.source)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(match)

        state.last_text_matches = [match.to_dict() for match in deduped]
        logger.info("Find text results: %d match(es) for %r", len(deduped), target)
        return deduped

    def read_capture_mode(self, capture_mode: str | None = None) -> OCRResult:
        return self._read_from_capture_mode(str(capture_mode or settings.get("ocr_capture_mode") or "active_window"))

    def get_last_result(self) -> OCRResult | None:
        return self._last_result

    def _build_backends(self) -> list[_OCRBackend]:
        if self._backends is not None:
            return list(self._backends)

        requested = self._requested_engine()
        backends: list[_OCRBackend] = []
        if requested in {"", "auto", "easyocr"}:
            backends.append(_EasyOCRBackend())
        if requested in {"", "auto", "pytesseract", "tesseract"}:
            backends.append(_PytesseractBackend())
        if requested == "easyocr":
            backends.append(_PytesseractBackend())
        elif requested in {"pytesseract", "tesseract"}:
            backends.append(_EasyOCRBackend())
        if not backends:
            backends = [_EasyOCRBackend(), _PytesseractBackend()]
        return backends

    def _requested_engine(self) -> str:
        candidate = self._preferred_engine or str(settings.get("ocr_engine") or "")
        return candidate.strip().lower() or "easyocr"

    def _save_debug_screenshot(self, image: Image.Image, *, capture_mode: str) -> str:
        if not settings.get("save_debug_screenshots"):
            return ""
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"ocr-{capture_mode}-{timestamp}.png"
        path = Path(SCREENSHOTS_DIR) / filename
        image.save(path)
        return str(path)

    def _map_words_to_capture(
        self,
        words: Sequence[OCRWord],
        *,
        scale_x: float,
        scale_y: float,
        capture_bounds: CaptureBounds | None,
    ) -> list[OCRWord]:
        mapped: list[OCRWord] = []
        offset_x = capture_bounds.left if capture_bounds else 0
        offset_y = capture_bounds.top if capture_bounds else 0
        for word in words:
            left, top, right, bottom = word.bbox
            mapped.append(
                OCRWord(
                    text=word.text,
                    confidence=word.confidence,
                    bbox=(
                        int(round(left / max(scale_x, 1e-9))) + offset_x,
                        int(round(top / max(scale_y, 1e-9))) + offset_y,
                        int(round(right / max(scale_x, 1e-9))) + offset_x,
                        int(round(bottom / max(scale_y, 1e-9))) + offset_y,
                    ),
                )
            )
        return mapped

    def _read_from_capture_mode(self, capture_mode: str) -> OCRResult:
        normalized = str(capture_mode or "active_window").strip().lower()
        if normalized in {"fullscreen", "full_screen", "full screen"}:
            return self.read_fullscreen()
        return self.read_active_window()

    def _empty_result(
        self,
        start: float,
        *,
        capture_mode: str,
        capture_bounds: CaptureBounds | None = None,
        screenshot_path: str = "",
    ) -> OCRResult:
        processing_time = max(0.0, time.perf_counter() - start)
        result = OCRResult(
            full_text="",
            words=[],
            lines=[],
            engine=self._backend.engine_name if self._backend else "",
            processing_time=processing_time,
            capture_mode=capture_mode,
            capture_bounds=capture_bounds.as_bbox() if capture_bounds else None,
            screenshot_path=screenshot_path,
        )
        self._last_result = result
        state.last_ocr_text = ""
        state.last_screenshot_path = screenshot_path
        state.last_text_matches = []
        if not self._backend:
            state.last_ocr_engine = ""
        return result

    @staticmethod
    def available_backends() -> dict[str, bool]:
        return {
            "easyocr": importlib.util.find_spec("easyocr") is not None,
            "pytesseract": importlib.util.find_spec("pytesseract") is not None and bool(_resolve_tesseract_executable()),
        }


def _clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text.strip()


def _normalize_text(value: Any) -> str:
    cleaned = _NORMALIZE_RE.sub(" ", str(value or "").strip().lower())
    return " ".join(cleaned.split())


def _parse_tesseract_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        return 0.0
    if confidence < 0:
        return 0.0
    if confidence > 1.0:
        confidence = confidence / 100.0
    return max(0.0, min(1.0, confidence))


def _bbox_from_polygon(points: Sequence[Sequence[float]]) -> tuple[int, int, int, int]:
    xs = [int(round(point[0])) for point in points]
    ys = [int(round(point[1])) for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _resolve_tesseract_executable() -> str:
    in_path = shutil.which("tesseract")
    if in_path:
        return in_path
    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def _group_words_into_lines(words: Sequence[OCRWord]) -> list[OCRLine]:
    if not words:
        return []

    lines: list[list[OCRWord]] = []
    ordered = sorted(words, key=lambda item: (item.bbox[1], item.bbox[0]))
    for word in ordered:
        center_y = (word.bbox[1] + word.bbox[3]) / 2.0
        height = max(1, word.bbox[3] - word.bbox[1])
        placed = False
        for line_words in lines:
            line_top = min(item.bbox[1] for item in line_words)
            line_bottom = max(item.bbox[3] for item in line_words)
            line_center = (line_top + line_bottom) / 2.0
            line_height = max(1, line_bottom - line_top)
            tolerance = max(12.0, height * 0.65, line_height * 0.65)
            if abs(center_y - line_center) <= tolerance:
                line_words.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])

    structured: list[OCRLine] = []
    for index, line_words in enumerate(lines):
        ordered_words = sorted(line_words, key=lambda item: (item.bbox[0], item.bbox[1]))
        for word in ordered_words:
            word.line_index = index
        text = " ".join(word.text for word in ordered_words).strip()
        confidence = sum(word.confidence for word in ordered_words) / max(1, len(ordered_words))
        bbox = _merge_bboxes(word.bbox for word in ordered_words)
        structured.append(OCRLine(text=text, confidence=confidence, bbox=bbox, words=ordered_words))
    return structured


def _merge_bboxes(boxes: Sequence[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    box_list = list(boxes)
    left = min(box[0] for box in box_list)
    top = min(box[1] for box in box_list)
    right = max(box[2] for box in box_list)
    bottom = max(box[3] for box in box_list)
    return (left, top, right, bottom)


def _match_words(query: str, words: Sequence[OCRWord], lines: Sequence[OCRLine]) -> list[OCRMatch]:
    matches: list[OCRMatch] = []
    line_text_by_index = {index: line.text for index, line in enumerate(lines)}
    for word in words:
        normalized_word = _normalize_text(word.text)
        if not normalized_word:
            continue
        if normalized_word == query or query in normalized_word:
            matches.append(
                OCRMatch(
                    text=word.text,
                    confidence=word.confidence,
                    bbox=word.bbox,
                    source="word",
                    line_text=line_text_by_index.get(word.line_index, ""),
                    words=[word],
                )
            )
    return matches


def _match_lines(query: str, lines: Sequence[OCRLine]) -> list[OCRMatch]:
    matches: list[OCRMatch] = []
    query_tokens = query.split()
    for line in lines:
        normalized_line = _normalize_text(line.text)
        if not normalized_line or query not in normalized_line:
            continue

        phrase_words = _find_phrase_words(line.words, query_tokens)
        if phrase_words:
            bbox = _merge_bboxes(word.bbox for word in phrase_words)
            confidence = sum(word.confidence for word in phrase_words) / max(1, len(phrase_words))
            matches.append(
                OCRMatch(
                    text=" ".join(word.text for word in phrase_words).strip(),
                    confidence=confidence,
                    bbox=bbox,
                    source="phrase",
                    line_text=line.text,
                    words=list(phrase_words),
                )
            )
        else:
            matches.append(
                OCRMatch(
                    text=line.text,
                    confidence=line.confidence,
                    bbox=line.bbox,
                    source="line",
                    line_text=line.text,
                    words=list(line.words),
                )
            )
    return matches


def _find_phrase_words(words: Sequence[OCRWord], query_tokens: Sequence[str]) -> list[OCRWord]:
    if not words or not query_tokens:
        return []

    normalized_words = [_normalize_text(word.text) for word in words]
    target = " ".join(query_tokens)
    for start in range(len(words)):
        buffer: list[str] = []
        matched_words: list[OCRWord] = []
        for offset in range(start, len(words)):
            candidate = normalized_words[offset]
            if not candidate:
                continue
            buffer.append(candidate)
            matched_words.append(words[offset])
            phrase = " ".join(buffer)
            if phrase == target:
                return matched_words
            if not target.startswith(phrase):
                break
    return []


_ocr_engine_instance: OCREngine | None = None


def get_ocr_engine() -> OCREngine:
    global _ocr_engine_instance
    if _ocr_engine_instance is None:
        _ocr_engine_instance = OCREngine()
    return _ocr_engine_instance
