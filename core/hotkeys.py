"""
Global shortcut manager for assistant shortcuts.
"""
from __future__ import annotations

try:
    import keyboard
except ImportError:  # pragma: no cover - optional dependency
    keyboard = None

from core.config_schema import normalize_hotkey
from core.logger import get_logger
from core import settings

logger = get_logger(__name__)

class HotkeyManager:
    def __init__(self):
        self.hotkey_name = settings.get("push_to_talk_hotkey") or settings.get("hotkey")
        self.start_stop_hotkey = settings.get("start_stop_listening_hotkey")
        self.open_assistant_hotkey = settings.get("open_assistant_hotkey")
        self.mode = settings.get("push_to_talk_mode")
        self._on_start = None
        self._on_stop = None
        self._on_toggle = None
        self._on_open = None
        self._is_pressed = False
        self._registered_hooks: list[object] = []
        self._registered_hotkeys: list[object] = []

    def register_push_to_talk(self, callback_start, callback_stop, callback_toggle=None, callback_open=None):
        """Register all configured global hotkeys."""
        self._on_start = callback_start
        self._on_stop = callback_stop
        self._on_toggle = callback_toggle
        self._on_open = callback_open

        if keyboard is None:
            logger.warning("keyboard library is not installed; global hotkeys are unavailable.")
            return

        self.unregister_all()

        try:
            self.hotkey_name = normalize_hotkey(settings.get("push_to_talk_hotkey") or settings.get("hotkey"))
            self.start_stop_hotkey = normalize_hotkey(settings.get("start_stop_listening_hotkey"))
            self.open_assistant_hotkey = normalize_hotkey(settings.get("open_assistant_hotkey"))
        except Exception as exc:
            logger.warning("Invalid hotkey rejected during registration: %s", exc)
            return

        try:
            push_hotkey = self._keyboard_hotkey(self.hotkey_name)
            if self.mode == "hold":
                main_key = self._keyboard_main_key(self.hotkey_name)
                self._registered_hooks.append(
                    keyboard.on_press_key(main_key, self._handle_press, suppress=False)
                )
                self._registered_hooks.append(
                    keyboard.on_release_key(main_key, self._handle_release, suppress=False)
                )
            else:
                self._registered_hotkeys.append(
                    keyboard.add_hotkey(push_hotkey, self._handle_toggle, suppress=False)
                )

            if self._on_toggle:
                self._registered_hotkeys.append(
                    keyboard.add_hotkey(
                        self._keyboard_hotkey(self.start_stop_hotkey),
                        self._handle_start_stop,
                        suppress=False,
                    )
                )
            if self._on_open:
                self._registered_hotkeys.append(
                    keyboard.add_hotkey(
                        self._keyboard_hotkey(self.open_assistant_hotkey),
                        self._handle_open,
                        suppress=False,
                    )
                )

            logger.info(
                "Hotkeys registered: push_to_talk=%s mode=%s start_stop=%s open_assistant=%s",
                self.hotkey_name,
                self.mode,
                self.start_stop_hotkey if self._on_toggle else "disabled",
                self.open_assistant_hotkey if self._on_open else "disabled",
            )
        except Exception as exc:
            logger.warning("Hotkey registration conflict or failure: %s", exc)
            self.unregister_all()

    def _handle_press(self, event):
        """Called repeatedly while the key is held down."""
        if keyboard is None:
            return
        # Ensure modifying keys match if needed, but for simplicity with keyboard library,
        # checking keyboard.is_pressed handles complex combinations best.
        if keyboard.is_pressed(self._keyboard_hotkey(self.hotkey_name)):
            if not self._is_pressed:
                self._is_pressed = True
                if self._on_start:
                    self._on_start()

    def _handle_release(self, event):
        """Called when the key is released."""
        if keyboard is None:
            return
        if self._is_pressed:
            # If any part of the hotkey is released, stop
            if not keyboard.is_pressed(self._keyboard_hotkey(self.hotkey_name)):
                self._is_pressed = False
                if self._on_stop:
                    self._on_stop()

    def _handle_toggle(self):
        """Called on hotkey press in toggle mode."""
        self._is_pressed = not self._is_pressed
        if self._is_pressed:
            if self._on_start:
                self._on_start()
        else:
            if self._on_stop:
                self._on_stop()

    def _handle_start_stop(self):
        if self._on_toggle:
            self._on_toggle()

    def _handle_open(self):
        if self._on_open:
            self._on_open()

    def unregister_all(self):
        """Unregister hotkeys owned by this manager."""
        if keyboard is None:
            return
        for handle in list(self._registered_hotkeys):
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                logger.debug("Failed to remove hotkey handle", exc_info=True)
        self._registered_hotkeys.clear()

        for handle in list(self._registered_hooks):
            try:
                keyboard.unhook(handle)
            except Exception:
                logger.debug("Failed to remove key hook", exc_info=True)
        self._registered_hooks.clear()
        logger.info("Hotkeys unregistered")

    @staticmethod
    def _keyboard_hotkey(hotkey: str) -> str:
        return str(hotkey).replace("Ctrl", "ctrl").replace("Alt", "alt").replace("Shift", "shift").replace("Win", "windows").lower()

    @classmethod
    def _keyboard_main_key(cls, hotkey: str) -> str:
        return cls._keyboard_hotkey(hotkey).split("+")[-1]
