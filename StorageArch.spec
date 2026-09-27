# -*- mode: python ; coding: utf-8 -*-
# One-file Windows build of the app. Build from this folder:
#   python -m PyInstaller StorageArch.spec --noconfirm
# The JPEG XL tools are bundled when tools/cjxl.exe and tools/djxl.exe exist (core/tools.py finds them under
# the frozen app's own folder); without them the app still runs, without the JPEG XL strategies.
import os

from PyInstaller.utils.hooks import collect_submodules

binaries = [(os.path.join("tools", exe), "tools") for exe in ("cjxl.exe", "djxl.exe")
            if os.path.isfile(os.path.join("tools", exe))]
datas = []
hiddenimports = collect_submodules("core") + collect_submodules("local_cloud") + ["brotli", "send2trash"]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tests_core", "scripts"],
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
    name="StorageArch",
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
)
