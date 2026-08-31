# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

a = Analysis(
    ['worker.py'],
    pathex=[str(Path(SPECPATH).parent)],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='rustdesk-worker',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False,
)
