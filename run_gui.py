"""PyInstaller entry point for the desktop control center."""

import multiprocessing

from app.gui.main_window import run_gui

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(run_gui())
