"""``python -m app.gui`` launches the desktop control center."""

import sys

from app.gui.main_window import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
