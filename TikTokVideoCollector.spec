# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
project = Path(SPECPATH)
runtime = os.environ.get('TIKTOK_BUILD_RUNTIME_ROOT')
binaries, hooks = [], []
datas = [(str(project / 'assets'), 'assets')]
if runtime:
    # Optional local runtime override; normal Python installations do not need it.
    from PyInstaller.depend import bindepend
    runtime = Path(runtime)
    bindepend.get_python_library_path = lambda: str(runtime / 'python_runtime/python312.dll')
    binaries = [(str(p), '.') for p in (runtime / 'python_runtime').iterdir()
                if p.suffix.lower() in {'.dll', '.pyd'}]
    datas += [(str(runtime / 'tcl_runtime/_tcl_data'), '_tcl_data'),
              (str(runtime / 'tcl_runtime/_tk_data'), '_tk_data')]
    hooks = [str(project / 'hooks')]
a = Analysis([str(project / 'app.py')], pathex=[str(project)], binaries=binaries,
    datas=datas, hiddenimports=['playwright.async_api', 'playwright._impl._driver',
    'mutagen.mp4', 'download_events', 'download_handles'], hookspath=hooks,
    excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='TikTokVideoCollector-v1.0',
    console=False, debug=False, strip=False, upx=False,
    icon=str(project / 'assets/app.ico'), version=str(project / 'version_info.txt'))
