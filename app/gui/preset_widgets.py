"""Prompt preset selector + editor (the Prompt section of the control panel)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme
from app.gui.widgets import GlassCard, primary_button


class PromptPanel(QWidget):
    """Preset selector + editor. Talks to the service only."""

    test_requested = Signal(str)  # content to test

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self._presets = service.presets

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # --- preset selector card ---
        card = GlassCard("续跑 Prompt 预设")
        row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        row.addWidget(self.preset_combo, 1)

        self.fav_btn = QPushButton("☆")
        self.fav_btn.setProperty("flat", True)
        self.fav_btn.setFixedWidth(34)
        self.fav_btn.clicked.connect(self._toggle_favorite)
        row.addWidget(self.fav_btn)

        self.dup_btn = QPushButton("复制")
        self.dup_btn.setProperty("flat", True)
        self.dup_btn.clicked.connect(self._duplicate)
        row.addWidget(self.dup_btn)

        self.new_btn = QPushButton("＋ 新建")
        self.new_btn.setProperty("flat", True)
        self.new_btn.clicked.connect(self._new)
        row.addWidget(self.new_btn)

        self.del_btn = QPushButton("删除")
        self.del_btn.setProperty("flat", True)
        self.del_btn.setProperty("danger", True)
        self.del_btn.clicked.connect(self._delete)
        row.addWidget(self.del_btn)

        self.default_btn = QPushButton("设为默认")
        self.default_btn.setProperty("flat", True)
        self.default_btn.clicked.connect(self._set_default)
        row.addWidget(self.default_btn)
        card.body().addLayout(row)

        self.desc_label = QLabel("")
        self.desc_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        self.desc_label.setWordWrap(True)
        card.body().addWidget(self.desc_label)
        root.addWidget(card)

        # --- editor card ---
        editor = GlassCard("Prompt 编辑器")
        self.name_edit = QLineEdit()
        editor.body().addWidget(QLabel("名称"))
        editor.body().addWidget(self.name_edit)
        editor.body().addWidget(QLabel("描述"))
        self.desc_edit = QLineEdit()
        editor.body().addWidget(self.desc_edit)

        self.content_edit = QPlainTextEdit()
        self.content_edit.setMinimumHeight(200)
        editor.body().addWidget(self.content_edit)

        meta_row = QHBoxLayout()
        self.count_label = QLabel("0 字")
        self.count_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.modified_label = QLabel("")
        self.modified_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.binding_label = QLabel("")
        self.binding_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        meta_row.addWidget(self.count_label)
        meta_row.addStretch()
        meta_row.addWidget(self.modified_label)
        editor.body().addLayout(meta_row)

        btn_row = QHBoxLayout()
        self.save_btn = primary_button("保存")
        self.save_btn.clicked.connect(self._save)
        self.saveas_btn = primary_button("另存为", flat=True)
        self.saveas_btn.clicked.connect(self._save_as)
        self.reset_btn = primary_button("恢复内置", flat=True)
        self.reset_btn.clicked.connect(self._reset)
        self.test_btn = primary_button("测试 Prompt", flat=True)
        self.test_btn.clicked.connect(self._test)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.saveas_btn)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.test_btn)
        editor.body().addLayout(btn_row)

        root.addWidget(editor)
        self._editing_id: str = ""
        self.reload()

    # -- data -------------------------------------------------------------

    def reload(self) -> None:
        self._presets = self.service.presets  # service may have reloaded
        current = self._editing_id or self._presets.default_preset_id
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for p in self._presets.list_presets():
            label = f"{'★ ' if p.favorite else ''}{p.name}"
            if p.id == self._presets.default_preset_id:
                label += "（默认）"
            self.preset_combo.addItem(label, p.id)
        self.preset_combo.blockSignals(False)
        idx = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._load_editor()

    def _current_preset_id(self) -> str:
        return self.preset_combo.currentData() or ""

    def _load_editor(self) -> None:
        pid = self._current_preset_id()
        preset = self._presets.get(pid)
        if preset is None:
            return
        self._editing_id = pid
        self.name_edit.setText(preset.name)
        self.desc_edit.setText(preset.description)
        self.content_edit.setPlainText(preset.content)
        self.desc_label.setText(preset.description)
        self.fav_btn.setText("★" if preset.favorite else "☆")
        self.del_btn.setEnabled(not preset.builtin)
        self.reset_btn.setEnabled(preset.builtin)
        self.modified_label.setText(
            f"更新于 {preset.updated_at[:16].replace('T', ' ')}" if preset.updated_at else ""
        )
        # binding hint
        if preset.id == self._presets.default_preset_id:
            self.binding_label.setText("（默认）")
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(f"{len(self.content_edit.toPlainText())} 字")

    def _on_preset_changed(self) -> None:
        self._load_editor()

    # -- actions ----------------------------------------------------------

    def _save(self) -> None:
        pid = self._editing_id
        if not pid:
            return
        self._presets.update(
            pid, name=self.name_edit.text(), description=self.desc_edit.text(),
            content=self.content_edit.toPlainText(),
        )
        self.reload()

    def _save_as(self) -> None:
        p = self._presets.create(
            self.name_edit.text() + "（副本）", self.content_edit.toPlainText(),
            self.desc_edit.text(),
        )
        self._editing_id = p.id
        self.reload()

    def _new(self) -> None:
        p = self._presets.create("新建 Preset", "", "")
        self._editing_id = p.id
        self.reload()

    def _duplicate(self) -> None:
        pid = self._current_preset_id()
        if pid:
            self._presets.duplicate(pid)
            self.reload()

    def _delete(self) -> None:
        pid = self._editing_id
        if pid:
            self._presets.delete(pid)
            self._editing_id = ""
            self.reload()

    def _reset(self) -> None:
        pid = self._editing_id
        if pid:
            self._presets.reset_builtin(pid)
            self.reload()

    def _toggle_favorite(self) -> None:
        pid = self._current_preset_id()
        if pid:
            self._presets.toggle_favorite(pid)
            self.reload()

    def _set_default(self) -> None:
        pid = self._current_preset_id()
        if pid:
            self._presets.set_default(pid)
            self.reload()

    def _test(self) -> None:
        self.test_requested.emit(self.content_edit.toPlainText())
