"""
Main startup entry point for Nova Assistant.
"""
import os
import sys

# ── Suppress Qt DPI warning on Windows ──────────────────────────────────
# Must be set before any Qt import.
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

def main() -> None:
    """Main application loop and setup."""
    logger = None
    try:
        # 1. Initialize paths (implicitly handled by importing core.paths)
        from core import paths
        
        # 2. Load config
        from core.config import config

        # 3. Load settings before observability so runtime preferences apply.
        from core import settings

        # 4. Setup logger / metrics / analytics
        from core.analytics import analytics
        from core.logger import AppLogger, get_logger, shutdown as shutdown_logging
        from core.metrics import metrics

        AppLogger.initialize()
        logger = get_logger(__name__)
        metrics.init()
        analytics.init()

        # 5. Startup logs
        logger.info("Application startup", app_name=config.APP_NAME, version=config.VERSION)
        logger.info("Foundation ready")
        
        # 6. Start UI
        from PySide6.QtWidgets import QApplication
        from core.notifications import NotificationManager, set_global_notification_manager
        from core.processor import CommandProcessor
        from core.tts import TextToSpeechEngine
        from ui.main_window import MainWindow

        # Create QApplication FIRST so Qt owns the COM apartment.
        app = QApplication(sys.argv)

        # Create TTS engine AFTER QApplication to avoid COM conflict.
        # pyttsx3 init is deferred to a background thread with its own
        # CoInitialize, so it never collides with Qt's OleInitialize.
        tts_engine = TextToSpeechEngine()
        notification_manager = NotificationManager(tts_engine=tts_engine)
        set_global_notification_manager(notification_manager)
        processor = CommandProcessor(notification_manager=notification_manager)
        window = MainWindow(
            processor=processor,
            tts_engine=tts_engine,
            notification_manager=notification_manager,
        )
        if hasattr(processor, "shutdown"):
            app.aboutToQuit.connect(processor.shutdown)
        app.aboutToQuit.connect(notification_manager.stop)
        app.aboutToQuit.connect(shutdown_logging)
        
        # Position window at bottom right
        screen_geometry = app.primaryScreen().availableGeometry()
        x = screen_geometry.width() - window.width() - 20
        y = screen_geometry.height() - window.height() - 20
        window.move(x, y)
        
        window.show()
        
        sys.exit(app.exec())
        
    except Exception as exc:
        try:
            from core.analytics import analytics
            from core.logger import AppLogger, get_logger

            logger = logger or get_logger(__name__)
            logger.exception("Application failed to start", exc=exc)
            analytics.init()
            analytics.record_error(
                "startup_failure",
                str(exc),
                exc=exc,
                module=__name__,
                context="application_startup",
                source="startup",
            )
            analytics.mark_session_crashed("startup_failure", exc=exc)
        except Exception:
            pass
        try:
            AppLogger.shutdown()
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
