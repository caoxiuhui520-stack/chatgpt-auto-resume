# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ChatGPT Auto Resume desktop control center.

Build (from the project root):
    .venv\\Scripts\\pyinstaller.exe scripts\\ChatGPTAutoResume.spec --noconfirm --clean

Output: dist\\ChatGPTAutoResume\\ChatGPTAutoResume.exe (onedir — starts fast,
keeps antivirus scan time low, and keeps the Qt plugins discoverable).
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent

block_cipher = None

hiddenimports = [
    "comtypes.stream",
    "pywinauto",
    "pywinauto.uia_defines",
    "pywinauto.keyboard",
    "win32timezone",
    "cramjam",
    "app.discovery.leveldb_reader",
    "app.discovery.provider",
    "app.gui.workers",
    "app.gui.pages",
]

a = Analysis(
    [str(PROJECT_ROOT / "run_gui.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "config.example.yaml"), "."),
        (str(PROJECT_ROOT / "prompts"), "prompts"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
        "PyInstaller",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ChatGPTAutoResume",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ChatGPTAutoResume",
)
