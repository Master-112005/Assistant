"""
Main application window.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import settings, state
from core.audio import LiveAudioListener
from core.context import context_manager as _ctx_mgr
from core.errors import DeviceUnavailableError
from core.hotkeys import HotkeyManager
from core.logger import get_logger
from core.notifications import Notification, NotificationManager
from core.processor import CommandProcessor
from core.response_service import initialize_response_service
from core.runtime_tasks import drain_runtime_threads, invoke_with_timeout
from core.stt import SpeechToTextEngine
from core.theme_manager import theme_manager
from core.tts import TextToSpeechEngine
from core.window_context import WindowInfo
from ui.animations import fade_in, stop_animations
from ui.dialogs import SettingsDialog
from ui.floating_orb import FloatingOrb
from ui.sidebar import Sidebar
from ui.toast import NotificationCenterWidget, ToastOverlay
from ui.widgets import ChatBubble, StatusIndicator

logger = get_logger(__name__)

# Friendly display names for the context label in the UI header
_CONTEXT_DISPLAY_NAMES: dict[str, str] = {
    "chrome":       "Chrome",
    "edge":         "Edge",
    "firefox":      "Firefox",
    "brave":        "Brave",
    "opera":        "Opera",
    "vivaldi":      "Vivaldi",
    "youtube":      "YouTube",
    "netflix":      "Netflix",
    "twitch":       "Twitch",
    "spotify":      "Spotify",
    "whatsapp":     "WhatsApp",
    "telegram":     "Telegram",
    "discord":      "Discord",
    "slack":        "Slack",
    "teams":        "Teams",
    "zoom":         "Zoom",
    "explorer":     "File Explorer",
    "vscode":       "VS Code",
    "pycharm":      "PyCharm",
    "notepad":      "Notepad",
    "word":         "Word",
    "excel":        "Excel",
    "powerpoint":   "PowerPoint",
    "terminal":     "Terminal",
    "cmd":          "Command Prompt",
    "powershell":   "PowerShell",
    "gmail":        "Gmail",
    "outlook":      "Outlook",
    "github":       "GitHub",
    "chatgpt":      "ChatGPT",
    "claude":       "Claude",
    "task_manager": "Task Manager",
    "unknown":      "Unknown",
}


class STTWorker(QThread):
    transcription_ready = Signal(str, bool, int)
    state_changed = Signal(str)

    def __init__(self, stt_engine: SpeechToTextEngine, audio_data, token: int, parent=None):
        super().__init__(parent)
        self.stt_engine = stt_engine
        self.audio_data = audio_data
        self.token = int(token)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            self.state_changed.emit("PROCESSING")
            timeout_seconds = float(settings.get("stt_timeout_seconds") or 10)
            outcome = invoke_with_timeout(
                lambda: self.stt_engine.transcribe(audio_data=self.audio_data),
                timeout_seconds=timeout_seconds,
                cancel_event=self._cancel_event,
            )
            if outcome.cancelled:
                self.state_changed.emit("READY")
                return
            if outcome.timed_out:
                self.transcription_ready.emit(
                    f"Speech recognition timed out after {int(timeout_seconds)} seconds.",
                    False,
                    self.token,
                )
                self.state_changed.emit("ERROR")
                return
            if outcome.error is not None:
                raise outcome.error
            result = outcome.value if isinstance(outcome.value, dict) else {}
            self.transcription_ready.emit(str(result.get("text", "") or ""), True, self.token)
            self.state_changed.emit("PROCESSING")  # Will transition to EXECUTING after text processing
        except Exception as exc:
            logger.error("STTWorker error: %s", exc)
            self.transcription_ready.emit(str(exc), False, self.token)
            self.state_changed.emit("ERROR")


class CommandWorker(QThread):
    result_ready = Signal(dict, int)
    error_ready = Signal(str, int)
    cancelled = Signal(int)
    state_changed = Signal(str)

    def __init__(self, processor: CommandProcessor, text: str, token: int, source: str = "text", parent=None):
        super().__init__(parent)
        self.processor = processor
        self.text = text
        self.token = int(token)
        self.source = source
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            self.state_changed.emit("EXECUTING")
            timeout_seconds = float(settings.get("command_timeout_seconds") or 20)
            outcome = invoke_with_timeout(
                lambda: self.processor.process(self.text, source=self.source),
                timeout_seconds=timeout_seconds,
                cancel_event=self._cancel_event,
            )
            if outcome.cancelled:
                self.state_changed.emit("READY")
                self.cancelled.emit(self.token)
                return
            if outcome.timed_out:
                result = self.processor.handle_external_error(
                    {
                        "message": f"The command '{self.text}' timed out after {int(timeout_seconds)} seconds.",
                        "error": "action_timeout",
                        "intent": "action_timeout",
                        "recoverable": True,
                        "timeout_seconds": timeout_seconds,
                        "command": self.text,
                    },
                    command_context={
                        "command": self.text,
                        "raw_input": self.text,
                        "normalized_input": self.text,
                        "detected_intent": "action_timeout",
                        "intent": "action_timeout",
                        "timeout_seconds": timeout_seconds,
                    },
                    source=self.source,
                )
                if self._cancel_event.is_set():
                    self.state_changed.emit("READY")
                    self.cancelled.emit(self.token)
                    return
                self.result_ready.emit(result, self.token)
                self.state_changed.emit("READY")
                return
            if outcome.error is not None:
                raise outcome.error
            if self._cancel_event.is_set():
                self.state_changed.emit("READY")
                self.cancelled.emit(self.token)
                return
            result = outcome.value if isinstance(outcome.value, dict) else {}
            self.result_ready.emit(result, self.token)
            self.state_changed.emit("READY")
        except Exception as exc:
            logger.exception("CommandWorker error", exc=exc, command=self.text, source=self.source)
            if self._cancel_event.is_set():
                self.state_changed.emit("READY")
                self.cancelled.emit(self.token)
                return
            try:
                result = self.processor.handle_external_error(
                    exc,
                    command_context={
                        "command": self.text,
                        "raw_input": self.text,
                        "normalized_input": self.text,
                        "detected_intent": "runtime_error",
                        "intent": "runtime_error",
                    },
                    source=self.source,
                )
            except Exception as recovery_exc:
                logger.exception(
                    "CommandWorker recovery failed",
                    exc=recovery_exc,
                    command=self.text,
                    source=self.source,
                )
                self.error_ready.emit(str(exc), self.token)
                return
            if self._cancel_event.is_set():
                self.cancelled.emit(self.token)
                return
            self.result_ready.emit(result, self.token)


class ExecutionProgressWorker(QThread):
    """
    Worker thread that drives the ExecutionEngine for a multi-step plan.

    Signals:
        step_progress(str)  â€” emitted after each step with a status message
        finished(dict)      â€” emitted when the plan is complete
        failed(str)         â€” emitted if an unhandled exception occurs
    """
    progress_changed = Signal(str)
    result_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self, processor, plan, parent=None):
        super().__init__(parent)
        self._processor = processor
        self._plan      = plan

    def run(self) -> None:
        try:
            # Wire progress updates to our signal (thread-safe via Qt signal)
            self._processor.set_progress_callback(self.progress_changed.emit)
            exec_result = self._processor.engine.execute_plan(self._plan)
            result_dict = {
                "success":          exec_result.success,
                "intent":           "multi_action",
                "response":         exec_result.summary,
                "execution_result": exec_result,
            }
            self.result_ready.emit(result_dict)
        except Exception as exc:
            logger.error("ExecutionProgressWorker error: %s", exc)
            self.error_ready.emit(str(exc))


class MainWindow(QMainWindow):
    """Main floating assistant window."""


    sig_start_listening = Signal()
    sig_stop_listening = Signal()
    sig_notification_event = Signal(object)
    sig_listener_state_changed = Signal(str, object)
    sig_utterance_ready = Signal(object, object)
    sig_listener_error = Signal(str)
    sig_tts_event = Signal(str, str)
    sig_finalize_listening = Signal()
    sig_toggle_listening = Signal()
    sig_open_window = Signal()
    sig_context_changed = Signal(str, str)
    sig_confirm_request = Signal(str, object)
    sig_assistant_message = Signal(str, str)

    def __init__(
        self,
        processor: CommandProcessor | None = None,
        listener: LiveAudioListener | None = None,
        hotkey_mgr: HotkeyManager | None = None,
        stt_engine: SpeechToTextEngine | None = None,
        tts_engine: TextToSpeechEngine | None = None,
        notification_manager: NotificationManager | None = None,
    ) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self.processor = processor or CommandProcessor()
        self.listener = listener or LiveAudioListener(
            on_state_change=lambda name, payload: self.sig_listener_state_changed.emit(name, payload),
            on_utterance_ready=lambda audio, meta: self.sig_utterance_ready.emit(audio, meta),
            on_error=lambda message, exc=None: self.sig_listener_error.emit(
                f"{message}: {exc}" if exc else message
            ),
        )
        self.hotkey_mgr = hotkey_mgr or HotkeyManager()
        self.stt_engine = stt_engine or SpeechToTextEngine()
        self.tts = tts_engine or TextToSpeechEngine()
        self.notification_manager = notification_manager or getattr(self.processor, "notification_manager", None)
        self._owns_notification_manager = self.notification_manager is None
        if self.notification_manager is None:
            self.notification_manager = NotificationManager(tts_engine=self.tts)
        self.floating_orb: FloatingOrb | None = None
        self._message_history: list[tuple[str, str]] = []
        self.stt_worker: STTWorker | None = None
        self.command_worker: CommandWorker | None = None
        self.exec_worker: ExecutionProgressWorker | None = None
        self._command_token: int = 0
        self._active_command_token: int | None = None
        self._detached_command_workers: list[CommandWorker] = []
        self._detached_stt_workers: list[STTWorker] = []
        self._live_listener_enabled: bool = False
        self._tts_paused_listener: bool = False
        self._last_tts_payload: str = ""
        self._tts_state_bridge = lambda event, payload: self.sig_tts_event.emit(event, payload)
        self._status_reset_timer = QTimer(self)
        self._status_reset_timer.setSingleShot(True)
        self._status_reset_timer.timeout.connect(self._restore_ready_state)
        self._chat_scroll_timer = QTimer(self)
        self._chat_scroll_timer.setSingleShot(True)
        self._chat_scroll_timer.timeout.connect(self._scroll_chat_to_bottom)
        self._shutting_down = False
        self._context_watcher_active = False

        if settings.get("context_detection_enabled") and settings.get("show_current_app"):
            self._start_context_watcher()

        self.sig_assistant_message.connect(self.add_message)
        initialize_response_service(
            ui_callback=lambda sender, text: self.sig_assistant_message.emit(sender, text),
            tts_callback=self._queue_response_speech,
            notification_callback=self._dispatch_response_notification,
        )

        self.setWindowTitle(self.processor.identity_mgr.format_title())
        self.resize(920, 660)
        self.setMinimumSize(720, 500)
        theme_manager.apply(self)

        self.central_widget = QWidget()
        self.central_widget.setObjectName("AppRoot")
        self.setCentralWidget(self.central_widget)
        self.root_layout = QHBoxLayout(self.central_widget)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.sidebar = Sidebar(self.central_widget)
        self.sidebar.page_requested.connect(self._on_sidebar_page_requested)
        self.root_layout.addWidget(self.sidebar)

        self.content_panel = QFrame(self.central_widget)
        self.content_panel.setObjectName("ContentPanel")
        self.root_layout.addWidget(self.content_panel, stretch=1)

        self.main_layout = QVBoxLayout(self.content_panel)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(12)

        self._setup_ui()
        self._setup_floating_orb()
        self._sync_mute_button()
        self.toast_overlay = ToastOverlay(self.central_widget)
        self.toast_overlay.sync_to_parent()
        self._setup_signals()

        # Wire safety confirmation callback when the injected processor supports it.
        if hasattr(self.processor, "set_confirm_callback"):
            self.processor.set_confirm_callback(self._confirm_dangerous_action)
        if hasattr(self.processor, "set_notification_manager"):
            self.processor.set_notification_manager(self.notification_manager)
        if hasattr(self.processor, "set_tts_engine"):
            self.processor.set_tts_engine(self.tts)
        self.notification_manager.set_tts_engine(self.tts)
        self.notification_manager.register_in_app_handler(self._forward_notification_to_ui)
        self.notification_manager.start()
        if hasattr(self.processor, "start_background_services"):
            self.processor.start_background_services()

        logger.info("Main window initialized")

    def _setup_signals(self) -> None:
        self.sig_start_listening.connect(self.start_listening)
        self.sig_stop_listening.connect(self.stop_listening)
        self.sig_notification_event.connect(self._on_notification_event)
        self.sig_listener_state_changed.connect(self._on_listener_state_changed)
        self.sig_utterance_ready.connect(self._on_utterance_ready)
        self.sig_listener_error.connect(self._on_listener_error)
        self.sig_tts_event.connect(self._on_tts_event)
        self.sig_finalize_listening.connect(self.release_push_to_talk)
        self.sig_toggle_listening.connect(self.toggle_listening)
        self.sig_open_window.connect(self.show_and_raise)
        self.sig_context_changed.connect(self._on_context_changed)
        self.sig_confirm_request.connect(self._handle_confirm_request)
        if hasattr(self.tts, "subscribe_state_changes"):
            self.tts.subscribe_state_changes(self._tts_state_bridge)

        self.refresh_hotkeys(replace=False)

    def _start_context_watcher(self) -> None:
        if self._context_watcher_active:
            return

        self._context_watcher_active = True

        def _on_change(info: WindowInfo) -> None:
            if self._shutting_down or not self._context_watcher_active:
                return
            self.sig_context_changed.emit(info.app_id, info.title)

        _ctx_mgr.start_watcher(callback=_on_change)

    def _stop_context_watcher(self) -> None:
        self._context_watcher_active = False
        if _ctx_mgr.is_watching:
            _ctx_mgr.stop_watcher()

    def closeEvent(self, event) -> None:
        try:
            logger.info("Closing application...")
            self._shutting_down = True
            self._status_reset_timer.stop()
            self._chat_scroll_timer.stop()

            # SHUTDOWN SEQUENCE (orderly, no blocking on UI thread)
            # 1. Stop accepting new input
            self.hotkey_mgr.unregister_all()

            # 2. Disconnect TTS state changes
            if hasattr(self.tts, "unsubscribe_state_changes"):
                try:
                    self.tts.unsubscribe_state_changes(self._tts_state_bridge)
                except Exception:
                    logger.debug("Failed to unsubscribe TTS listener", exc_info=True)

            # 3. Stop listener (audio capture)
            if self.listener.is_active():
                try:
                    self.listener.stop()
                except Exception as e:
                    logger.warning("Listener stop failed: %s", e)

            # 4. Stop TTS immediately (non-blocking)
            try:
                self.tts.stop()
            except Exception as e:
                logger.warning("TTS stop failed: %s", e)

            # 5. Stop worker threads with proper cleanup
            # STT Worker
            if self.stt_worker and self.stt_worker.isRunning():
                try:
                    try:
                        self.stt_worker.transcription_ready.disconnect(self._on_stt_finished)
                        self.stt_worker.state_changed.disconnect(self._set_voice_state)
                        self.stt_worker.finished.disconnect(self._on_stt_thread_finished)
                    except Exception:
                        pass
                    self.stt_worker.cancel()
                    if not self.stt_worker.wait(1000):
                        logger.warning("STT worker did not stop in time")
                    self.stt_worker.deleteLater()
                    self.stt_worker = None
                except Exception as e:
                    logger.warning("STT worker shutdown failed: %s", e)

            # Command Worker
            if self.command_worker and self.command_worker.isRunning():
                try:
                    try:
                        self.command_worker.result_ready.disconnect(self._on_command_finished)
                        self.command_worker.error_ready.disconnect(self._on_command_failed)
                        self.command_worker.cancelled.disconnect(self._on_command_cancelled)
                        self.command_worker.state_changed.disconnect(self._set_voice_state)
                        self.command_worker.finished.disconnect(self._on_command_thread_finished)
                    except Exception:
                        pass
                    self.command_worker.cancel()
                    if not self.command_worker.wait(1000):
                        logger.warning("Command worker did not stop in time")
                    self.command_worker.deleteLater()
                    self.command_worker = None
                except Exception as e:
                    logger.warning("Command worker shutdown failed: %s", e)

            # Execution Progress Worker (CRITICAL FIX: must call quit() first!)
            if self.exec_worker and self.exec_worker.isRunning():
                try:
                    self.processor.engine.cancel()
                    self.exec_worker.requestInterruption()
                    if not self.exec_worker.wait(3000):
                        logger.warning("Execution worker did not stop in time")
                    self.exec_worker.deleteLater()
                    self.exec_worker = None
                except Exception as e:
                    logger.warning("Execution worker shutdown failed: %s", e)

            for worker in list(self._detached_stt_workers):
                try:
                    try:
                        worker.transcription_ready.disconnect(self._on_stt_finished)
                        worker.state_changed.disconnect(self._set_voice_state)
                        worker.finished.disconnect(self._on_stt_thread_finished)
                    except Exception:
                        pass
                    worker.cancel()
                    if not worker.wait(500):
                        logger.warning("Detached STT worker did not stop in time")
                    worker.deleteLater()
                except Exception as e:
                    logger.warning("Detached STT worker shutdown failed: %s", e)
            self._detached_stt_workers.clear()

            for worker in list(self._detached_command_workers):
                try:
                    try:
                        worker.result_ready.disconnect(self._on_command_finished)
                        worker.error_ready.disconnect(self._on_command_failed)
                        worker.cancelled.disconnect(self._on_command_cancelled)
                        worker.state_changed.disconnect(self._set_voice_state)
                        worker.finished.disconnect(self._on_command_thread_finished)
                    except Exception:
                        pass
                    worker.cancel()
                    if not worker.wait(500):
                        logger.warning("Detached command worker did not stop in time")
                    worker.deleteLater()
                except Exception as e:
                    logger.warning("Detached command worker shutdown failed: %s", e)
            self._detached_command_workers.clear()

            # Context watcher
            try:
                self._stop_context_watcher()
            except Exception as e:
                logger.warning("Context watcher shutdown failed: %s", e)

            # 6. Stop notification manager
            try:
                self.notification_manager.unregister_in_app_handler(self._forward_notification_to_ui)
                if self._owns_notification_manager:
                    self.notification_manager.stop()
            except Exception as e:
                logger.warning("Notification manager shutdown failed: %s", e)

            # 7. Close floating UI
            for widget in (
                getattr(self, "summary_widget", None),
                getattr(self, "toast_overlay", None),
                getattr(self, "notification_center", None),
                self.floating_orb,
                self,
            ):
                if widget is None:
                    continue
                try:
                    stop_animations(widget)
                except Exception as e:
                    logger.debug("Animation cleanup failed: %s", e)

            if self.floating_orb is not None:
                try:
                    self.floating_orb.hide_orb()
                    self.floating_orb.close()
                    self.floating_orb.deleteLater()
                    self.floating_orb = None
                except Exception as e:
                    logger.warning("Floating orb close failed: %s", e)

            # 8. Shutdown processor background services
            if hasattr(self.processor, "shutdown"):
                try:
                    self.processor.shutdown()
                except Exception as e:
                    logger.warning("Processor shutdown failed: %s", e)
            drain_runtime_threads(timeout_seconds=2.0)

            logger.info("Application shutdown complete")
            super().closeEvent(event)

        except KeyboardInterrupt:
            logger.warning("Keyboard interrupt during closeEvent - forcing exit")
            super().closeEvent(event)
        except Exception as e:
            logger.error("Unexpected error in closeEvent: %s", e, exc_info=True)
            super().closeEvent(event)

    def _setup_ui(self) -> None:
        header_layout = QHBoxLayout()
        self.title_label = QLabel(self.processor.identity_mgr.format_title())
        self.title_label.setObjectName("TitleLabel")

        # â”€â”€ Phase 13: Current App context label â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.context_app_label = QLabel("unknown")
        self.context_app_label.setObjectName("ContextAppLabel")
        self.context_app_label.setToolTip("Currently focused application")
        if not settings.get("show_current_app"):
            self.context_app_label.hide()

        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setFixedHeight(32)
        self.mute_btn.setMinimumWidth(64)
        self.mute_btn.setCheckable(True)
        self.mute_btn.setChecked(settings.get("mute"))
        self.mute_btn.clicked.connect(self.toggle_mute)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setFixedHeight(32)
        self.settings_btn.setMinimumWidth(84)
        self.settings_btn.setObjectName("SecondaryButton")
        self.settings_btn.clicked.connect(self.open_settings)

        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.context_app_label)   # Phase 13
        header_layout.addStretch()
        header_layout.addWidget(self.mute_btn)
        header_layout.addWidget(self.settings_btn)
        self.main_layout.addLayout(header_layout)

        line = QFrame()
        line.setObjectName("Divider")
        self.main_layout.addWidget(line)

        self.page_stack = QStackedWidget()
        self.main_layout.addWidget(self.page_stack, stretch=1)

        self.chat_page = QWidget()
        self.chat_page_layout = QVBoxLayout(self.chat_page)
        self.chat_page_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_page_layout.setSpacing(12)

        self.notification_center = NotificationCenterWidget(self)
        self.chat_page_layout.addWidget(self.notification_center)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_widget = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_widget)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_scroll.setWidget(self.chat_widget)
        self.chat_page_layout.addWidget(self.chat_scroll, stretch=1)
        self.page_stack.addWidget(self.chat_page)

        self.summary_page = QScrollArea()
        self.summary_page.setWidgetResizable(True)
        self.summary_widget = QWidget()
        self.summary_layout = QVBoxLayout(self.summary_widget)
        self.summary_layout.setContentsMargins(0, 0, 0, 0)
        self.summary_layout.setSpacing(10)
        self.summary_page.setWidget(self.summary_widget)
        self.page_stack.addWidget(self.summary_page)

        input_layout = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Type a command...")
        self.text_input.returnPressed.connect(self.send_message)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)

        input_layout.addWidget(self.text_input)
        input_layout.addWidget(self.send_btn)
        self.main_layout.addLayout(input_layout)

        controls_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Listening")
        self.start_btn.clicked.connect(self.start_listening)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("SecondaryButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_listening)

        self.cancel_exec_btn = QPushButton("Cancel")
        self.cancel_exec_btn.setObjectName("SecondaryButton")
        self.cancel_exec_btn.setEnabled(False)
        self.cancel_exec_btn.setToolTip("Stop the current utterance, task, or speech")
        self.cancel_exec_btn.clicked.connect(self.cancel_execution)

        controls_layout.addWidget(self.start_btn)
        controls_layout.addWidget(self.stop_btn)
        controls_layout.addWidget(self.cancel_exec_btn)
        self.main_layout.addLayout(controls_layout)

        # â”€â”€ Phase 12: Execution progress label â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.exec_progress_label = QLabel("")
        self.exec_progress_label.setObjectName("ExecProgressLabel")
        self.exec_progress_label.setWordWrap(True)
        self.exec_progress_label.hide()
        self.main_layout.addWidget(self.exec_progress_label)

        self.status_indicator = StatusIndicator()
        self.main_layout.addWidget(self.status_indicator)
        self.status_indicator.set_status("ready")

    def _setup_floating_orb(self) -> None:
        if not bool(settings.get("show_floating_orb")):
            state.orb_visible = False
            return
        try:
            self.floating_orb = FloatingOrb(open_callback=self.show_and_raise)
            self.floating_orb.show_orb()
            self.floating_orb.set_listening(bool(state.is_listening))
        except Exception as exc:
            self.floating_orb = None
            state.orb_visible = False
            logger.exception("Floating orb failed to initialize", exc=exc)

    def apply_ui_preferences(self, *, changed_key: str | None = None) -> None:
        """Apply live UI preferences changed from settings."""
        theme_manager.apply(self)
        key = changed_key or "*"
        if key in {"show_floating_orb", "orb_always_on_top", "theme", "accent_color", "*"}:
            if bool(settings.get("show_floating_orb")):
                if self.floating_orb is None:
                    self._setup_floating_orb()
                elif not self.floating_orb.isVisible() or key == "orb_always_on_top":
                    self.floating_orb.show_orb()
                if self.floating_orb is not None:
                    self.floating_orb.update()
            elif self.floating_orb is not None:
                self.floating_orb.hide_orb()

        if key in {"sidebar_collapsed", "*"} and hasattr(self, "sidebar"):
            should_collapse = bool(settings.get("sidebar_collapsed"))
            if should_collapse != self.sidebar.is_collapsed():
                if should_collapse:
                    self.sidebar.animate_close()
                else:
                    self.sidebar.animate_open()

    def apply_speech_preferences(self, *, changed_key: str | None = None) -> None:
        """Apply persisted speech-input settings to the live voice stack."""
        language = str(settings.get("speech_language") or "en")
        sample_rate = int(settings.get("sample_rate") or 16000)
        silence_threshold = float(settings.get("silence_threshold") or 0.01)
        microphone_device = settings.get("microphone_device")

        try:
            if hasattr(self.stt_engine, "apply_runtime_settings"):
                self.stt_engine.apply_runtime_settings(
                    language=language,
                    sample_rate=sample_rate,
                    silence_threshold=silence_threshold,
                    model_name=str(settings.get("stt_model") or "").strip() or None,
                )
            else:
                if hasattr(self.stt_engine, "set_language"):
                    self.stt_engine.set_language(language)
                if hasattr(self.stt_engine, "sample_rate"):
                    self.stt_engine.sample_rate = sample_rate
                if hasattr(self.stt_engine, "silence_threshold"):
                    self.stt_engine.silence_threshold = silence_threshold

            if hasattr(self.listener, "apply_runtime_settings"):
                self.listener.apply_runtime_settings(
                    sample_rate=sample_rate,
                    rms_gate=silence_threshold,
                    device=microphone_device,
                    restart_if_active=bool(getattr(self.listener, "is_active", lambda: False)()),
                )
            else:
                config = getattr(self.listener, "config", None)
                if config is not None:
                    if hasattr(config, "sample_rate"):
                        config.sample_rate = sample_rate
                    if hasattr(config, "rms_gate"):
                        config.rms_gate = silence_threshold
                if hasattr(self.listener, "_device"):
                    self.listener._device = microphone_device

            logger.info(
                "Speech preferences applied",
                changed_key=changed_key or "*",
                language=language,
                sample_rate=sample_rate,
                silence_threshold=silence_threshold,
                microphone_device=microphone_device,
            )
        except Exception as exc:
            logger.warning("Speech preference apply failed for %s: %s", changed_key or "*", exc)

    def _on_sidebar_page_requested(self, page: str) -> None:
        if page == "settings":
            self.open_settings()
            return
        state.sidebar_page = page
        if page == "chat":
            self.page_stack.setCurrentWidget(self.chat_page)
            logger.info("Sidebar page=chat")
            return
        self._render_summary_page(page)
        self.page_stack.setCurrentWidget(self.summary_page)
        fade_in(self.summary_widget, duration_ms=120)
        logger.info("Sidebar page=%s", page)

    def _render_summary_page(self, page: str) -> None:
        self._clear_summary_layout()
        title = QLabel(page.replace("_", " ").title())
        title.setObjectName("PageTitle")
        self.summary_layout.addWidget(title)

        if page == "skills":
            rows = self._collect_skill_rows()
        elif page == "reminders":
            rows = self._collect_reminder_rows()
        elif page == "history":
            rows = self._collect_history_rows()
        elif page == "plugins":
            rows = self._collect_plugin_rows()
        else:
            rows = []

        for heading, detail in rows:
            self._add_summary_row(heading, detail)
        if not rows:
            self._add_summary_row("Status", "No entries found.")
        self.summary_layout.addStretch()

    def _add_summary_row(self, heading: str, detail: str) -> None:
        frame = QFrame()
        frame.setObjectName("PagePanel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        heading_label = QLabel(str(heading))
        heading_label.setObjectName("SectionTitle")
        detail_label = QLabel(str(detail))
        detail_label.setObjectName("MetaLabel")
        detail_label.setWordWrap(True)
        detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(heading_label)
        layout.addWidget(detail_label)
        self.summary_layout.addWidget(frame)

    def _clear_summary_layout(self) -> None:
        while self.summary_layout.count():
            item = self.summary_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _collect_skill_rows(self) -> list[tuple[str, str]]:
        skills_manager = getattr(self.processor, "skills_manager", None)
        if skills_manager is None or not hasattr(skills_manager, "list_skills"):
            return [("Registered skills", "0")]
        try:
            skills = skills_manager.list_skills()
        except Exception as exc:
            logger.warning("Unable to list skills for UI: %s", exc)
            return [("Registered skills", "Unavailable")]
        rows: list[tuple[str, str]] = []
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            name = str(skill.get("name") or "Skill")
            capabilities = skill.get("capabilities") if isinstance(skill.get("capabilities"), dict) else {}
            supports = capabilities.get("supports") if isinstance(capabilities, dict) else []
            health = skill.get("health") if isinstance(skill.get("health"), dict) else {}
            health_text = ", ".join(f"{k}={v}" for k, v in sorted(health.items())) if health else "health=ok"
            support_text = ", ".join(str(item) for item in supports[:8]) if isinstance(supports, list) else ""
            detail = health_text if not support_text else f"{health_text}\n{support_text}"
            rows.append((name, detail))
        return rows

    def _collect_reminder_rows(self) -> list[tuple[str, str]]:
        reminder_skill = getattr(self.processor, "reminder_skill", None)
        if reminder_skill is None:
            skills_manager = getattr(self.processor, "skills_manager", None)
            reminder_skill = getattr(skills_manager, "reminder_skill", None)
        manager = getattr(reminder_skill, "manager", None)
        if manager is None or not hasattr(manager, "list_reminders"):
            return [
                ("Reminder count", str(getattr(state, "reminder_count", 0))),
                ("Next reminder", str(getattr(state, "next_reminder_time", "") or "None scheduled")),
            ]
        try:
            reminders = manager.list_reminders()
        except Exception as exc:
            logger.warning("Unable to list reminders for UI: %s", exc)
            return [("Reminders", "Unavailable")]
        rows = []
        for reminder in reminders[:20]:
            status = "enabled" if getattr(reminder, "enabled", False) else "disabled"
            when = getattr(reminder, "trigger_time", "")
            message = getattr(reminder, "message", "")
            rows.append((f"{getattr(reminder, 'id', '')}. {getattr(reminder, 'title', 'Reminder')}", f"{when}\n{status} - {message}"))
        return rows

    def _collect_history_rows(self) -> list[tuple[str, str]]:
        rows = []
        for index, (sender, text) in enumerate(self._message_history[-30:], start=1):
            rows.append((f"{index}. {sender}", text))
        return rows

    def _collect_plugin_rows(self) -> list[tuple[str, str]]:
        skills_manager = getattr(self.processor, "skills_manager", None)
        plugin_manager = getattr(skills_manager, "plugin_manager", None)
        if plugin_manager is None or not hasattr(plugin_manager, "list_plugins"):
            return [
                ("Plugins enabled", str(bool(settings.get("plugins_enabled")))),
                ("Loaded plugins", ", ".join(getattr(state, "loaded_plugins", []) or []) or "None loaded"),
            ]
        try:
            plugins = plugin_manager.list_plugins(discover=False)
        except Exception as exc:
            logger.warning("Unable to list plugins for UI: %s", exc)
            return [("Plugins", "Unavailable")]
        rows: list[tuple[str, str]] = []
        for plugin in plugins:
            data = plugin.to_dict() if hasattr(plugin, "to_dict") else dict(plugin) if isinstance(plugin, dict) else {}
            name = str(data.get("name") or data.get("id") or "Plugin")
            detail = (
                f"version={data.get('version', '')} "
                f"enabled={data.get('enabled', False)} "
                f"loaded={data.get('loaded', False)} "
                f"healthy={data.get('healthy', False)}"
            ).strip()
            error = str(data.get("error") or "").strip()
            rows.append((name, detail if not error else f"{detail}\n{error}"))
        return rows

    def add_message(self, sender: str, text: str) -> None:
        if self._shutting_down:
            return
        self._message_history.append((str(sender), str(text)))
        self._message_history = self._message_history[-200:]
        bubble = ChatBubble(sender, text)
        self.chat_layout.addWidget(bubble)
        self._chat_scroll_timer.start(10)

    def _scroll_chat_to_bottom(self) -> None:
        if self._shutting_down:
            return
        try:
            scrollbar = self.chat_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except RuntimeError:
            logger.debug("Skipping chat auto-scroll during teardown", exc_info=True)

    def send_message(self) -> None:
        text = self.text_input.text().strip()
        if not text or self._is_command_running():
            return

        self.text_input.clear()
        self.add_message("User", text)
        logger.info("Message sent: %s", text)
        self._start_command_processing(text, source="text")

    def start_listening(self) -> None:
        if self.listener.is_active() or self._is_command_running():
            return

        try:
            self.apply_speech_preferences()
            if self.tts.is_speaking():
                self.tts.stop()
            self._live_listener_enabled = True
            self.listener.start()
            self._set_voice_state("listening")
            self._sync_listener_controls()

        except Exception as exc:
            self._live_listener_enabled = False
            self._set_voice_state("error")
            logger.error("Microphone error: %s", exc)
            recovery_result = self.processor.handle_external_error(
                DeviceUnavailableError(
                    str(exc) or "Microphone is unavailable.",
                    code="microphone_unavailable",
                    context={"device": "microphone", "stage": "start_listening"},
                ),
                command_context={
                    "command": "voice input",
                    "device": "microphone",
                    "stage": "start_listening",
                    "feature": "microphone",
                    "intent": "voice_input",
                },
                source="voice",
            )
            self._post_command_result(recovery_result)

    def stop_listening(self, *, finalize_utterance: bool = False) -> None:
        if not self.listener.is_active():
            return

        self.listener.stop(finalize_utterance=finalize_utterance)
        self._live_listener_enabled = False
        self._set_voice_state("ready")  # Use valid state machine state
        self._sync_listener_controls()

    def _on_listener_state_changed(self, listener_state: str, payload: object) -> None:
        del payload
        if listener_state == "listening" and self.listener.is_active() and not self._is_command_running():
            self._set_voice_state("LISTENING")
        elif listener_state == "hearing_speech":
            self._set_voice_state("LISTENING")  # Map to valid state

    def _on_utterance_ready(self, audio_data, metadata) -> None:
        del metadata
        if not self._live_listener_enabled:
            return
        self._command_token += 1
        token = self._command_token
        self._active_command_token = token
        self._status_reset_timer.stop()
        self._set_voice_state("understanding")
        self._set_command_controls_enabled(False)
        self.stt_worker = STTWorker(self.stt_engine, audio_data, token, self)
        self.stt_worker.transcription_ready.connect(self._on_stt_finished)
        self.stt_worker.state_changed.connect(self._set_voice_state)
        self.stt_worker.finished.connect(self._on_stt_thread_finished)
        self.stt_worker.start()

    def _on_listener_error(self, message: str) -> None:
        self._live_listener_enabled = False
        if self.listener.is_active():
            self.listener.stop()
        self._set_voice_state("error")
        recovery_result = self.processor.handle_external_error(
            DeviceUnavailableError(
                message or "Microphone is unavailable.",
                code="microphone_unavailable",
                context={"device": "microphone", "stage": "live_listener"},
            ),
            command_context={"command": "voice input", "device": "microphone", "intent": "voice_input"},
            source="voice",
        )
        self._post_command_result(recovery_result)
        self._sync_listener_controls()

    def _on_stt_finished(self, text: str, success: bool, token: int) -> None:
        if token != self._command_token:
            logger.info("Discarding stale STT result token=%s", token)
            return
        if success:
            if text:
                logger.info("Transcript=%s", text)
                self.add_message("User", text)
                self._start_command_processing(text, source="speech", token=token)
                return
            logger.info("No actionable transcript produced")
            self.add_message("Assistant", "I didn't catch that. Please repeat.")
            self._resume_listener_after_activity()
            self._finish_processing()
            return
        else:
            self.add_message("Assistant", f"Could not understand speech: {text}")
            self._set_voice_state("ERROR")
            self._resume_listener_after_activity()
            self._finish_processing()
            return

    def _on_stt_thread_finished(self) -> None:
        worker = self.sender()
        if worker is self.stt_worker:
            self.stt_worker = None
        elif isinstance(worker, STTWorker):
            self._detached_stt_workers = [item for item in self._detached_stt_workers if item is not worker]
        if hasattr(worker, "deleteLater"):
            worker.deleteLater()

    def _start_command_processing(self, text: str, source: str, *, token: int | None = None) -> None:
        if self._is_command_running():
            return

        state.is_processing = True
        if token is None:
            self._command_token += 1
            token = self._command_token
        self._active_command_token = token
        self._status_reset_timer.stop()
        if self.listener.is_active() and not self.listener.is_paused():
            self.listener.pause(reason="command_processing")
        self._set_command_controls_enabled(False)
        self._set_voice_state("PROCESSING")  # Use valid state machine state
        self.exec_progress_label.setText("Understanding...")
        self.exec_progress_label.show()

        self.command_worker = CommandWorker(self.processor, text, token=token, source=source, parent=self)
        self.command_worker.result_ready.connect(self._on_command_finished)
        self.command_worker.error_ready.connect(self._on_command_failed)
        self.command_worker.cancelled.connect(self._on_command_cancelled)
        self.command_worker.state_changed.connect(self._set_voice_state)
        self.command_worker.finished.connect(self._on_command_thread_finished)
        self.command_worker.start()

    def _on_command_finished(self, result: dict, token: int) -> None:
        if token != self._command_token:
            logger.info("Discarding stale command result token=%s", token)
            return
        try:
            self._post_command_result(result)
        finally:
            terminal_state = "READY" if bool((result or {}).get("success")) else "ERROR"
            terminal_label = "Done"
            if terminal_state == "ERROR":
                terminal_label = "Timed out" if str((result or {}).get("error") or "").strip() == "action_timeout" else "Error"
            self._finish_processing(next_state=terminal_state, progress_text=terminal_label, hold_state_ms=self._status_hold_ms())

    def _on_command_failed(self, message: str, token: int) -> None:
        if token != self._command_token:
            logger.info("Discarding stale command failure token=%s", token)
            return
        self.add_message("Assistant", f"Command processing failed: {message}")
        self._set_voice_state("ERROR")
        self._resume_listener_after_activity()
        self._finish_processing(next_state="ERROR", progress_text="Error", hold_state_ms=self._status_hold_ms())

    def _on_command_cancelled(self, token: int) -> None:
        if token != self._command_token:
            logger.info("Discarding stale command cancellation token=%s", token)
            return
        self._finish_processing(next_state="READY", progress_text="Cancelled", hold_state_ms=self._status_hold_ms())

    def _on_command_thread_finished(self) -> None:
        worker = self.sender()
        if worker is self.command_worker:
            self.command_worker = None
        elif isinstance(worker, CommandWorker):
            self._detached_command_workers = [item for item in self._detached_command_workers if item is not worker]
        if hasattr(worker, "deleteLater"):
            worker.deleteLater()

    def _forward_notification_to_ui(self, notification: Notification) -> None:
        if self._shutting_down:
            return
        self.sig_notification_event.emit(notification)

    def _dispatch_response_notification(self, title: str, text: str | None) -> None:
        if not text or self.notification_manager is None:
            return
        self.notification_manager.notify(
            title or "Assistant",
            str(text),
            source="response_service",
            speak=False,
        )

    def _queue_response_speech(self, text: str, **_kwargs) -> bool:
        if text:
            self._pause_listener_for_tts()
        queued = bool(self.tts.speak(str(text or "")))
        if not queued:
            self._resume_listener_after_activity()
        return queued

    def _on_notification_event(self, notification: Notification) -> None:
        self.notification_center.add_notification(notification)
        self.toast_overlay.show_notification(
            notification,
            duration_seconds=max(1, int(settings.get("notification_duration_seconds") or 5)),
        )
        logger.info("Notification rendered in UI: %s", notification.title)

    def _post_command_result(self, result: dict) -> None:
        result_data = result.get("data", {}) if isinstance(result, dict) else {}
        if not isinstance(result_data, dict):
            result_data = {}
        if not bool(result_data.get("speak_response", True)):
            self._resume_listener_after_activity()
        if result_data.get("focus_text_input"):
            self.text_input.setFocus(Qt.OtherFocusReason)

    # â”€â”€ Phase 12: Execution progress handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # â”€â”€ Phase 13: Context change handler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_context_changed(self, app_id: str, title: str) -> None:
        """
        Slot called (on Qt main thread) when the foreground window changes.

        Updates the context_app_label in the header with the current app.
        """
        if not settings.get("show_current_app"):
            return
        display = _CONTEXT_DISPLAY_NAMES.get(app_id, app_id.replace("_", " ").title())
        self.context_app_label.setText(display)
        tooltip = f"Active: {display}"
        if title:
            tooltip += f"\n{title[:80]}"
        self.context_app_label.setToolTip(tooltip)
        logger.debug("UI context updated: %s -> %s", app_id, display)


    def _on_step_progress(self, message: str) -> None:
        """Update progress label on the UI thread (slot, called from worker signal)."""
        self.exec_progress_label.setText(message)
        self.exec_progress_label.show()
        # Also post to chat as a System message
        self.add_message("System", message)

    def cancel_execution(self) -> None:
        """Cancel the current utterance, task, and speech."""
        logger.info("User requested cancel")
        self._status_reset_timer.stop()
        if self.listener.is_capturing_speech():
            self.listener.cancel_current_utterance()
        if self.stt_worker and self.stt_worker.isRunning():
            self.stt_worker.cancel()
            self._detached_stt_workers.append(self.stt_worker)
            self.stt_worker = None
        if self.command_worker and self.command_worker.isRunning():
            self.command_worker.cancel()
            self._detached_command_workers.append(self.command_worker)
            self.command_worker = None
        if self.exec_worker and self.exec_worker.isRunning():
            state.cancel_requested = True
            self.processor.engine.cancel()
            self.exec_worker.requestInterruption()
        self._command_token += 1
        self._active_command_token = None
        if self.listener.is_active():
            self.listener.resume()
        self.tts.stop()
        self.add_message("System", "Cancelled the current command.")
        self._finish_processing(next_state="cancelled", progress_text="Cancelled", hold_state_ms=self._status_hold_ms())

    def _confirm_dangerous_action(self, description: str) -> bool:
        """
        Show a Qt message box asking the user to confirm a dangerous action.
        This callback may be invoked from command/execution worker threads, so
        it marshals the actual dialog onto the Qt main thread and blocks only
        the calling worker until the user answers.
        """
        if QThread.currentThread() == self.thread():
            return self._show_confirmation_dialog(description)

        done = threading.Event()
        payload = {"event": done, "result": False}
        self.sig_confirm_request.emit(str(description), payload)
        timeout = max(10, int(settings.get("confirmation_timeout_seconds") or 60))
        if not done.wait(timeout=timeout):
            logger.warning("Confirmation dialog timed out before a response was received")
            return False
        return bool(payload.get("result"))

    def _handle_confirm_request(self, description: str, payload: object) -> None:
        result = self._show_confirmation_dialog(description)
        if isinstance(payload, dict):
            payload["result"] = result
            event = payload.get("event")
            if isinstance(event, threading.Event):
                event.set()

    def _show_confirmation_dialog(self, description: str) -> bool:
        """Render the confirmation dialog on the Qt main thread."""
        box = QMessageBox(self)
        box.setWindowTitle("Confirm Dangerous Action")
        box.setText(f"This action requires confirmation:\n\n{description}")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.setIcon(QMessageBox.Warning)
        result = box.exec()
        return result == QMessageBox.Yes

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "toast_overlay"):
            self.toast_overlay.sync_to_parent()

    def _finish_processing(
        self,
        *,
        next_state: str | None = None,
        progress_text: str | None = None,
        hold_state_ms: int = 0,
    ) -> None:
        state.is_processing = False
        state.cancel_requested = False
        self._active_command_token = None
        self._set_command_controls_enabled(True)
        self.cancel_exec_btn.setEnabled(self.listener.is_active() or state.is_executing or self.tts.is_speaking())
        if progress_text:
            self.exec_progress_label.setText(progress_text)
            self.exec_progress_label.show()
        if next_state:
            self._set_voice_state(next_state)
            if hold_state_ms > 0:
                self._status_reset_timer.start(hold_state_ms)
                return
        self._restore_ready_state()

    def _restore_ready_state(self) -> None:
        self.exec_progress_label.hide()
        if self.listener.is_active() and not self.listener.is_paused() and not self.tts.is_speaking():
            self._set_voice_state("LISTENING")
            return
        if self.tts.is_speaking():
            self._set_voice_state("SPEAKING")
            return
        self._set_voice_state("READY")

    @staticmethod
    def _status_hold_ms() -> int:
        return 1200

    def _set_command_controls_enabled(self, enabled: bool) -> None:
        self.text_input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self._sync_listener_controls(enabled=enabled)

    def _is_command_running(self) -> bool:
        running = bool(self._active_command_token is not None and self.command_worker and self.command_worker.isRunning())
        exec_running = bool(self.exec_worker and self.exec_worker.isRunning())
        stt_running = bool(self.stt_worker and self.stt_worker.isRunning())
        return running or exec_running or stt_running

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, tts_engine=self.tts)
        dialog.setting_changed.connect(self._on_setting_changed)
        dialog.exec()
        self.update_identity_title()
        self._sync_mute_button()

    def _on_setting_changed(self, key: str, value) -> None:
        del value
        if key in {"assistant_name", "user_name", "*"}:
            self.update_identity_title()
        if key in {
            "theme",
            "use_system_theme",
            "accent_color",
            "window_transparency",
            "show_floating_orb",
            "orb_always_on_top",
            "animations_enabled",
            "reduced_motion",
            "sidebar_collapsed",
            "*",
        }:
            self.apply_ui_preferences(changed_key=key)
        if key in {"mute", "*"}:
            self._sync_mute_button()
        if key in {"speech_language", "sample_rate", "silence_threshold", "microphone_device", "*"}:
            self.apply_speech_preferences(changed_key=key)
        if key in {"push_to_talk_hotkey", "start_stop_listening_hotkey", "open_assistant_hotkey", "push_to_talk_mode", "*"}:
            self.refresh_hotkeys()

    def toggle_mute(self) -> None:
        is_muted = self.mute_btn.isChecked()
        self.tts.set_muted(is_muted)
        self.mute_btn.setText("Unmute" if is_muted else "Mute")

    def update_identity_title(self) -> None:
        if hasattr(self.processor.identity_mgr, "load_identity"):
            self.processor.identity_mgr.load_identity()
        new_title = self.processor.identity_mgr.format_title()
        self.title_label.setText(new_title)
        self.setWindowTitle(new_title)

    def refresh_hotkeys(self, *, replace: bool = True) -> None:
        if replace:
            try:
                self.hotkey_mgr.unregister_all()
            except Exception:
                logger.debug("Failed to unregister previous hotkeys", exc_info=True)
            self.hotkey_mgr = HotkeyManager()

        try:
            self.hotkey_mgr.register_push_to_talk(
                callback_start=lambda: self.sig_start_listening.emit(),
                callback_stop=lambda: self.sig_finalize_listening.emit(),
                callback_toggle=lambda: self.sig_toggle_listening.emit(),
                callback_open=lambda: self.sig_open_window.emit(),
            )
        except TypeError:
            self.hotkey_mgr.register_push_to_talk(
                callback_start=lambda: self.sig_start_listening.emit(),
                callback_stop=lambda: self.sig_finalize_listening.emit(),
            )

    def toggle_listening(self) -> None:
        if self.listener.is_active():
            self.sig_stop_listening.emit()
        else:
            self.sig_start_listening.emit()

    def release_push_to_talk(self) -> None:
        if self.listener.is_active():
            self.listener.stop(finalize_utterance=True)
            self._live_listener_enabled = False
            if not self._is_command_running():
                self._set_voice_state("ready")
            self._sync_listener_controls()

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _sync_mute_button(self) -> None:
        muted = bool(settings.get("mute"))
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(muted)
        self.mute_btn.setText("Unmute" if muted else "Mute")
        self.mute_btn.blockSignals(False)

    def _pause_listener_for_tts(self) -> None:
        if self.listener.is_active():
            self.listener.pause(reason="tts")
            self._tts_paused_listener = True

    def _resume_listener_after_activity(self) -> None:
        if self._live_listener_enabled and self.listener.is_active() and self.listener.is_paused() and not self.tts.is_speaking():
            self.listener.resume()
            self._tts_paused_listener = False
        self._sync_listener_controls()

    def _on_tts_event(self, event_name: str, payload: str) -> None:
        self._last_tts_payload = payload
        if event_name == "started":
            logger.info("TTS started")
            if self.listener.is_active():
                self._pause_listener_for_tts()
            self._set_voice_state("SPEAKING")
            self._sync_listener_controls()
            return
        if event_name in {"finished", "error"}:
            if event_name == "error":
                logger.warning("TTS failure: %s", payload)
            if self._tts_paused_listener:
                self._resume_listener_after_activity()
            if self.listener.is_active() and not self._is_command_running():
                self._set_voice_state("LISTENING")
            elif not self._is_command_running():
                self._set_voice_state("READY")

    def _set_voice_state(self, status: str) -> None:
        """Set voice state with proper state machine transitions."""
        normalized = str(status or "ready").lower()

        # Map non-standard states to valid state machine states
        state_map = {
            "understanding": "PROCESSING",
            "hearing_speech": "LISTENING",
            "executing": "EXECUTING",
            "ready": "READY",
            "listening": "LISTENING",
            "processing": "PROCESSING",
            "speaking": "SPEAKING",
            "error": "ERROR",
        }
        machine_state = state_map.get(normalized, normalized.upper())

        # Validate against valid states
        valid_states = {"READY", "LISTENING", "PROCESSING", "EXECUTING", "SPEAKING", "ERROR"}
        if machine_state not in valid_states:
            machine_state = "READY"

        # Update both UI state and canonical state machine
        state.voice_state = normalized
        state.set_state(machine_state, reason=f"voice_status:{normalized}")

        self.status_indicator.set_status(normalized)
        if self.floating_orb is not None:
            self.floating_orb.set_listening(normalized in {"listening", "hearing_speech"})

        logger.debug("State transition: %s -> %s", state.assistant_state, machine_state)

    def _sync_listener_controls(self, enabled: bool | None = None) -> None:
        can_edit = True if enabled is None else bool(enabled)
        listening = self.listener.is_active()
        self.start_btn.setEnabled(can_edit and not listening)
        self.stop_btn.setEnabled(listening)
        self.cancel_exec_btn.setEnabled(listening or self._is_command_running() or self.tts.is_speaking())
