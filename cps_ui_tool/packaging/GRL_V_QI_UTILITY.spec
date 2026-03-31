# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

# Get the spec file directory, then go up one level to project root
SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_DIR = SPEC_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
ASSETS_DIR = PROJECT_DIR / "assets"
INCLUDE_DIR = PROJECT_DIR / "include"

datas = [
    (str(ASSETS_DIR / "grl_logo.png"), "assets"),
    (str(ASSETS_DIR / "grl_logo.ico"), "assets"),
    (str(INCLUDE_DIR / "regset.txt"), "include"),
]
datas += collect_data_files("matplotlib")
datas += collect_data_files("mpl_toolkits")


a = Analysis(
    [str(SRC_DIR / "app.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GRL_V_QI_UTILITY",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ASSETS_DIR / "grl_logo.ico")],
)
