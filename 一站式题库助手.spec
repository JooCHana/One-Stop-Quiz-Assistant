# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas = [(str(root/'resources'), 'resources')]
binaries, hiddenimports = [], []
for package in ('rapidocr_onnxruntime', 'onnxruntime', 'cv2'):
    kwargs = {'filter_submodules': lambda name: name == 'onnxruntime' or name.startswith('onnxruntime.capi')} if package == 'onnxruntime' else {}
    d, b, h = collect_all(package, **kwargs)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ['openpyxl', 'xlrd', 'xlwt', 'legacy_doc']
a = Analysis([str(root/'main.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=['torch', 'tensorflow', 'matplotlib', 'pandas', 'scipy', 'IPython'],
             hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='一站式题库助手', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=True, console=False,
          disable_windowed_traceback=False)
