"""Left conversation sidebar: search, current/target markers, source badges."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from app.discovery.models import ConversationInfo
from app.gui import theme
from app.gui.widgets import SourceBadge


def _fmt_time(iso) -> str:
    if not iso:
        return ""
    try:
        if isinstance(iso, datetime):
            dt = iso
        else:
            dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


class ConversationRow(QWidget):
    def __init__(self, conv: ConversationInfo, is_current: bool, is_target: bool,
                 matched: bool | None, parent=None) -> None:
        super().__init__(parent)
        self.conv = conv
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        # status marker
        marker = ""
        marker_color = theme.TEXT_FAINT
        if is_current and is_target and matched:
            marker, marker_color = "✓", theme.READY
        elif is_current:
            marker, marker_color = "●", theme.MONITORING
        elif is_target:
            marker, marker_color = "◎", theme.DRY_RUN

        self.marker = QLabel(marker)
        self.marker.setStyleSheet(f"color: {marker_color}; font-weight: 700;")
        layout.addWidget(self.marker)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title = QLabel(conv.display_title)
        title.setStyleSheet("font-weight: 600;")
        title.setWordWrap(True)
        meta = QLabel(f"{conv.short_id}  ·  {_fmt_time(conv.updated_at)}".strip(" ·"))
        meta.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        text_col.addWidget(title)
        text_col.addWidget(meta)
        layout.addLayout(text_col, 1)

        badges = QVBoxLayout()
        badges.setSpacing(2)
        badges.setAlignment(Qt.AlignRight)
        if is_current:
            b = QLabel("Current")
            b.setStyleSheet(f"color: white; background: {theme.MONITORING}; padding: 1px 6px;"
                            "border-radius: 4px; font-size: 10px; font-weight: 700;")
            badges.addWidget(b)
        if is_target:
            b = QLabel("Target")
            b.setStyleSheet(f"color: white; background: {theme.DRY_RUN}; padding: 1px 6px;"
                            "border-radius: 4px; font-size: 10px; font-weight: 700;")
            badges.addWidget(b)
        badges.addWidget(SourceBadge(conv.source_kind))
        layout.addLayout(badges)


class ConversationSidebar(QWidget):
    """Left pane: search + current/target/recent lists + actions."""

    selection_changed = Signal(str)  # conversation id (pending target)
    refresh_requested = Signal()
    use_current_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(8)

        title = QLabel("Conversations · Desktop")
        title.setStyleSheet("font-weight: 700; font-size: 15px;")
        layout.addWidget(title)
        src_hint = QLabel("来源：ChatGPT 桌面端本地数据")
        src_hint.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        layout.addWidget(src_hint)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search conversations…")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setSpacing(1)
        self.list.itemClicked.connect(self._on_clicked)
        layout.addWidget(self.list, 1)

        actions = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setProperty("flat", True)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        self.use_current_btn = QPushButton("Use Current")
        self.use_current_btn.setProperty("flat", True)
        self.use_current_btn.clicked.connect(self.use_current_requested.emit)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.use_current_btn)
        layout.addLayout(actions)

        self._conversations: list[ConversationInfo] = []
        self._current_id = ""
        self._target_id = ""
        self._pending_id = ""
        self._matched: bool | None = None

    # -- population --------------------------------------------------------

    def set_data(self, conversations: list[ConversationInfo], current_id: str,
                 target_id: str, matched: bool | None) -> None:
        self._conversations = conversations
        self._current_id = current_id
        self._target_id = target_id
        self._matched = matched
        self._pending_id = self._pending_id or current_id
        self._rebuild()

    def set_pending(self, conversation_id: str) -> None:
        self._pending_id = conversation_id
        # re-highlight without rebuilding data
        for i in range(self.list.count()):
            item = self.list.item(i)
            row = self.list.itemWidget(item)
            if isinstance(row, ConversationRow) and row.conv.id == conversation_id:
                self.list.setCurrentItem(item)
                break

    @property
    def pending_id(self) -> str:
        return self._pending_id

    # -- internals ---------------------------------------------------------

    def _rebuild(self) -> None:
        self.list.clear()
        # Order: current first, then target, then recent.
        ordered = sorted(
            self._conversations,
            key=lambda c: (
                0 if c.id == self._current_id else 1 if c.id == self._target_id else 2,
                -(c.updated_at.timestamp() if c.updated_at else 0),
            ),
        )
        for conv in ordered:
            if self.search.text().strip() and self.search.text().strip().lower() not in conv.display_title.lower():
                continue
            item = QListWidgetItem()
            item.setSizeHint(QSize(240, 58))
            row = ConversationRow(
                conv,
                is_current=conv.id == self._current_id,
                is_target=conv.id == self._target_id,
                matched=self._matched if conv.id == self._target_id else None,
            )
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def _apply_filter(self) -> None:
        self._rebuild()

    def _on_clicked(self, item: QListWidgetItem) -> None:
        row = self.list.itemWidget(item)
        if isinstance(row, ConversationRow):
            self._pending_id = row.conv.id
            self.selection_changed.emit(row.conv.id)
