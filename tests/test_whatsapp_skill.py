from __future__ import annotations

import pytest

from core import settings, state
from skills.whatsapp import WhatsAppContactMatch, WhatsAppSkill, WhatsAppWindowState


def _match(name: str, subtitle: str = "", unread_count: int = 0) -> WhatsAppContactMatch:
    return WhatsAppContactMatch(display_name=name, subtitle=subtitle, unread_count=unread_count, rect=(0, 0, 10, 10))


class FakeWhatsAppSkill(WhatsAppSkill):
    def __init__(self) -> None:
        super().__init__(desktop_factory=lambda: None)
        self.window_state = WhatsAppWindowState(
            hwnd=1,
            title="WhatsApp",
            process_name="whatsapp.exe",
            rect=(0, 0, 1200, 900),
            source="desktop",
            is_foreground=True,
            is_minimized=False,
        )
        self.lookup_table: dict[str, list[WhatsAppContactMatch]] = {}
        self.unread_items: list[WhatsAppContactMatch] = []
        self.current_chat = ""
        self.sent_messages: list[tuple[str, str]] = []
        self.clicked_buttons: list[str] = []
        self.search_queries: list[str] = []
        self.direct_search_table: dict[str, str] = {}
        self.applied_search_queries: list[str] = []
        self.pressed_keys: list[str] = []
        self.recent_messages: list[str] = []
        self._active_search_query = ""
        self._automation = _FakeAutomation(self)

    def focus_whatsapp(self):
        state.whatsapp_active = True
        return self.window_state

    def detect_whatsapp_context(self):
        state.whatsapp_active = True
        return self.window_state

    def _lookup_contacts(self, contact_name: str, *, window_state=None):
        self.search_queries.append(contact_name)
        matches = [
            WhatsAppContactMatch(
                display_name=item.display_name,
                subtitle=item.subtitle,
                unread_count=item.unread_count,
                rect=item.rect,
                raw_text=item.raw_text,
            )
            for item in self.lookup_table.get(contact_name.lower(), [])
        ]
        for idx, item in enumerate(matches, 1):
            item.index = idx
        return self.window_state, matches, None

    def _read_sidebar_matches(self, window_state, *, query: str):
        if query:
            return [
                WhatsAppContactMatch(
                    display_name=item.display_name,
                    subtitle=item.subtitle,
                    unread_count=item.unread_count,
                    rect=item.rect,
                    raw_text=item.raw_text,
                )
                for item in self.lookup_table.get(query.lower(), [])
            ]
        return [
            WhatsAppContactMatch(
                display_name=item.display_name,
                subtitle=item.subtitle,
                unread_count=item.unread_count,
                rect=item.rect,
                raw_text=item.raw_text,
            )
            for item in self.unread_items
        ]

    def _open_contact_match(self, window_state, match):
        self.current_chat = match.display_name
        state.last_chat_name = match.display_name
        return True

    def _click_header_button(self, window_state, *, button_type: str) -> bool:
        self.clicked_buttons.append(button_type)
        return True

    def _verify_call_started(self, window_state, *, expected_name: str) -> bool:
        return True

    def _send_message_in_active_chat(self, window_state, message_text: str) -> bool:
        self.sent_messages.append((self.current_chat, message_text))
        return True

    def _read_current_chat_name_raw(self, window_state):
        return self.current_chat, "fake"

    def _collect_recent_message_texts(self, window_state, *, limit: int):
        return self.recent_messages[-limit:]

    def _apply_search_query(self, window_state, query: str) -> bool:
        self.applied_search_queries.append(query)
        self._active_search_query = query.lower()
        return True

    def _search_contact_keyboard(self, window_state, contact_name: str) -> bool:
        self._active_search_query = contact_name.lower()
        return True

    def _wait_for_chat_name(self, window_state, *, expected_names):
        if self._active_search_query in self.direct_search_table:
            self.current_chat = self.direct_search_table[self._active_search_query]
            state.last_chat_name = self.current_chat
            return self.window_state, self.current_chat, True
        return self.window_state, "", True


