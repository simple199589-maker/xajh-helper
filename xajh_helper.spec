# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for XAJH helper GUI (onedir).
# Build: pyinstaller xajh_helper.spec
# @author by ak

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

from app.core.bridge_protocol import BRIDGE_BUILD_ID

block_cipher = None
ROOT = Path(SPECPATH).resolve()
PYTHON_ROOT = Path(sys.prefix).resolve()
NATIVE_BIN_SOURCE = Path(
    os.environ.get("XAJH_NATIVE_BIN_DIR") or ROOT / "native" / "bin"
).resolve()
PROFILE_PATH = Path(
    os.environ.get("XAJH_BUILD_PROFILE_PATH")
    or ROOT / "app" / "data" / "build_profile.json"
)
BUILD_CHANNEL = str(os.environ.get("BUILD_CHANNEL") or "prod").strip().lower()
# Optional versioned output dir, e.g. "xajh_helper-v1.0.7" via XAJH_PKG_DIR.
# Lets the build switch to a fresh folder when dist\xajh_helper is locked by a
# running game (staged native bridge DLL cannot be replaced). Default keeps the
# historical dist\xajh_helper layout.
PKG_DIR = str(os.environ.get("XAJH_PKG_DIR") or "xajh_helper").strip()

# Optional tray deps: collect everything so pystray works offline.
pystray_datas, pystray_binaries, pystray_hidden = collect_all("pystray")
pil_datas, pil_binaries, pil_hidden = collect_all("PIL")

datas = [
    # build_profile.json is generated outside the source tree for dev/prod.
    *[
        (str(path), "app/data")
        for path in (ROOT / "app" / "data").iterdir()
        if path.is_file() and path.name != "build_profile.json"
    ],
    (str(PROFILE_PATH), "app/data"),
    # Ship current bridge DLL + injector next to runtime _MEIPASS/native/bin
    (str(NATIVE_BIN_SOURCE / "xajh_bridge.dll"), "native/bin"),
    (
        str(NATIVE_BIN_SOURCE / f"xajh_bridge_{BRIDGE_BUILD_ID}.dll"),
        "native/bin",
    ),
    (str(NATIVE_BIN_SOURCE / "xajh_chat_tap.dll"), "native/bin"),
    (str(NATIVE_BIN_SOURCE / "xajh_team_tap.dll"), "native/bin"),
    (str(NATIVE_BIN_SOURCE / "xajh_inject.exe"), "native/bin"),
    (str(NATIVE_BIN_SOURCE / "dummy_damage_reader.exe"), "native/bin"),
    (str(NATIVE_BIN_SOURCE / "xajh_login_bridge_v2.dll"), "native/bin"),
    (str(NATIVE_BIN_SOURCE / "xajh_login_inject.exe"), "native/bin"),
]
datas += pystray_datas + pil_datas

binaries = pystray_binaries + pil_binaries

# Some embedded/pyenv Python installations are not detected by PyInstaller's
# Tk probe. Ship the standard Tk runtime explicitly so the frozen GUI starts
# on a clean production machine.
TKINTER_ROOT = PYTHON_ROOT / "Lib" / "tkinter"
TK_DLL_ROOT = PYTHON_ROOT / "DLLs"
TK_RUNTIME_ROOT = PYTHON_ROOT / "tcl"
if TKINTER_ROOT.exists():
    datas.append((str(TKINTER_ROOT), "tkinter"))
if TK_RUNTIME_ROOT.exists():
    datas.append((str(TK_RUNTIME_ROOT), "tcl"))
for tk_binary in (TK_DLL_ROOT / "_tkinter.pyd", TK_DLL_ROOT / "tcl86t.dll", TK_DLL_ROOT / "tk86t.dll"):
    if tk_binary.exists():
        binaries.append((str(tk_binary), "."))

hiddenimports = sorted(
    set(
        [
            "app",
            "app.core",
            "app.core.build_profile",
            # Imported at hang-start time; explicitly collect the bundled
            # 北疆疯丐 / 上官霸刀 dungeon target baseline.
            "app.core.dungeon_target_policy",
            "app.core._pack_secret",
            "app.core.game_send",
            "app.ui",
            "common",
            "common.paths",
            "packet",
            "packet.action_capture",
            "packet.action_diff",
            "packet.stream_extract",
            "packet.frame_split",
            "packet.dumpcap_capture",
            "pymem",
            "pymem.process",
            "pymem.memory",
            "psutil",
            "pystray",
            "pystray._win32",
            "PIL",
            "PIL.Image",
            "PIL.ImageDraw",
            "tkinter",
            "tkinter.ttk",
            "tkinter.messagebox",
            "tkinter.filedialog",
            "tkinter.font",
            "_tkinter",
        ]
        + pystray_hidden
        + pil_hidden
    )
)

excluded_modules = [
    "tools",
    "memory",
    "pytest",
    "unittest",
    "IPython",
    "matplotlib",
    "numpy",
    "pandas",
]
if BUILD_CHANNEL not in ("dev", "test"):
    # The frozen shell never opens the legacy workbench in production. Keep
    # memory-writing reverse-engineering probes out of the formal artifact.
    excluded_modules += [
        "app.ui.main_window",
        "app.core.combat_probe",
        "app.core.skill_cast_probe",
    ]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
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
    name="xajh_helper",
    # NOTE: onedir exe name stays "xajh_helper"; only the COLLECT folder below
    # is renamed via PKG_DIR so the versioned output keeps dist\xajh_helper
    # untouched when the default folder is locked.
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=PKG_DIR,
)
