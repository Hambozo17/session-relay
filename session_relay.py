"""Single executable entry point for Session Relay source and Windows builds."""
from __future__ import annotations

import importlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

from relay_runtime import bundled_node, installer_script, is_frozen, node_binary, resource_root


_APPLICATION_MODULES = (
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
    "reset_bank",
    "session_relay",
    "transcript",
    "watchdog",
)
_JS_RESOURCES = ("orca_pipe.cjs", "desktop_bridge.cjs")


def _attach_console() -> bool:
    """Attach CLI invocations to their parent console for a windowed EXE."""
    if sys.stdout is not None and sys.stderr is not None:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            return True
        except OSError:
            return False
    except (AttributeError, OSError):
        return False


def _report(message: str, *, title: str = "Session Relay") -> None:
    if sys.stdout is not None:
        print(message, flush=True)
        return
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        except (AttributeError, OSError):
            pass


def _tk_resource_check() -> tuple[str, str]:
    import tkinter

    interpreter = tkinter.Tcl()
    tcl_version = interpreter.eval("info patchlevel")
    tcl_library = Path(interpreter.eval("info library"))
    if not (tcl_library / "init.tcl").is_file():
        raise RuntimeError(f"Tcl init.tcl is missing from {tcl_library}")

    root = resource_root()
    candidates = []
    if os.environ.get("TK_LIBRARY"):
        candidates.append(Path(os.environ["TK_LIBRARY"]))
    candidates.extend((
        root / "tk8.6",
        root / "tk8.6.13",
        root / "_tk_data" / "tk8.6",
        root / "_tk_data",
        tcl_library.parent / "tk8.6",
        Path(sys.base_prefix) / "tcl" / "tk8.6",
    ))
    tk_library = next((path for path in candidates if (path / "tk.tcl").is_file()), None)
    if tk_library is None:
        raise RuntimeError("Tk scripts (tk.tcl) are missing")
    if tkinter.TkVersion <= 0:
        raise RuntimeError("The Tk runtime version is unavailable")
    return tcl_version, str(tk_library.resolve())


def smoke_test() -> tuple[int, str]:
    """Check imports and packaged runtime files without opening a UI or app."""
    results = []
    failures = []
    for name in _APPLICATION_MODULES:
        try:
            importlib.import_module(name)
            results.append(f"import {name}: OK")
        except Exception as exc:
            failures.append(f"import {name}: {type(exc).__name__}: {exc}")

    try:
        tcl_version, tk_library = _tk_resource_check()
        results.append(f"Tcl/Tk: Tcl {tcl_version}, Tk {tk_library}: OK")
    except Exception as exc:
        failures.append(f"Tcl/Tk: {type(exc).__name__}: {exc}")

    root = resource_root()
    resources = [root / name for name in _JS_RESOURCES]
    policy = root / "desktop-settings-policy.json"
    for path in (*resources, policy):
        if path.is_file():
            results.append(f"resource {path.name}: OK")
        else:
            failures.append(f"resource {path.name}: missing at {path}")
    if policy.is_file():
        try:
            json.loads(policy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures.append(f"resource {policy.name}: invalid JSON: {exc}")

    node = bundled_node() if is_frozen() else node_binary()
    if not node or not Path(node).is_file():
        failures.append("Node.js: bundled node.exe is missing" if is_frozen() else "Node.js: node.exe is unavailable")
    else:
        try:
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True,
            ).stdout.strip()
            for path in resources:
                checked = subprocess.run(
                    [node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
                if checked.returncode:
                    raise RuntimeError(f"{path.name}: {checked.stderr.strip() or checked.returncode}")
            source = "bundled Node.js" if is_frozen() else "Node.js on PATH"
            results.append(f"Node.js {version}; {source} and JS syntax: OK")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            failures.append(f"Node.js: {type(exc).__name__}: {exc}")

    lines = [*results, *failures]
    lines.append(f"Smoke test: {'FAILED' if failures else 'OK'}")
    return (1 if failures else 0), "\n".join(lines)


def _run_installer() -> int:
    if os.name != "nt":
        _report("The standalone installer is supported on Windows x64 only.")
        return 2
    installer = installer_script()
    powershell = shutil.which("powershell.exe")
    if not powershell:
        powershell = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    if not installer.is_file():
        _report(f"Installer script is missing: {installer}")
        return 2
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(installer)],
            cwd=str(installer.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except OSError as exc:
        _report(f"Could not start the Windows installer: {exc}")
        return 1
    return result.returncode


def _run_role(role: str) -> int:
    module = {
        "panel": "app",
        "monitor": "controller",
        "supervisor": "watchdog",
        "open": "launcher",
    }[role]
    if is_frozen():
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    else:
        filename = {
            "panel": "app.py",
            "monitor": "controller.py",
            "supervisor": "watchdog.py",
            "open": "launcher.py",
        }[role]
        runpy.run_path(str(resource_root() / filename), run_name="__main__")
    return 0


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    _attach_console()
    if not arguments:
        return _run_role("open")
    if arguments == ["--version"]:
        from controller import VERSION

        _report(VERSION)
        return 0
    if arguments == ["--smoke-test"]:
        status, message = smoke_test()
        _report(message, title="Session Relay smoke test")
        return status
    if arguments == ["--install"]:
        return _run_installer()
    roles = {f"--{role}": role for role in ("panel", "monitor", "supervisor", "open")}
    if len(arguments) == 1 and arguments[0] in roles:
        return _run_role(roles[arguments[0]])
    _report("Usage: SessionRelay.exe [--open|--panel|--monitor|--supervisor|--install|--version|--smoke-test]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
