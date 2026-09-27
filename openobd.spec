# PyInstaller spec — builds a single-file Windows exe: openobd.exe
# Build:  pyinstaller openobd.spec   (run from the repo root on Windows)
# Output: dist/openobd.exe
#
# Bundles the seed calibration (data/2010_silverado_full.cal.json) so the exe
# opens on the 2010 Silverado #24 calibration with no external files.

import os
import re

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo)

block_cipher = None

# Windows version resource, derived from openobd/__init__.py so the installed
# exe's version is readable at the boundary ((Get-Item exe).VersionInfo)
# without launching it, and can never drift from the package version.
with open(os.path.join(SPECPATH, 'openobd', '__init__.py'), encoding='utf-8') as _f:
    _ver = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', _f.read())
if not _ver:
    raise SystemExit('openobd/__init__.py: __version__ must be "X.Y.Z"')
_vt = tuple(int(x) for x in _ver.groups()) + (0,)
_vs = '.'.join(_ver.groups())
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_vt, prodvers=_vt),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'Ohio15'),
            StringStruct('FileDescription', 'OpenOBD'),
            StringStruct('FileVersion', _vs),
            StringStruct('InternalName', 'openobd'),
            StringStruct('OriginalFilename', 'openobd.exe'),
            StringStruct('ProductName', 'OpenOBD'),
            StringStruct('ProductVersion', _vs)])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ])

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[('data/2010_silverado_full.cal.json', 'data'),
           ('assets/openobd.ico', 'assets')],
    hiddenimports=['serial', 'serial.tools', 'serial.tools.list_ports',
                   'openobd.gt', 'pyqtgraph'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy is REQUIRED since v0.4.0 (pyqtgraph charting) — do not exclude it
    excludes=['tkinter', 'matplotlib', 'scipy', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='openobd',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # windowed app, no console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/openobd.ico',
    version=version_info,
)
