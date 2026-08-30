from pathlib import Path


project_root = Path(SPECPATH).resolve()
entry_script = project_root / "src" / "sales_mobile_ingest" / "desktop_entry.py"
datas = [(str(project_root / "scripts" / "mtp_bridge.ps1"), "scripts")]
datas.extend(
    (str(path), "contract")
    for path in sorted((project_root / "contract").glob("*.schema.json"))
)

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root / "src")],
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
    [],
    exclude_binaries=True,
    name="SalesMobileIngest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name="SalesMobileIngest",
)
