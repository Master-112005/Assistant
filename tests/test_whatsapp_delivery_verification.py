from __future__ import annotations

from core import settings, state
from skills.whatsapp import WhatsAppSkill, WhatsAppWindowState


class _FakeAutomation:
    def hotkey(self, keys) -> bool:
        return True

    def type_text(self, text: str, *, clear: bool = False, delay_ms: int | None = None) -> bool:
        return True

    def press_key(self, key: str) -> bool:
        return True


class _VerificationSkill(WhatsAppSkill):
    def __init__(self) -> None:
        super().__init__(automation=_FakeAutomation(), desktop_factory=lambda: None)
        self.window_state = WhatsAppWindowState(
            hwnd=1,
            title="WhatsApp",
            process_name="whatsapp.exe",
            rect=(0, 0, 1200, 900),
            source="desktop",
            is_foreground=True,
            is_minimized=False,
        )
        self.chat_name = "Hemanth (PU)"
        self.composer_text = ""
        self.recent_messages = ["older message"]

    def _find_window(self):
        return {"hwnd": 1, "title": "WhatsApp", "rect": self.window_state.rect}

    def _read_current_chat_name_raw(self, window_state):
        return self.chat_name, "fake"

    def _collect_recent_message_texts(self, window_state, *, limit: int):
        return list(self.recent_messages)[-limit:]

    def _read_message_input_text(self, window_state):
        return self.composer_text


def setup_function():
    settings.reset_defaults()
    state.last_message_target = ""
    state.whatsapp_active = False


def test_verify_message_delivery_accepts_new_visible_message():
    skill = _VerificationSkill()

    verified, reason = skill._verify_message_delivery_once("hemanth", "hi", ["older message"])

    assert verified is False
    assert reason == "message_not_visible"

    skill.recent_messages = ["older message", "hi"]
    verified, reason = skill._verify_message_delivery_once("hemanth", "hi", ["older message"])

    assert verified is True
    assert reason == ""


def test_verify_message_delivery_rejects_wrong_chat_or_unsent_composer_text():
    skill = _VerificationSkill()
    skill.chat_name = "Charan"

    verified, reason = skill._verify_message_delivery_once("hemanth", "hi", ["older message"])

    assert verified is False
    assert reason.startswith("chat_mismatch")

    skill.chat_name = "Hemanth"
    skill.composer_text = "hi"
    verified, reason = skill._verify_message_delivery_once("hemanth", "hi", ["older message"])

    assert verified is False
    assert reason == "message_still_in_composer"


def test_do_send_message_fails_honestly_when_delivery_cannot_be_verified(monkeypatch):
    skill = _VerificationSkill()

    monkeypatch.setattr(skill, "_wait_for_expected_chat", lambda contact, timeout: skill.window_state)
    monkeypatch.setattr(skill, "_wait_for_message_delivery", lambda contact, message, before_messages, timeout: (False, "message_not_visible"))

    success = skill._do_send_message("hemanth", "hi")

    assert success is False
