"""Liquid Glass design system (light theme, Codex-style workspace).

Restrained translucency, soft borders, a light blue-grey base, and a single
accent per semantic state. Colour is always a supplement to text, never the
only signal.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

# --- base palette ---------------------------------------------------------
BG = "#eef1f6"            # level 0 - window background
SURFACE = "rgba(255,255,255,0.82)"   # level 1 - main panel
SURFACE_SOLID = "#fbfcfe"
SIDEBAR = "rgba(246,248,252,0.90)"   # level 2 - sidebar
CARD = "rgba(255,255,255,0.88)"      # level 3 - cards
CARD_BORDER = "rgba(148,163,184,0.28)"
HIGHLIGHT = "rgba(255,255,255,0.9)"  # inner highlight (1px)

TEXT = "#1f2937"
TEXT_MUTED = "#64748b"
TEXT_FAINT = "#94a3b8"

ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"

# --- semantic states ------------------------------------------------------
MONITORING = "#2563eb"   # blue
READY = "#16a34a"        # green
WAITING = "#d97706"      # amber
DRY_RUN = "#7c3aed"      # purple
MISMATCH = "#ea580c"     # orange
ERROR = "#dc2626"        # red
UNCERTAIN = "#dc2626"    # red
NEUTRAL = "#64748b"      # grey

#: source badge colour per source kind
SOURCE_COLORS = {
    "desktop_active": "#16a34a",
    "codex_work_session": "#2563eb",
    "desktop_cache": "#0891b2",
    "codex_local_storage": "#d97706",
    "web_cache": "#d97706",
    "unknown": "#94a3b8",
}

RADIUS_CARD = 18
RADIUS_CONTAINER = 22
RADIUS_BUTTON = 11


def state_color(kind: str) -> str:
    return {
        "monitoring": MONITORING,
        "ready": READY,
        "waiting": WAITING,
        "dry_run": DRY_RUN,
        "mismatch": MISMATCH,
        "error": ERROR,
        "uncertain": UNCERTAIN,
    }.get(kind, NEUTRAL)


def source_color(kind: str) -> str:
    return SOURCE_COLORS.get(kind, NEUTRAL)


def source_label(kind: str) -> str:
    return {
        "desktop_active": "桌面",
        "codex_work_session": "工作",
        "desktop_cache": "缓存",
        "codex_local_storage": "网页缓存",
        "web_cache": "网页缓存",
        "unknown": "未验证",
    }.get(kind, kind)


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


STYLESHEET = f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{
    padding: 8px 10px; border-radius: 10px; margin: 1px 4px;
}}
QListWidget::item:hover {{ background: rgba(59,130,246,0.08); }}
QListWidget::item:selected {{ background: rgba(59,130,246,0.16); color: {TEXT}; }}

QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {SURFACE_SOLID};
    border: 1px solid {CARD_BORDER};
    border-radius: {RADIUS_BUTTON}px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}

QPushButton {{
    background: {ACCENT}; color: white; border: none;
    border-radius: {RADIUS_BUTTON}px; padding: 7px 14px;
}}
QPushButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton:disabled {{ background: rgba(148,163,184,0.35); color: rgba(255,255,255,0.9); }}
QPushButton[flat="true"] {{
    background: rgba(59,130,246,0.08); color: {ACCENT};
}}
QPushButton[danger="true"] {{ background: {ERROR}; }}
QPushButton[danger="true"]:hover {{ background: #b91c1c; }}

QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {CARD_BORDER}; background: {SURFACE_SOLID};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border: 1px solid {ACCENT}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: rgba(148,163,184,0.5); border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

QToolTip {{ background: {SURFACE_SOLID}; color: {TEXT}; border: 1px solid {CARD_BORDER}; }}
"""


def glass_card_qss(radius: int = RADIUS_CARD) -> str:
    return (
        f"QFrame#glassCard {{ background: {CARD}; border: 1px solid {CARD_BORDER};"
        f" border-radius: {radius}px; }}"
    )
