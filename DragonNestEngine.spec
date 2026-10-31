# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 冻结配置：把 ``main.py`` + ``engine/`` 打成一个 onedir 引擎。

产物：``dist/DragonNestEngine/DragonNestEngine.exe`` + ``_internal/``。

两个布局约定不能改：

1. **数据必须是 ``<_MEIPASS>/engine/data``**
   ``engine/data_loader.py`` 的 ``app_base_dir()`` 在冻结后返回 ``sys._MEIPASS``
   （PyInstaller 6 的 onedir 里等于 ``_internal``），再拼 ``engine/data``。
   所以 datas 的目标路径写 ``engine/data``，写别的位置会表现为
   "所有怪/副本/技能表都认不出来"，且不报错。

2. **``version.json`` 必须一起打进去**
   README 里那条提醒：它在包目录外面，spec 的 datas 漏了它，冻结后的引擎
   读不到文件就会静默回落到兜底版本号，表现为"新版本报旧版本"。

引擎只依赖标准库，所以不需要 hiddenimports；``noarchive=True`` 让模块以
独立文件的形式躺在 ``_internal`` 里（``__file__`` 相对定位才成立，也更好排错）。
"""

datas = [
    ('engine/data', 'engine/data'),
    ('engine/assets', 'engine/assets'),
    ('version.json', '.'),
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=True,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DragonNestEngine',
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
    icon='engine/assets/app_icon_build.ico',
    version='engine/build_support/windows_version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DragonNestEngine',
)
