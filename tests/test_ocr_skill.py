from __future__ import annotations

import pytest

from core import settings
from core.ocr import OCRLine, OCRMatch, OCRResult, OCRWord
from skills.ocr import OCRSkill


class FakeOCREngine:
    def __init__(self, *, read_result: OCRResult | None = None, matches: list[OCRMatch] | None = None, last_error: str = ""):
        self.read_result = read_result or OCRResult(
            full_text="Search Results",
            words=[OCRWord(text="Search", confidence=0.98, bbox=(10, 20, 110, 50))],
            lines=[OCRLine(text="Search Results", confidence=0.98, bbox=(10, 20, 250, 50), words=[])],
            engine="fakeocr",
            processing_time=0.12,
            capture_mode="active_window",
        )
        self.matches = matches or []
        self.last_error = last_error
        self.calls: list[tuple] = []

    def read_active_window(self):
        self.calls.append(("read_active_window",))
        return self.read_result

    def read_fullscreen(self):
        self.calls.append(("read_fullscreen",))
        return self.read_result

    def find_text(self, target: str, *, capture_mode: str | None = None, result=None):
        self.calls.append(("find_text", target, capture_mode))
        return self.matches

    def get_status(self):
        return {"ready": True, "active_engine": "fakeocr"}


@pytest.fixture(autouse=True)
def reset_ocr_skill_settings():
    settings.reset_defaults()
    yield
    settings.reset_defaults()


def test_ocr_skill_reads_screen_and_formats_visible_text():
    skill = OCRSkill(ocr_engine=FakeOCREngine())

    result = skill.execute("read screen", {"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "ocr_read"
    assert result.response == "Visible text:\nSearch Results"


def test_ocr_skill_reports_find_text_matches():
    match = OCRMatch(text="Search", confidence=0.98, bbox=(50, 70, 140, 100), source="word")
    skill = OCRSkill(ocr_engine=FakeOCREngine(matches=[match]))

    result = skill.execute("find text Search", {"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "ocr_find_text"
    assert result.response == 'Found "Search" on screen at 1 location.'
    assert result.data["matches"][0]["bbox"] == [50, 70, 140, 100]
