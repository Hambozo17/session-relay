# PyInstaller one-directory, windowed build for Windows x64.
from pathlib import Path
import os
import shutil


ROOT = Path(SPECPATH).resolve()
node_path = os.environ.get("SESSION_RELAY_NODE_PATH") or shutil.which("node.exe") or shutil.which("node")
if not node_path or not Path(node_path).is_file():
    raise SystemExit("Node.js was not found; build.ps1 must provide SESSION_RELAY_NODE_PATH")

datas = [
    (str(ROOT / "orca_pipe.cjs"), "."),
    (str(ROOT / "desktop_bridge.cjs"), "."),
    (str(ROOT / "desktop-settings-policy.json"), "."),
]
hiddenimports = [
    "accounts",
    "app",
    "controller",
    "desktop_adapter",
    "desktop_settings",
    "helper_job",
    "launcher",
    "local_rpc",
    "orca_adapter",
    "recovery_policy",
    "relay_runtime",
    "reset_bank",
    "session_relay",
    "transcript",
    "watchdog",
    "tkinter",
    "tkinter.ttk",
    "websocket",
]

a = Analysis(
    [str(ROOT / "session_relay.py")],
    pathex=[str(ROOT)],
    binaries=[(str(Path(node_path).resolve()), ".")],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SessionRelay",
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
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SessionRelay",
)
