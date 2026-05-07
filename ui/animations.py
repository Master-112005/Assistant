"""
Reusable non-blocking Qt animation helpers.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from core import settings, state
from core.logger import get_logger
from core.theme_manager import theme_manager

logger = get_logger(__name__)


def animations_enabled() -> bool:
    """Return True when motion is allowed by user preferences."""
    try:
        enabled = bool(settings.get("animations_enabled"))
        reduced = bool(settings.get("reduced_motion"))
    except Exception:
        enabled = True
        reduced = False
    state.animations_ready = enabled and not reduced
    return state.animations_ready


def fade_in(widget: QWidget, *, duration_ms: int | None = None) -> QPropertyAnimation | None:
    """Fade a widget in without blocking the UI thread."""
    if widget is None:
        return None
    if not animations_enabled():
        widget.show()
        return None

    effect = _opacity_effect(widget)
    effect.setOpacity(0.0)
    widget.show()
    animation = _animation(effect, b"opacity", duration_ms, 0.0, 1.0)
    animation.start()
    _keep_animation(widget, animation)
    logger.debug("Animation triggered fade_in widget=%s", widget.objectName())
    return animation


def fade_out(widget: QWidget, *, duration_ms: int | None = None, hide: bool = True) -> QPropertyAnimation | None:
    """Fade a widget out and optionally hide it when finished."""
    if widget is None:
        return None
    if not animations_enabled():
        if hide:
            widget.hide()
        return None

    effect = _opacity_effect(widget)
    effect.setOpacity(1.0)
    animation = _animation(effect, b"opacity", duration_ms, 1.0, 0.0)
    if hide:
        animation.finished.connect(widget.hide)
    animation.start()
    _keep_animation(widget, animation)
    logger.debug("Animation triggered fade_out widget=%s", widget.objectName())
    return animation


def slide_in(
    widget: QWidget,
    *,
    start_offset: QPoint | None = None,
    duration_ms: int | None = None,
) -> QPropertyAnimation | None:
    """Slide a manually-positioned widget into its current geometry."""
    if widget is None:
        return None
    if not animations_enabled():
        widget.show()
        return None

    end_pos = widget.pos()
    offset = start_offset or QPoint(28, 0)
    start_pos = end_pos + offset
    widget.move(start_pos)
    widget.show()
    animation = _animation(widget, b"pos", duration_ms, start_pos, end_pos)
    animation.start()
    _keep_animation(widget, animation)
    logger.debug("Animation triggered slide_in widget=%s", widget.objectName())
    return animation


def pulse(widget: QWidget, *, duration_ms: int | None = None) -> QPropertyAnimation | None:
    """Apply a lightweight opacity pulse to a widget."""
    if widget is None or not animations_enabled():
        return None
    effect = _opacity_effect(widget)
    effect.setOpacity(1.0)
    animation = _animation(effect, b"opacity", duration_ms, 1.0, 0.72)
    animation.setLoopCount(-1)
    animation.setEasingCurve(QEasingCurve.InOutSine)
    animation.start()
    _keep_animation(widget, animation)
    logger.debug("Animation triggered pulse widget=%s", widget.objectName())
    return animation


def highlight(widget: QWidget, *, duration_ms: int | None = None) -> None:
    """Temporarily mark a widget as highlighted for stylesheet-driven feedback."""
    if widget is None:
        return
    widget.setProperty("highlighted", "true")
    _repolish(widget)
    delay = duration_ms or int(theme_manager.theme_tokens().get("duration_slow", 260))
    QTimer.singleShot(max(80, delay), lambda: _clear_highlight(widget))
    logger.debug("Animation triggered highlight widget=%s", widget.objectName())


def stop_animations(widget: QWidget) -> None:
    """Stop animations owned by a widget."""
    for animation in list(getattr(widget, "_nova_animations", []) or []):
        try:
            animation.stop()
        except Exception:
            logger.debug("Animation stop failed", exc_info=True)
    widget._nova_animations = []


def _animation(
    target: Any,
    prop: bytes,
    duration_ms: int | None,
    start: Any,
    end: Any,
) -> QPropertyAnimation:
    animation = QPropertyAnimation(target, prop)
    animation.setDuration(max(0, int(duration_ms or theme_manager.theme_tokens().get("duration_normal", 180))))
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    return animation


def _opacity_effect(widget: QWidget) -> QGraphicsOpacityEffect:
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    return effect


def _keep_animation(widget: QWidget, animation: QPropertyAnimation) -> None:
    animations = list(getattr(widget, "_nova_animations", []) or [])
    animations.append(animation)
    widget._nova_animations = animations

    def _cleanup() -> None:
        current = list(getattr(widget, "_nova_animations", []) or [])
        if animation in current:
            current.remove(animation)
        widget._nova_animations = current

    animation.finished.connect(_cleanup)


def _clear_highlight(widget: QWidget) -> None:
    try:
        widget.setProperty("highlighted", "false")
        _repolish(widget)
    except RuntimeError:
        return


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
