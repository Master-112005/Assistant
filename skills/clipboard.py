"""
Clipboard history skill.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from core import state
from core.clipboard import ClipboardItem, ClipboardManager
from core.clipboard_store import ClipboardStoreError
from core.logger import get_logger
from core.text_utils import normalize_command
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

_YES_WORDS = {"yes", "y", "confirm", "continue", "proceed", "do it", "okay", "ok"}
_NO_WORDS = {"no", "n", "cancel", "stop", "never mind", "dont", "don't"}
_ORDINALS = {
    "first": 1,
    "1": 1,
    "1st": 1,
    "one": 1,
    "second": 2,
    "2": 2,
    "2nd": 2,
    "two": 2,
    "third": 3,
    "3": 3,
    "3rd": 3,
    "three": 3,
    "fourth": 4,
    "4": 4,
    "4th": 4,
    "four": 4,
    "fifth": 5,
    "5": 5,
    "5th": 5,
    "five": 5,
    "sixth": 6,
    "6": 6,
    "6th": 6,
    "six": 6,
    "seventh": 7,
    "7": 7,
    "7th": 7,
    "seven": 7,
    "eighth": 8,
    "8": 8,
    "8th": 8,
    "eight": 8,
    "ninth": 9,
    "9": 9,
    "9th": 9,
    "nine": 9,
    "tenth": 10,
    "10": 10,
    "10th": 10,
    "ten": 10,
}


class ClipboardSkill(SkillBase):
    """Natural-language clipboard monitoring and history actions."""

    def __init__(self, *, clipboard_manager: ClipboardManager | None = None) -> None:
        self.manager = clipboard_manager or ClipboardManager()
        self._confirm_cb: Callable[[str], bool] | None = None

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        self._confirm_cb = callback

    def start(self) -> None:
        self.manager.start_watcher()

    def shutdown(self) -> None:
        self.manager.shutdown()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        normalized = self._normalize(command)
        if not normalized:
            return False

        if self._pending_request() is not None:
            return normalized in _YES_WORDS or normalized in _NO_WORDS

        if self._has_pending_choices() and (self._extract_ordinal(normalized) is not None or normalized in _NO_WORDS):
            return True

        return any(
            check(normalized)
            for check in (
                self._is_last_query,
                self._is_history_query,
                self._is_restore_query,
                self._is_clear_current_query,
                self._is_clear_history_query,
            )
        )

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        normalized = self._normalize(command)

        pending = self._pending_request()
        if pending is not None:
            return self._handle_pending_request(normalized, pending)

        if self._has_pending_choices() and normalized in _NO_WORDS:
            state.pending_clipboard_choices = []
            return self._cancelled("Cancelled the pending clipboard selection.")

        if self._has_pending_choices() and self._extract_ordinal(normalized) is not None:
            return self._restore_selection(normalized)

        if self._is_last_query(normalized):
            self.manager.capture_change()
            logger.info("Clipboard history queried: last item")
            return self._respond_with_last_item()

        if self._is_history_query(normalized):
            self.manager.capture_change()
            limit = self._extract_history_limit(normalized)
            logger.info("Clipboard history queried: limit=%s", limit)
            return self._respond_with_history(limit)

        if self._is_restore_query(normalized):
            logger.info("Clipboard restore requested")
            return self._restore_selection(normalized)

        if self._is_clear_history_query(normalized):
            return self._clear_history()

        if self._is_clear_current_query(normalized):
            cleared = self.manager.clear_current()
            if not cleared:
                return self._failure("I couldn't clear the current clipboard.", "clipboard_clear_failed")
            return SkillExecutionResult(
                success=True,
                intent="clipboard_clear",
                response="Cleared the current clipboard.",
                skill_name=self.name(),
                data={"target_app": "clipboard"},
            )

        return self._failure("I couldn't understand the clipboard command.", "clipboard_command_invalid")

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "clipboard",
            "supports": [
                "history",
                "restore",
                "clear_current",
                "clear_history",
                "sensitive_masking",
            ],
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "target": "clipboard",
            "enabled": True,
            "watcher_running": bool(getattr(self.manager, "_watcher_thread", None) and self.manager._watcher_thread.is_alive()),
            "history_count": int(getattr(state, "clipboard_count", 0) or 0),
            "pending_choices": bool(self._has_pending_choices()),
        }

    @staticmethod
    def _normalize(text: str) -> str:
        return normalize_command(text)

    @staticmethod
    def _is_last_query(normalized: str) -> bool:
        return normalized in {
            "what did i copy last",
            "what was copied last",
            "what is on my clipboard",
            "what is on the clipboard",
        }

    @staticmethod
    def _is_history_query(normalized: str) -> bool:
        if normalized in {
            "show clipboard history",
            "show recent clipboard items",
            "show recent clipboard",
            "show copied items",
        }:
            return True
        return bool(
            re.fullmatch(r"show last (?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) copied items?", normalized)
            or re.fullmatch(r"show last (?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) clipboard items?", normalized)
        )

    @staticmethod
    def _is_restore_query(normalized: str) -> bool:
        if normalized in {
            "copy last item again",
            "copy last clipboard item again",
            "copy last item",
            "restore last item",
            "restore clipboard item",
        }:
            return True
        return bool(
            re.fullmatch(r"(?:restore|copy) (?:the )?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+)(?: clipboard)? item(?: again)?", normalized)
            or re.fullmatch(r"(?:restore|copy) (?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+) one", normalized)
        )

    @staticmethod
    def _is_clear_current_query(normalized: str) -> bool:
        return normalized in {"clear clipboard", "clear current clipboard", "empty clipboard"}

    @staticmethod
    def _is_clear_history_query(normalized: str) -> bool:
        return normalized in {"clear clipboard history", "delete clipboard history", "erase clipboard history"}

    @staticmethod
    def _extract_ordinal(normalized: str) -> int | None:
        for token in normalized.split():
            if token in _ORDINALS:
                return _ORDINALS[token]
        match = re.search(r"\b(\d{1,2})\b", normalized)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _extract_history_limit(normalized: str) -> int:
        match = re.search(r"show last (\d{1,2})", normalized)
        if match:
            return max(1, min(50, int(match.group(1))))
        for word, value in _ORDINALS.items():
            if re.search(rf"\bshow last {re.escape(word)}\b", normalized):
                return value
        return 10

    def _respond_with_last_item(self) -> SkillExecutionResult:
        item = self.manager.get_last()
        if item is None:
            state.pending_clipboard_choices = []
            return self._failure("Clipboard history is empty.", "clipboard_history_empty")

        state.pending_clipboard_choices = [item.to_dict()]
        if item.is_sensitive and item.full_text is None:
            response = f"You last copied a sensitive item. Stored preview: {item.text_preview}"
        else:
            response = f"You last copied: {item.text_preview}"
        return SkillExecutionResult(
            success=True,
            intent="clipboard_last",
            response=response,
            skill_name=self.name(),
            data={"target_app": "clipboard", "item": item.to_dict()},
        )

    def _respond_with_history(self, limit: int) -> SkillExecutionResult:
        items = self.manager.get_recent(limit)
        if not items:
            state.pending_clipboard_choices = []
            return self._failure("Clipboard history is empty.", "clipboard_history_empty")

        state.pending_clipboard_choices = [item.to_dict() for item in items]
        lines = [f"{index}. {item.text_preview}" for index, item in enumerate(items, start=1)]
        return SkillExecutionResult(
            success=True,
            intent="clipboard_history",
            response="\n".join(lines),
            skill_name=self.name(),
            data={"target_app": "clipboard", "items": [item.to_dict() for item in items]},
        )

    def _restore_selection(self, normalized: str) -> SkillExecutionResult:
        if normalized in {"copy last item again", "copy last clipboard item again", "copy last item", "restore last item", "restore clipboard item"}:
            item = self.manager.get_last()
            ordinal = None
        else:
            ordinal = self._extract_ordinal(normalized)
            item = self._resolve_choice_item(ordinal)

        if item is None:
            return self._failure("I couldn't find that clipboard item.", "clipboard_item_not_found")

        if item.full_text is None:
            message = (
                "The last clipboard item was marked sensitive and only a masked preview was stored, so it cannot be restored."
                if ordinal is None
                else f"Item {ordinal} was marked sensitive and only a masked preview was stored, so it cannot be restored."
            )
            return self._failure(
                message,
                "clipboard_item_not_restorable",
            )

        restored = self.manager.restore(int(item.id or 0))
        if restored is None:
            return self._failure("I couldn't restore that clipboard item.", "clipboard_restore_failed")

        if ordinal is None:
            response = "Copied the last clipboard item back to the clipboard."
        else:
            response = f"Copied item {ordinal} back to the clipboard."
        return SkillExecutionResult(
            success=True,
            intent="clipboard_restore",
            response=response,
            skill_name=self.name(),
            data={"target_app": "clipboard", "item": restored.to_dict()},
        )

    def _resolve_choice_item(self, ordinal: int | None) -> ClipboardItem | None:
        if ordinal is None or ordinal <= 0:
            return None

        pending = list(getattr(state, "pending_clipboard_choices", []) or [])
        if pending and ordinal <= len(pending):
            item_id = pending[ordinal - 1].get("id")
            if item_id:
                try:
                    return self.manager.store.get_by_id(int(item_id))
                except ClipboardStoreError:
                    return None

        recent = self.manager.get_recent(max(ordinal, 10))
        if 1 <= ordinal <= len(recent):
            return recent[ordinal - 1]
        return None

    def _clear_history(self) -> SkillExecutionResult:
        count = int(getattr(state, "clipboard_count", 0) or 0)
        prompt = f"Clear clipboard history and delete {count} saved item{'s' if count != 1 else ''}?"

        if self._confirm_cb is not None:
            if self._confirm_cb(prompt):
                removed = self.manager.clear_history()
                return SkillExecutionResult(
                    success=True,
                    intent="clipboard_history_clear",
                    response=f"Cleared clipboard history. Removed {removed} item{'s' if removed != 1 else ''}.",
                    skill_name=self.name(),
                    data={"target_app": "clipboard", "removed": removed},
                )
            return self._cancelled("Cancelled clearing clipboard history.")

        self._store_pending_request({"skill": "clipboard", "kind": "clear_history", "prompt": prompt})
        return SkillExecutionResult(
            success=False,
            intent="clipboard_history_clear",
            response=f"{prompt} Say yes to continue or no to cancel.",
            skill_name=self.name(),
            error="confirmation_required",
            data={"target_app": "clipboard"},
        )

    def _pending_request(self) -> dict[str, Any] | None:
        pending = getattr(state, "pending_confirmation", {}) or {}
        if pending.get("skill") != "clipboard":
            return None
        return dict(pending)

    def _handle_pending_request(self, normalized: str, pending: dict[str, Any]) -> SkillExecutionResult:
        if normalized in _NO_WORDS:
            self._clear_pending_request()
            return self._cancelled("Cancelled clearing clipboard history.")

        if normalized not in _YES_WORDS:
            return SkillExecutionResult(
                success=False,
                intent="clipboard_history_clear",
                response=str(pending.get("prompt") or "Please answer yes or no."),
                skill_name=self.name(),
                error="confirmation_required",
                data={"target_app": "clipboard"},
            )

        self._clear_pending_request()
        removed = self.manager.clear_history()
        return SkillExecutionResult(
            success=True,
            intent="clipboard_history_clear",
            response=f"Cleared clipboard history. Removed {removed} item{'s' if removed != 1 else ''}.",
            skill_name=self.name(),
            data={"target_app": "clipboard", "removed": removed},
        )

    def _store_pending_request(self, payload: dict[str, Any]) -> None:
        state.pending_confirmation = payload

    def _clear_pending_request(self) -> None:
        pending = getattr(state, "pending_confirmation", {}) or {}
        if pending.get("skill") == "clipboard":
            state.pending_confirmation = {}

    @staticmethod
    def _has_pending_choices() -> bool:
        return bool(getattr(state, "pending_clipboard_choices", []) or [])

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="clipboard",
            response=response,
            skill_name=self.name(),
            error=error,
            data={"target_app": "clipboard"},
        )

    def _cancelled(self, response: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="clipboard",
            response=response,
            skill_name=self.name(),
            error="cancelled",
            data={"target_app": "clipboard"},
        )
