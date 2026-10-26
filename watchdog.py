"""Windows Task Scheduler owns this supervisor independently of any chat."""
from __future__ import annotations
import base64, ctypes, json, os, pathlib, subprocess, sys, time
from accounts import LOCALDATA
from local_rpc import HIDDEN
from relay_runtime import bundle_root, command, process_query

ROOT = bundle_root()

def monitor_pids():
    if os.name != 'nt':
        return []
    expected = json.dumps(process_query('monitor', include_source_interpreters=True), ensure_ascii=True)
    encoded = base64.b64encode(expected.encode('utf-8')).decode('ascii')
    query = r"""
$expected = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')))
foreach ($process in Get-CimInstance Win32_Process) {
    if (-not $process.ExecutablePath -or -not $process.CommandLine) { continue }
    try { $actual = [IO.Path]::GetFullPath($process.ExecutablePath) } catch { continue }
    foreach ($spec in $expected) {
        if ([StringComparer]::OrdinalIgnoreCase.Equals($actual, [string]$spec.executable) -and
            @($spec.command_lines) -contains [string]$process.CommandLine) {
            [Console]::WriteLine($process.ProcessId)
            break
        }
    }
}
""".replace('__PAYLOAD__', encoded)
    powershell = pathlib.Path(os.environ.get('WINDIR', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    query_bytes = base64.b64encode(query.encode('utf-16le')).decode('ascii')
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', query_bytes],
                            capture_output=True, text=True, timeout=20, creationflags=HIDDEN)
    if result.returncode: raise RuntimeError('Cannot check monitor process')
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]

def _pid_is_running(pid):
    if os.name == 'nt':
        return _windows_pid_is_running(pid)
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True

def _windows_pid_is_running(pid):
    """Probe a PID with a waitable handle; never signal a process on Windows."""
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
    except (AttributeError, OSError):
        return True
    if not handle:
        error = ctypes.get_last_error()
        if error in (87, 1168):  # ERROR_INVALID_PARAMETER / ERROR_NOT_FOUND
            return False
        return True  # Access denied and unknown errors are conservatively live.
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0:  # WAIT_OBJECT_0: process has exited.
            return False
        if result == 0x00000102:  # WAIT_TIMEOUT: process is still active.
            return True
        return True  # WAIT_FAILED or any unexpected result is unknown.
    except (AttributeError, OSError):
        return True
    finally:
        try:
            kernel.CloseHandle(handle)
        except (AttributeError, OSError):
            pass

def _upgrade_is_active(marker):
    try:
        if time.time() - marker.stat().st_mtime < 300:
            return True
        owner = marker.read_text(encoding='utf-8').strip()
        return _pid_is_running(owner) if owner.isdigit() else False
    except OSError:
        return False

def _supervisor_stop_requested(marker):
    try:
        if time.time() - marker.stat().st_mtime < 300:
            return True
        request = json.loads(marker.read_text(encoding='utf-8'))
        return _pid_is_running(request.get('pid'))
    except (OSError, ValueError, TypeError):
        return False

def ensure_monitor():
    marker = LOCALDATA / 'upgrade-in-progress'
    if marker.exists() and _upgrade_is_active(marker): return
    if monitor_pids(): return
    subprocess.Popen(command('monitor'), creationflags=HIDDEN, cwd=str(ROOT))

def supervise():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, 'Local\\SessionRelaySupervisor')
    if ctypes.get_last_error() == 183: return
    stop_request = LOCALDATA / 'supervisor-stop-request.json'
    while True:
        if stop_request.exists() and _supervisor_stop_requested(stop_request):
            stop_request.unlink(missing_ok=True)
            return
        try: ensure_monitor()
        except Exception: pass  # Retry without ever changing account or chat state.
        time.sleep(10)

if __name__ == '__main__': supervise()
