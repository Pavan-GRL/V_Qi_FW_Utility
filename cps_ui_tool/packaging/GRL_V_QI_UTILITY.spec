# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Get the spec file directory, then go up one level to project root
SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_DIR = SPEC_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
ASSETS_DIR = PROJECT_DIR / "assets"
INCLUDE_DIR = PROJECT_DIR / "include"
TOOLS_DIR = PROJECT_DIR / "tools"
CUBEPROG_DIR = TOOLS_DIR / "STM32CubeProgrammer"

datas = [
    (str(ASSETS_DIR / "grl_logo.png"), "assets"),
    (str(ASSETS_DIR / "grl_logo.ico"), "assets"),
    (str(INCLUDE_DIR / "regset.txt"), "include"),
]
if CUBEPROG_DIR.exists():
    datas.append((str(CUBEPROG_DIR), "tools/STM32CubeProgrammer"))
datas += collect_data_files("matplotlib")
datas += collect_data_files("mpl_toolkits")

binaries = []
for package_name in ("libusb", "libusb_package"):
    try:
        binaries += collect_dynamic_libs(package_name)
    except Exception:
        pass


a = Analysis(
    [str(SRC_DIR / "app.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
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
