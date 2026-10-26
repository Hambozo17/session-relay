"""Runtime paths and process commands shared by source and frozen builds."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parent
_ROLE_MODULES = {
    "panel": "app.py",
    "monitor": "controller.py",
    "supervisor": "watchdog.py",
    "open": "launcher.py",
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Directory containing app modules and bundled JS/Tcl support files."""
    if is_frozen():
        root = getattr(sys, "_MEIPASS", None)
        if root:
            return Path(root).resolve()
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def bundle_root() -> Path:
    """Directory containing SessionRelay.exe and its adjacent installer."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def installer_script() -> Path:
    """The installer lives beside SessionRelay.exe in a frozen package."""
    return bundle_root() / "install-bundle.ps1"


def command(role: str) -> list[str]:
    """Return the process command for a role in source or frozen mode."""
    if role not in _ROLE_MODULES:
        raise ValueError(f"Unknown Session Relay role: {role}")
    if is_frozen():
        return [sys.executable, f"--{role}"]

    pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
    executable = str(pythonw) if os.name == "nt" and pythonw.is_file() else sys.executable
    return [executable, str((_SOURCE_ROOT / _ROLE_MODULES[role]).resolve())]


def process_specs(role: str, *, include_source_interpreters: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """Return exact executable/argument pairs used to start a role."""
    if role not in _ROLE_MODULES:
        raise ValueError(f"Unknown Session Relay role: {role}")
    if is_frozen():
        return [(str(Path(sys.executable).resolve()), (f"--{role}",))]

    script = str((_SOURCE_ROOT / _ROLE_MODULES[role]).resolve())
    executable = command(role)[0]
    executables = [executable]
    if include_source_interpreters and os.name == "nt":
        parent = Path(sys.executable).resolve().parent
        executables.extend(str(parent / name) for name in ("python.exe", "pythonw.exe"))
    unique = dict.fromkeys(str(Path(item).resolve()) for item in executables)
    return [(item, (script,)) for item in unique]


def command_line_variants(executable: str, arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Windows command-line spellings emitted by Python and Task Scheduler."""
    executable = str(executable)
    normal_executable = subprocess.list2cmdline([executable])
    quoted_executable = '"' + executable + '"'
    normal_arguments = subprocess.list2cmdline(list(arguments))
    quoted_arguments = " ".join('"' + argument + '"' for argument in arguments)
    suffixes = {normal_arguments, quoted_arguments}
    lines = []
    for exe in {normal_executable, quoted_executable}:
        for suffix in suffixes:
            lines.append(exe + (" " + suffix if suffix else ""))
    return tuple(dict.fromkeys(lines))


def process_query(role: str, *, include_source_interpreters: bool = False) -> list[dict[str, object]]:
    """Serialize exact process identities for a PowerShell/CIM query."""
    specs = process_specs(role, include_source_interpreters=include_source_interpreters)
    return [
        {"executable": executable, "command_lines": command_line_variants(executable, arguments)}
        for executable, arguments in specs
    ]


def bundled_node() -> str | None:
    """Return the packaged Node executable, without consulting PATH."""
    candidates = []
    if is_frozen():
        candidates.append(resource_root() / "node.exe")
        candidates.append(bundle_root() / "node.exe")
    else:
        candidates.append(_SOURCE_ROOT / "node.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def node_binary() -> str | None:
    """Prefer bundled Node and retain PATH-based source installations."""
    return bundled_node() or shutil.which("node.exe") or shutil.which("node")
