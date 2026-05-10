"""
Text-driven click automation using UIA-based text detection.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from PIL import Image

from core import settings, state
from core.automation import DesktopAutomation
from core.logger import get_logger
from core.screen import CaptureBounds, ScreenCapture

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from rapidfuzz import fuzz as rapidfuzz_fuzz
except Exception:  # pragma: no cover - import guard
    rapidfuzz_fuzz = None


_DANGEROUS_TARGETS = {
    "delete",
    "remove",
    "erase",
    "format",
    "uninstall",
    "sign out",
    "log out",
    "logout",
    "quit",
    "close account",
    "factory reset",
    "reset pc",
    "shut down",
    "shutdown",
}


@dataclass
class TextTarget:
    text: str
    normalized_text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    center_x: int
    center_y: int
    source_engine: str
    match_score: float = 0.0
    source_kind: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "normalized_text": self.normalized_text,
            "confidence": self.confidence,
            "bbox": list(self.bbox),
            "center_x": self.center_x,
            "center_y": self.center_y,
            "source_engine": self.source_engine,
            "match_score": self.match_score,
            "source_kind": self.source_kind,
            "details": dict(self.details),
        }

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bbox
        return max(0, right - left) * max(0, bottom - top)


@dataclass
class ClickResult:
    success: bool
    matched_text: str
    clicked_x: int
    clicked_y: int
    match_score: float
    verification_passed: bool
    message: str
    candidate_count: int = 0
    ambiguous: bool = False
    target: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "matched_text": self.matched_text,
            "clicked_x": self.clicked_x,
            "clicked_y": self.clicked_y,
            "match_score": self.match_score,
            "verification_passed": self.verification_passed,
            "message": self.message,
            "candidate_count": self.candidate_count,
            "ambiguous": self.ambiguous,
            "target": dict(self.target),
        }


@dataclass
class _VerificationSnapshot:
    captured_at: float
    foreground_hwnd: int
    window_title: str
    result: OCRResult


class TextClickEngine:
    """Find visible text using OCR, rank candidates, click safely, and verify honestly."""

    CACHE_TTL_SECONDS = 1.0

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        capture: ScreenCapture | None = None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._capture = capture or ScreenCapture()
        self._lock = threading.RLock()
        self._cached_result: Any = None
        self._cached_capture_mode: str = ""
        self._cached_at: float = 0.0
        self._click_history: list[tuple[float, str, int, int]] = []
        self.last_error: str = ""

    def find_targets(self, target_text: str, image: Image.Image | None = None) -> list[TextTarget]:
        query = self.normalize_text(target_text)
        if not query:
            return []
        
        # Use screen capture and UIA for text detection
        result = self._capture_result(force_refresh=False)
        if not result:
            return []
        return self.rank_targets(query, self._build_targets_from_capture(result))

    def rank_targets(self, target_text: str, candidates: Iterable[TextTarget]) -> list[TextTarget]:
        query = self.normalize_text(target_text)
        if not query:
            return []

        capture_bounds = self._capture_bounds_for_targets(candidates)
        ranked: list[TextTarget] = []
        for candidate in candidates:
            score = self._rank_score(query, candidate, capture_bounds=capture_bounds)
            if score <= 0.0:
                continue
            ranked.append(
                TextTarget(
                    text=candidate.text,
                    normalized_text=candidate.normalized_text,
                    confidence=candidate.confidence,
                    bbox=candidate.bbox,
                    center_x=candidate.center_x,
                    center_y=candidate.center_y,
                    source_engine=candidate.source_engine,
                    match_score=score,
                    source_kind=candidate.source_kind,
                    details=dict(candidate.details),
                )
            )
        ranked.sort(
            key=lambda item: (
                item.match_score,
                item.confidence,
                item.area,
                -abs(item.center_x),
                -abs(item.center_y),
            ),
            reverse=True,
        )
        return ranked

    def best_match(self, target_text: str, candidates: Iterable[TextTarget]) -> TextTarget | None:
        ranked = self.rank_targets(target_text, candidates)
        return ranked[0] if ranked else None

    def click_text(self, target_text: str) -> ClickResult:
        requested = str(target_text or "").strip()
        logger.info("Click target requested: %s", requested)

        if not settings.get("click_text_enabled"):
            return self._build_failure(requested, "Click-by-text is disabled in settings.")

        query = self.normalize_text(requested)
        if not query:
            return self._build_failure(requested, "I need visible text to click.")

        before_result = self._capture_result(force_refresh=False)
        before = self._snapshot_from_result(before_result)
        candidates = self.rank_targets(query, self._build_targets(before_result))
        logger.info("OCR targets found: %d", len(candidates))

        if not candidates:
            result = self._build_failure(requested, f'I could not find visible text matching "{requested}".')
            self._record_result(None, result)
            return result

        logger.info("Matches found: %d", len(candidates))
        best = candidates[0]
        min_confidence = float(settings.get("click_text_min_confidence") or 0.55)
        if best.match_score < min_confidence:
            message = (
                f'I found text near "{requested}", but the best match was below the safe threshold '
                f"({best.match_score:.2f} < {min_confidence:.2f})."
            )
            result = self._build_failure(
                requested,
                message,
                match_score=best.match_score,
                candidate_count=len(candidates),
                target=best,
            )
            self._record_result(best, result)
            return result

        if self._is_ambiguous(best, candidates[1:] if len(candidates) > 1 else []):
            noun = requested or best.text
            message = f'I found multiple "{noun}" targets with similar confidence. Please be more specific.'
            result = self._build_failure(
                requested,
                message,
                match_score=best.match_score,
                candidate_count=len(candidates),
                ambiguous=True,
                target=best,
            )
            self._record_result(best, result)
            return result

        if self._is_dangerous_target(query) and self._has_close_competitor(best, candidates[1:]):
            message = f'I found multiple dangerous matches for "{requested}". Please be more specific before I click.'
            result = self._build_failure(
                requested,
                message,
                match_score=best.match_score,
                candidate_count=len(candidates),
                ambiguous=True,
                target=best,
            )
            self._record_result(best, result)
            return result

        click_result = self.click_target(best)
        click_result.candidate_count = len(candidates)

        if click_result.success and settings.get("click_text_verify"):
            verified, verification_message = self._verify_with_retry(before, best)
            click_result.verification_passed = verified
            if len(candidates) > 1:
                prefix = f'I found {len(candidates)} matches for {best.text}. Clicking the best match.'
            else:
                prefix = f"Found {best.text} and clicked it."
            if verified:
                click_result.message = prefix
            else:
                click_result.message = f"{prefix} {verification_message}"
        elif click_result.success and len(candidates) > 1:
            click_result.message = f'I found {len(candidates)} matches for {best.text}. Clicking the best match.'

        if click_result.success:
            self._remember_click(best)
            self.last_error = ""
        self._record_result(best, click_result)
        return click_result

    def click_target(self, target: TextTarget) -> ClickResult:
        if not self._is_valid_bbox(target.bbox):
            return self._build_failure(
                target.text,
                f'The detected target for "{target.text}" has an invalid bounding box.',
                match_score=target.match_score,
                target=target,
            )

        x, y = self.compute_click_point(target.bbox)
        if not self._point_is_on_screen(x, y):
            return self._build_failure(
                target.text,
                f'The detected target for "{target.text}" is outside the visible desktop.',
                match_score=target.match_score,
                target=target,
            )

        if settings.get("highlight_target_before_click"):
            self._automation.move_point(x, y)
            self._automation.safe_sleep(120)

        clicked = self._automation.click_point(x, y)
        if not clicked:
            return self._build_failure(
                target.text,
                f'I found "{target.text}", but the click could not be executed.',
                clicked_x=x,
                clicked_y=y,
                match_score=target.match_score,
                target=target,
            )

        logger.info("Selected: %s score=%.2f", target.text, target.match_score)
        logger.info("Clicked at (%d, %d)", x, y)
        return ClickResult(
            success=True,
            matched_text=target.text,
            clicked_x=x,
            clicked_y=y,
            match_score=target.match_score,
            verification_passed=not bool(settings.get("click_text_verify")),
            message=f"Found {target.text} and clicked it.",
            target=target.to_dict(),
        )

    @staticmethod
    def normalize_text(text: str) -> str:
        lowered = str(text or "").strip().lower()
        lowered = re.sub(r"[^\w\s]", " ", lowered)
        return " ".join(lowered.split())

    @staticmethod
    def compute_click_point(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
        left, top, right, bottom = bbox
        center_x = left + max(0, right - left) // 2
        center_y = top + max(0, bottom - top) // 2
        return int(center_x), int(center_y)

    def verify_after_click(
        self,
        before: _VerificationSnapshot,
        after: _VerificationSnapshot,
        *,
        target: TextTarget | None = None,
    ) -> tuple[bool, str]:
        if before.foreground_hwnd and after.foreground_hwnd and before.foreground_hwnd != after.foreground_hwnd:
            return True, "Focus moved to another window."

        before_title = str(before.window_title or "").strip()
        after_title = str(after.window_title or "").strip()
        if before_title and after_title and before_title != after_title:
            return True, "Window title changed."

        before_text = self.normalize_text(before.result.full_text)
        after_text = self.normalize_text(after.result.full_text)
        if before_text and after_text and before_text != after_text:
            return True, "OCR content changed."

        if target is not None:
            before_matches = self._count_matching_targets(target.normalized_text, before.result)
            after_matches = self._count_matching_targets(target.normalized_text, after.result)
            if before_matches and after_matches < before_matches:
                return True, "Target disappeared after the click."

        if not before_title and not after_title and not before_text and not after_text:
            return False, "I clicked it, but no reliable verification signal was available."
        return False, "I clicked it, but I could not verify a visible UI change."

    def _verify_with_retry(self, before: _VerificationSnapshot, target: TextTarget) -> tuple[bool, str]:
        deadline = time.monotonic() + 1.6
        message = "I clicked it, but I could not verify a visible UI change."
        while time.monotonic() <= deadline:
            self._automation.safe_sleep(220)
            after = self._snapshot_from_result(self._capture_result(force_refresh=True))
            verified, message = self.verify_after_click(before, after, target=target)
            if verified:
                logger.info("Verification result: passed")
                return True, message
        logger.info("Verification result: not verified")
        return False, message

    def _capture_result(self, *, force_refresh: bool) -> OCRResult:
        capture_mode = self._capture_mode()
        with self._lock:
            age = time.monotonic() - self._cached_at
            if (
                not force_refresh
                and self._cached_result is not None
                and self._cached_capture_mode == capture_mode
                and age <= self.CACHE_TTL_SECONDS
            ):
                return self._cached_result

        # Use screen capture directly without OCR
        capture = self._capture
        if capture_mode == "active_window":
            result = capture.capture_active_window()
        elif capture_mode == "full_screen":
            result = capture.capture_full_screen()
        else:
            result = capture.capture_active_window()
        
        with self._lock:
            self._cached_result = result
            self._cached_capture_mode = capture_mode
            self._cached_at = time.monotonic()
        return result
    
    def _build_targets_from_capture(self, result: Any) -> list[TextTarget]:
        """Build targets from screen capture using UIA."""
        targets = []
        if not result:
            return targets
        
        # Use UIA to get text from screen
        try:
            automation = self._automation
            windows = automation.get_all_windows()
            for window in windows:
                if window.title:
                    targets.append(TextTarget(
                        text=window.title,
                        normalized_text=self.normalize_text(window.title),
                        confidence=0.8,
                        bbox=(window.rect.left, window.rect.top, window.rect.right, window.rect.bottom),
                        center_x=(window.rect.left + window.rect.right) // 2,
                        center_y=(window.rect.top + window.rect.bottom) // 2,
                        source_engine="uia",
                        match_score=0.0,
                        source_kind="window_title",
                    ))
        except Exception:
            pass
        
        return targets

    def _snapshot_from_result(self, result: OCRResult) -> _VerificationSnapshot:
        foreground_hwnd = self._automation.get_foreground_window()
        window = self._automation.get_window(foreground_hwnd) if foreground_hwnd else None
        window_title = str(window.title if window else "").strip()
        if not window_title and result.capture_mode == "active_window":
            bounds = self._capture.get_active_window_bounds()
            window_title = str(bounds.title if bounds else "").strip()
        return _VerificationSnapshot(
            captured_at=time.monotonic(),
            foreground_hwnd=foreground_hwnd,
            window_title=window_title,
            result=result,
        )

    def _build_targets(self, result: Any) -> list[TextTarget]:
        """Build targets from capture result using UIA."""
        targets: list[TextTarget] = []
        seen: set[tuple[str, tuple[int, int, int, int]]] = set()

        # Use UIA to get text elements
        try:
            windows = self._automation.get_all_windows()
            for window in windows:
                if not window.title:
                    continue
                text = window.title.strip()
                normalized = self.normalize_text(text)
                bbox = (window.rect.left, window.rect.top, window.rect.right, window.rect.bottom)
                key = (normalized, bbox)
                if not normalized or key in seen or not self._is_valid_bbox(bbox):
                    continue
                seen.add(key)
                center_x, center_y = self.compute_click_point(bbox)
                targets.append(
                    TextTarget(
                        text=text,
                        normalized_text=normalized,
                        confidence=0.8,
                        bbox=bbox,
                        center_x=center_x,
                        center_y=center_y,
                        source_engine="uia",
                        source_kind=kind,
                        details={"capture_mode": result.capture_mode},
                    )
                )
        except Exception:
            pass
        return targets

    @staticmethod
    def _iter_result_items(result: OCRResult) -> Iterable[tuple[OCRWord | OCRLine, str]]:
        for line in result.lines:
            yield line, "line"
        for word in result.words:
            yield word, "word"

    def _rank_score(
        self,
        query: str,
        candidate: TextTarget,
        *,
        capture_bounds: CaptureBounds | None,
    ) -> float:
        text_score = self._text_similarity(query, candidate.normalized_text)
        if text_score < 0.45:
            return 0.0

        exact_bonus = 0.0
        query_compact = query.replace(" ", "")
        candidate_compact = candidate.normalized_text.replace(" ", "")
        if candidate.normalized_text == query:
            exact_bonus += 0.08
        elif candidate_compact == query_compact:
            exact_bonus += 0.06
        elif query and query in candidate.normalized_text:
            exact_bonus += 0.03

        kind_bonus = 0.03 if (" " in query and candidate.source_kind == "line") else 0.02 if candidate.source_kind == "word" else 0.0
        area_score = self._area_score(candidate, capture_bounds)
        center_score = self._center_score(candidate, capture_bounds)
        foreground_score = self._foreground_score(candidate)
        history_penalty = self._recent_click_penalty(candidate)

        final_score = (
            (text_score * 0.55)
            + (candidate.confidence * 0.17)
            + (area_score * 0.10)
            + (center_score * 0.10)
            + (foreground_score * 0.04)
            + exact_bonus
            + kind_bonus
            - history_penalty
        )
        return max(0.0, min(1.0, final_score))

    def _text_similarity(self, query: str, candidate: str) -> float:
        if not query or not candidate:
            return 0.0
        if query == candidate:
            return 1.0

        query_compact = query.replace(" ", "")
        candidate_compact = candidate.replace(" ", "")
        if query_compact == candidate_compact:
            return 0.98
        if not settings.get("click_text_fuzzy_match"):
            return 0.88 if query in candidate or candidate in query else 0.0

        if rapidfuzz_fuzz is not None:  # pragma: no branch - optional fast path
            scores = [
                rapidfuzz_fuzz.ratio(query, candidate),
                rapidfuzz_fuzz.partial_ratio(query, candidate),
                rapidfuzz_fuzz.token_sort_ratio(query, candidate),
                rapidfuzz_fuzz.ratio(query_compact, candidate_compact),
            ]
            return max(scores) / 100.0

        scores = [
            SequenceMatcher(None, query, candidate).ratio(),
            SequenceMatcher(None, query_compact, candidate_compact).ratio(),
        ]
        if query in candidate or candidate in query:
            shorter = min(len(query), len(candidate))
            longer = max(len(query), len(candidate))
            scores.append(shorter / max(1, longer))
        return max(scores)

    def _capture_bounds_for_targets(self, candidates: Iterable[TextTarget]) -> CaptureBounds | None:
        capture_mode = self._capture_mode()
        if capture_mode == "active_window":
            return self._capture.get_active_window_bounds()
        return None

    @staticmethod
    def _area_score(candidate: TextTarget, capture_bounds: CaptureBounds | None) -> float:
        if candidate.area <= 0:
            return 0.0
        if capture_bounds is None:
            return min(1.0, math.log(candidate.area + 10, 10) / 5.0)
        reference = max(1, capture_bounds.width * capture_bounds.height)
        return max(0.0, min(1.0, math.sqrt(candidate.area / reference) * 4.0))

    @staticmethod
    def _center_score(candidate: TextTarget, capture_bounds: CaptureBounds | None) -> float:
        if capture_bounds is None:
            return 0.5
        center_x = capture_bounds.left + (capture_bounds.width / 2.0)
        center_y = capture_bounds.top + (capture_bounds.height / 2.0)
        dx = float(candidate.center_x) - center_x
        dy = float(candidate.center_y) - center_y
        diagonal = math.hypot(capture_bounds.width, capture_bounds.height) or 1.0
        distance = math.hypot(dx, dy)
        return max(0.0, min(1.0, 1.0 - (distance / diagonal)))

    def _foreground_score(self, candidate: TextTarget) -> float:
        active = self._capture.get_active_window_bounds()
        if active is None:
            return 0.5
        return 1.0 if self._point_in_bbox(candidate.center_x, candidate.center_y, active.as_bbox()) else 0.2

    def _recent_click_penalty(self, candidate: TextTarget) -> float:
        cutoff = time.monotonic() - 10.0
        recent = [item for item in self._click_history if item[0] >= cutoff]
        self._click_history = recent
        penalty = 0.0
        for _timestamp, recent_text, x, y in recent:
            if recent_text == candidate.normalized_text:
                penalty += 0.08
            if abs(x - candidate.center_x) <= 12 and abs(y - candidate.center_y) <= 12:
                penalty += 0.08
        return min(0.20, penalty)

    def _remember_click(self, target: TextTarget) -> None:
        self._click_history.append((time.monotonic(), target.normalized_text, target.center_x, target.center_y))
        self._click_history = self._click_history[-12:]

    def _count_matching_targets(self, query: str, result: OCRResult) -> int:
        count = 0
        for target in self._build_targets(result):
            if self._text_similarity(query, target.normalized_text) >= 0.75:
                count += 1
        return count

    def _point_is_on_screen(self, x: int, y: int) -> bool:
        bounds = self._capture.get_virtual_screen_bounds()
        return self._point_in_bbox(x, y, bounds.as_bbox())

    @staticmethod
    def _point_in_bbox(x: int, y: int, bbox: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = bbox
        return left <= int(x) <= right and top <= int(y) <= bottom

    @staticmethod
    def _is_valid_bbox(bbox: Sequence[int]) -> bool:
        if len(tuple(bbox)) != 4:
            return False
        left, top, right, bottom = [int(value) for value in bbox]
        return right > left and bottom > top

    def _has_close_competitor(self, best: TextTarget, others: Sequence[TextTarget]) -> bool:
        if not others:
            return False
        return others[0].match_score >= max(0.0, best.match_score - 0.05)

    def _is_ambiguous(self, best: TextTarget, others: Sequence[TextTarget]) -> bool:
        if not others:
            return False
        runner_up = others[0]
        return runner_up.match_score >= 0.80 and abs(best.match_score - runner_up.match_score) <= 0.03

    @staticmethod
    def _is_dangerous_target(normalized: str) -> bool:
        return normalized in _DANGEROUS_TARGETS

    @staticmethod
    def _capture_mode() -> str:
        return str(settings.get("screen_capture_mode") or "active_window").strip().lower()

    def _build_failure(
        self,
        requested: str,
        message: str,
        *,
        clicked_x: int = 0,
        clicked_y: int = 0,
        match_score: float = 0.0,
        candidate_count: int = 0,
        ambiguous: bool = False,
        target: TextTarget | None = None,
    ) -> ClickResult:
        self.last_error = message
        logger.warning("Click-by-text failed: %s", message)
        return ClickResult(
            success=False,
            matched_text=requested,
            clicked_x=clicked_x,
            clicked_y=clicked_y,
            match_score=match_score,
            verification_passed=False,
            message=message,
            candidate_count=candidate_count,
            ambiguous=ambiguous,
            target=target.to_dict() if target is not None else {},
        )

    def _record_result(self, target: TextTarget | None, result: ClickResult) -> None:
        state.last_text_click_target = target.to_dict() if target is not None else {}
        state.last_text_click_result = result.to_dict()
        state.last_clicked_position = {"x": result.clicked_x, "y": result.clicked_y} if result.success else {}
        if result.success:
            state.text_click_count = int(getattr(state, "text_click_count", 0) or 0) + 1


_text_click_engine_instance: TextClickEngine | None = None


def get_text_click_engine() -> TextClickEngine:
    global _text_click_engine_instance
    if _text_click_engine_instance is None:
        _text_click_engine_instance = TextClickEngine()
    return _text_click_engine_instance
