"""Reusable GUI widgets built on the Liquid Glass theme."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme


class GlassCard(QFrame):
    """A translucent, rounded card with an optional title."""

    def __init__(self, title: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("glassCard")
        self.setStyleSheet(theme.glass_card_qss())
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)
        if title:
            lbl = QLabel(title)
            lbl.setStyleSheet("font-weight: 700; font-size: 14px; color: #334155;")
            self._layout.addWidget(lbl)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)


class StatusBadge(QLabel):
    """Colour + text. Colour is a supplement, never the only signal."""

    def __init__(self, text: str = "", kind: str = "neutral", parent=None) -> None:
        super().__init__(text, parent)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        color = theme.state_color(kind)
        self.setStyleSheet(
            f"color: white; background: {color}; padding: 3px 10px; border-radius: 6px;"
            f"font-weight: 600; font-size: 12px;"
        )

    def set_text(self, text: str) -> None:
        self.setText(text)


class SourceBadge(QLabel):
    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(theme.source_label(kind), parent)
        color = theme.source_color(kind)
        self.setStyleSheet(
            f"color: {color}; background: {theme.hex_to_rgba(color, 0.12)};"
            f"padding: 1px 7px; border-radius: 5px; font-size: 10px; font-weight: 600;"
        )


class SettingRow(QWidget):
    """A labelled row holding a control widget."""

    def __init__(self, label: str, control: QWidget, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(lbl)
        layout.addStretch()
        layout.addWidget(control)


class KVRow(QWidget):
    """Key / value read-only row (e.g. 'Target' → value)."""

    def __init__(self, key: str, value: str = "—", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.key = QLabel(key)
        self.key.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.value = QLabel(value)
        self.value.setStyleSheet("font-weight: 600;")
        self.value.setWordWrap(True)
        layout.addWidget(self.key, 0)
        layout.addWidget(self.value, 1)

    def set_value(self, value: str, color: str | None = None) -> None:
        self.value.setText(value)
        if color:
            self.value.setStyleSheet(f"font-weight: 600; color: {color};")


class SafetyChecklist(QWidget):
    """A live checklist of the arming preconditions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)
        self._rows: dict[str, QLabel] = {}

    def set_items(self, items: list[tuple[str, bool, str]]) -> None:
        """items: (label, ok, detail)."""
        # Remove stale rows.
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows.clear()
        for label, ok, detail in items:
            mark = "✓" if ok else "✕"
            color = theme.READY if ok else theme.ERROR
            text = f"{mark}  {label}"
            if detail and not ok:
                text += f"  —  {detail}"
            row = QLabel(text)
            row.setStyleSheet(f"color: {color}; font-size: 12.5px;")
            row.setWordWrap(True)
            self._layout.addWidget(row)
            self._rows[label] = row


def primary_button(text: str, *, danger: bool = False, flat: bool = False) -> QPushButton:
    btn = QPushButton(text)
    if danger:
        btn.setProperty("danger", True)
    if flat:
        btn.setProperty("flat", True)
    return btn
