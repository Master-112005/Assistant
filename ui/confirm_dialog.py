"""
Dedicated confirmation dialog for permission-gated actions.

Provides a clear, risk-aware UI for medium and dangerous actions with:
- Color-coded risk banners (yellow for MEDIUM, red for DANGEROUS)
- Action summary with word-wrap
- Optional temporary approval combo (disabled for DANGEROUS)
- Confirm / Cancel buttons with keyboard shortcuts
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from core.theme_manager import theme_manager


_RISK_STYLES: dict[str, dict[str, str]] = {
    "DANGEROUS": {
        "banner_bg": "#8b0000",
        "banner_fg": "#ffffff",
        "icon": "!",
        "label": "Dangerous",
    },
    "MEDIUM": {
        "banner_bg": "#b8860b",
        "banner_fg": "#ffffff",
        "icon": "!",
        "label": "Medium Risk",
    },
    "SAFE": {
        "banner_bg": "#2e7d32",
        "banner_fg": "#ffffff",
        "icon": "OK",
        "label": "Safe",
    },
}


class ConfirmationDialog(QDialog):
    """Clear confirmation UI for medium and dangerous actions."""

    def __init__(
        self,
        *,
        action_summary: str,
        risk_level: str,
        allow_temporary_approvals: bool = False,
        details: dict | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Action")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setMaximumWidth(600)

        risk_text = str(risk_level or "UNKNOWN").upper()
        style = _RISK_STYLES.get(risk_text, _RISK_STYLES["DANGEROUS"])
        tokens = theme_manager.theme_tokens()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Risk banner ──────────────────────────────────────────────────
        banner = QLabel(f"  {style['icon']}  {style['label']}  ")
        banner.setAlignment(Qt.AlignCenter)
        banner.setStyleSheet(
            f"background-color: {style['banner_bg']}; "
            f"color: {style['banner_fg']}; "
            "font-size: 14px; font-weight: bold; "
            "padding: 8px; border-radius: 4px;"
        )
        layout.addWidget(banner)

        # ── Action summary ───────────────────────────────────────────────
        self.summary_label = QLabel(action_summary)
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("SectionTitle")
        layout.addWidget(self.summary_label)

        # ── Details section (optional) ───────────────────────────────────
        if details:
            sep = QFrame()
            sep.setObjectName("Divider")
            layout.addWidget(sep)

            for key, value in details.items():
                if key in {"callback", "params"} or value in (None, ""):
                    continue
                row = QHBoxLayout()
                key_label = QLabel(f"{key.replace('_', ' ').title()}:")
                key_label.setObjectName("SectionTitle")
                key_label.setFixedWidth(120)
                val_label = QLabel(str(value)[:200])
                val_label.setWordWrap(True)
                val_label.setObjectName("MetaLabel")
                row.addWidget(key_label)
                row.addWidget(val_label, 1)
                layout.addLayout(row)

        # ── Temporary approval combo ─────────────────────────────────────
        self.temporary_combo: QComboBox | None = None
        if allow_temporary_approvals and risk_text != "DANGEROUS":
            sep2 = QFrame()
            sep2.setObjectName("Divider")
            layout.addWidget(sep2)

            combo_label = QLabel("Approval scope:")
            combo_label.setObjectName("MetaLabel")
            layout.addWidget(combo_label)

            self.temporary_combo = QComboBox()
            self.temporary_combo.addItem("Only this time", "none")
            self.temporary_combo.addItem("Allow for 10 minutes", 600)
            self.temporary_combo.addItem("Allow for 30 minutes", 1800)
            self.temporary_combo.addItem("Allow for this session", 0)
            layout.addWidget(self.temporary_combo)

        # ── Buttons ──────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Yes | QDialogButtonBox.Cancel, parent=self
        )
        yes_btn = buttons.button(QDialogButtonBox.Yes)
        if yes_btn:
            yes_btn.setText("Confirm")
            if risk_text == "DANGEROUS":
                yes_btn.setStyleSheet(
                    f"background-color: {style['banner_bg']}; color: {style['banner_fg']}; "
                    "font-weight: bold; padding: 6px 16px;"
                )
            else:
                yes_btn.setStyleSheet(f"padding: 6px 16px; color: {tokens['accent_text']};")
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        if cancel_btn:
            cancel_btn.setStyleSheet("padding: 6px 16px;")
            cancel_btn.setFocus()

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.adjustSize()

    def selected_grant_duration(self) -> int | None:
        """Return the temporary grant duration in seconds, or None for one-time."""
        if self.temporary_combo is None:
            return None
        value = self.temporary_combo.currentData()
        if value == "none":
            return None
        return int(value)