class _FakeAutomation:
    def __init__(self, owner: FakeWhatsAppSkill) -> None:
        self.owner = owner

    def press_key(self, key: str) -> bool:
        self.owner.pressed_keys.append(key)
        return True

    def hotkey(self, keys) -> bool:
        return True

    def type_text(self, text: str, *, clear: bool = False, delay_ms: int | None = None) -> bool:
        return True

    def fast_sleep(self, _ms: int) -> None:
        return None


@pytest.fixture(autouse=True)
def reset_whatsapp_state():
    settings.reset_defaults()
    state.whatsapp_active = False
    state.last_contact_search = {}
    state.pending_contact_choices = []
    state.last_message_target = ""
    state.last_chat_name = ""
    yield
    settings.reset_defaults()


def test_can_handle_call_command_without_current_context():
    skill = FakeWhatsAppSkill()

    can_handle = skill.can_handle(
        {"current_app": "unknown", "current_process_name": "", "context_target_app": ""},
        "unknown",
        "call hemanth",
    )

    assert can_handle is True


def test_duplicate_contact_call_prompts_for_choice():
    skill = FakeWhatsAppSkill()
    skill.lookup_table["hemanth"] = [_match("Hemanth"), _match("Hemanth", "Office"), _match("Hemanth", "Family")]

    result = skill.call_contact("Hemanth")

    assert result.success is True
    assert result.intent == "whatsapp_disambiguation"
    assert "which one" in result.response.lower()
    assert len(state.pending_contact_choices) == 3


def test_follow_up_selection_uses_pending_contact_choices():
    skill = FakeWhatsAppSkill()
    skill.lookup_table["hemanth"] = [_match("Hemanth"), _match("Hemanth", "Office")]

    first = skill.call_contact("Hemanth")
    follow_up = skill.execute("call the second one", context={"current_app": "unknown"})

    assert first.intent == "whatsapp_disambiguation"
    assert follow_up.success is True
    assert follow_up.intent == "whatsapp_call"
    assert follow_up.response == "Calling Hemanth"
    assert skill.clicked_buttons[-1] == "voice_call"


def test_message_command_resolves_multi_word_contact_name():
    skill = FakeWhatsAppSkill()
    skill.lookup_table["project"] = []
    skill.lookup_table["project team"] = [_match("Project Team", "Group")]

    result = skill.execute("message project team meeting at 5", context={"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "whatsapp_message"
    assert result.response == "Message sent to Project Team"
    assert skill.sent_messages == [("Project Team", "meeting at 5")]
    assert state.last_message_target == "Project Team"


def test_call_contact_uses_direct_search_before_sidebar_lookup():
    skill = FakeWhatsAppSkill()
    skill.direct_search_table["mohit"] = "Mohit"

    result = skill.call_contact("Mohit")

    assert result.success is True
    assert result.intent == "whatsapp_call"
    assert result.response == "Calling Mohit"
    assert skill.clicked_buttons[-1] == "voice_call"
    assert skill.search_queries == []


def test_send_message_uses_direct_search_when_sidebar_lookup_is_empty():
    skill = FakeWhatsAppSkill()
    skill.direct_search_table["mohit"] = "Mohit"

    result = skill.send_message("Mohit", "ok")

    assert result.success is True
    assert result.intent == "whatsapp_message"
    assert result.response == "Sent to Mohit"
    assert skill.sent_messages == [("Mohit", "ok")]
    assert skill.search_queries == []


def test_read_unread_chats_reports_visible_unread_names_only():
    skill = FakeWhatsAppSkill()
    skill.unread_items = [_match("Hemanth", unread_count=2), _match("Team Group", unread_count=1), _match("Read Chat", unread_count=0)]

    result = skill.read_unread_chats()

    assert result.success is True
    assert "Hemanth" in result.response
    assert "Team Group" in result.response
    assert "Read Chat" not in result.response


def test_read_current_chat_name_uses_live_chat_context():
    skill = FakeWhatsAppSkill()
    skill.current_chat = "Rakesh"

    result = skill.read_current_chat_name()

    assert result.success is True
    assert result.response == "Current WhatsApp chat: Rakesh"
    assert state.last_chat_name == "Rakesh"
