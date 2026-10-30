# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH)

a = Analysis(
    [str(PROJECT_ROOT / "build" / "gui_launcher.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=[(str(PROJECT_ROOT / "lookups"), "lookups")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

contaminated_sources = sorted({
    source
    for _destination, source, _kind in a.binaries
    if "codex-runtimes" in source.replace("/", "\\").lower()
})
if contaminated_sources:
    raise RuntimeError(
        "Refusing to package DLLs from codex-runtimes; run "
        "tools/build_windows.ps1 with a clean PATH."
    )

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SanaDeclarationUploader",
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
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SanaDeclarationUploader",
)
