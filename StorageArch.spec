# -*- mode: python ; coding: utf-8 -*-
# One-file Windows build of the app. Build from this folder:
#   python -m PyInstaller StorageArch.spec --noconfirm
# The JPEG XL tools are bundled when tools/cjxl.exe and tools/djxl.exe exist (core/tools.py finds them under
# the frozen app's own folder); without them the app still runs, without the JPEG XL strategies.
import os
import re

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
                                                 VarStruct, VSVersionInfo)

# The version shown in the file's Properties > Details, read from the one place it is written (core/__init__.py)
# without importing the app.
with open(os.path.join("core", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^CORE_VERSION = "(\d+)\.(\d+)\.(\d+)"$', f.read(), re.MULTILINE)
NUMBERS = tuple(int(n) for n in VERSION.groups()) + (0,)
TEXT = ".".join(VERSION.groups())
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Loay Amer"),
            StringStruct("FileDescription", "StorageArch"),
            StringStruct("FileVersion", TEXT),
            StringStruct("InternalName", "StorageArch"),
            StringStruct("LegalCopyright", "MIT License"),
            StringStruct("OriginalFilename", "StorageArch.exe"),
            StringStruct("ProductName", "StorageArch"),
            StringStruct("ProductVersion", TEXT),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

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
    version=version_info,
)
