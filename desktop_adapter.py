from __future__ import annotations

import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote
from recovery_policy import proxy_session, recovery_error
from relay_runtime import node_binary


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_SETTINGS = (
    "approvalPolicy", "approvalsReviewer", "sandboxPolicy", "permissions",
    "runtimeWorkspaceRoots", "model", "effort", "collaborationMode", "cwd",
    "serviceTier", "multiAgentMode",
)
_QUOTA_ERRORS = {"usageLimitExceeded", "quota", "login", "rate_limit_exceeded"}
_ACTIVE_GOALS = {None, "active", "usage_limited"}
_PACKAGE_LOCATION: pathlib.Path | None = None


class DesktopAdapterError(RuntimeError):
    """A sanitized error suitable for the controller's status panel."""


class DesktopProtocolError(DesktopAdapterError):
    pass


class DesktopNoOwnerError(DesktopAdapterError):
    pass


class DesktopUncertainError(DesktopAdapterError):
    """A mutation may have reached Desktop; the operation must not be replayed."""


def quit_window(windows, foreground):
    # Electron also owns transparent overlays and offscreen prewarm windows.
    # A package PID and Chrome class alone do not identify its keyboard window.
    candidates = [hwnd for hwnd, style, extended in windows
                  if not style & 0x40000000 and not extended & (0x80 | 0x08000000)]
    if not candidates:
        raise DesktopAdapterError('Codex Desktop has no verifiable application window to close')
    return foreground if foreground in candidates else candidates[0]


def pending_desktop_recovery(operation, rows, account):
    """Only finish a verified restart which never reached a prompt submission."""
    if (operation.get('phase') not in ('desktop-reopen-intent','desktop-open-thread-intent')
            or not account or operation.get('target') != account or not operation.get('sessions')):
        return None
    by_id = {s['id']:s for s in rows};pending=[]
    for sid in operation['sessions']:
        row=by_id.get(sid);before=operation.get('recovery_context',{}).get(sid)
        if (not row or not before or row.get('account_key') != account or not row.get('_owner')
                or row.get('waiting') or row.get('is_child') or not row.get('supported') or row.get('_goal_deferred')
                or row.get('_goal_mismatch') or row.get('_ephemeral') or row.get('goal_status') not in (None,'active')
                or row.get('_settings_fingerprint') != before.get('settings')):return None
        goal=dict(before['goal_record']) if before.get('goal_record') else None
        if goal and goal.get('status')=='usage_limited':goal['status']='active'
        if not DesktopAdapter._goal_progress_preserved(goal,row.get('_goal_record')):return None
        if row.get('turn_id') != before.get('turn'):
            # Native automatic Goal recovery is already working; do not send again.
            if not goal or goal.get('status')!='active' or row.get('status') not in ('running','idle'):return None
            continue
        receipt=operation.get('interruption_receipts',{}).get(sid,{})
        own_stop=(row.get('status')=='interrupted' and receipt.get('relay_interrupted')
                  and receipt.get('turn')==row.get('turn_id') and receipt.get('settings')==before.get('settings'))
        usage_failure=row.get('status')=='failed' and recovery_error(row.get('error_kind'))
        if not own_stop and not usage_failure:return None
        pending.append(sid)
    return pending


def _installed_package_location() -> pathlib.Path:
    global _PACKAGE_LOCATION
    if _PACKAGE_LOCATION is not None:
        return _PACKAGE_LOCATION
    if os.name != "nt":
        raise DesktopProtocolError("Codex Desktop recovery requires Windows")
    powershell = shutil.which("powershell.exe") or str(pathlib.Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    command = "$p=Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1 -ExpandProperty InstallLocation; if($p){[Console]::WriteLine($p)}"
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopProtocolError("Installed Codex Desktop package could not be queried") from exc
    location = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    package = pathlib.Path(location)
    if result.returncode != 0 or not re.match(r"^OpenAI\.Codex_[0-9].*_x64__[^\\/]+$", package.name, re.I):
        raise DesktopProtocolError("Installed Codex Desktop package identity could not be verified")
    if not (package / "app" / "resources" / "app.asar").is_file():
        raise DesktopProtocolError("Installed Codex Desktop protocol source is unavailable")
    _PACKAGE_LOCATION = package
    return package


class _BridgeClient:
    def __init__(self, bridge_path: pathlib.Path, node: str | None = None):
        self.bridge_path = bridge_path
        self.node = node or node_binary()
        self.process = None
        self._events: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._event_error: str | None = None
        self._lock = threading.Lock()
        self._closed = False

    def _reader(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            if event == "connected":
                self._ready.set()
            elif event in ("error", "incompatible", "disconnected"):
                # Keep only a classification. Never forward raw child-process errors.
                self._event_error = str(item.get("kind") or event)
                if event == "incompatible":
                    self._event_error = "incompatible"
                self._ready.set()
            if "id" in item:
                self._events.put(item)

    def _stderr_drain(self):
        if self.process and self.process.stderr:
            for _line in self.process.stderr:
                pass

    def start(self):
        if self.process is not None:
            if self.process.poll() is None and self._ready.is_set() and not self._event_error:
                return
            raise DesktopAdapterError("Codex Desktop bridge is unavailable")
        if not self.node or not self.bridge_path.is_file():
            raise DesktopProtocolError("Codex Desktop bridge or Node.js is unavailable")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            env = dict(os.environ)
            env["CODEX_APP_ASAR"] = str(_installed_package_location() / "app" / "resources" / "app.asar")
            self.process = subprocess.Popen(
                [self.node, str(self.bridge_path)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=flags, env=env,
            )
        except OSError as exc:
            raise DesktopAdapterError("Codex Desktop bridge could not start") from exc
        threading.Thread(target=self._reader, name="session-relay-desktop-stdout", daemon=True).start()
        threading.Thread(target=self._stderr_drain, name="session-relay-desktop-stderr", daemon=True).start()
        if not self._ready.wait(12):
            self.close()
            raise DesktopAdapterError("Codex Desktop owner pipe did not become ready")
        if self._event_error:
            reason = self._event_error
            self.close()
            if reason == "incompatible":
                raise DesktopProtocolError("Installed Codex Desktop protocol is incompatible")
            raise DesktopAdapterError("Codex Desktop owner pipe is unavailable")

    def call(self, command: dict, timeout: float = 20.0):
        with self._lock:
            self.start()
            if self.process is None or self.process.stdin is None or self.process.poll() is not None:
                raise DesktopAdapterError("Codex Desktop bridge disconnected")
            request_id = str(uuid.uuid4())
            payload = dict(command)
            payload["id"] = request_id
            try:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                if command.get("op") in ("interrupt", "resume"):
                    raise DesktopUncertainError("Codex Desktop mutation outcome is uncertain; do not retry") from exc
                raise DesktopAdapterError("Codex Desktop bridge disconnected") from exc
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    response = self._events.get(timeout=min(0.25, deadline - time.monotonic()))
                except queue.Empty:
                    if self.process.poll() is not None:
                        break
                    continue
                if response.get("id") != request_id:
                    continue
                if response.get("kind") == "uncertain":
                    raise DesktopUncertainError("Codex Desktop mutation outcome is uncertain; do not retry")
                if response.get("kind") == "incompatible":
                    raise DesktopProtocolError("Installed Codex Desktop protocol is incompatible")
                if response.get("kind"):
                    if command.get("op") in ("interrupt", "resume") and response.get("kind") not in ("state_changed", "no_owner", "rejected", "incompatible"):
                        raise DesktopUncertainError("Codex Desktop mutation outcome is uncertain; do not retry")
                    raise DesktopAdapterError("Codex Desktop request could not be completed")
                return response.get("result")
            if command.get("op") in ("interrupt", "resume"):
                raise DesktopUncertainError("Codex Desktop mutation timed out; do not retry")
            raise DesktopAdapterError("Codex Desktop read request timed out")

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin:
                process.stdin.write(json.dumps({"id": str(uuid.uuid4()), "op": "close"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            # This is only our Node helper, never the Codex application.
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


class _WindowsDesktopControl:
    """Normal Windows close/reopen operations for one installed Codex package."""

    def __init__(self):
        self._package = None

    def package(self):
        if self._package:
            return self._package
        candidate = _installed_package_location()
        manifest = candidate / "AppxManifest.xml"
        executable = candidate / "app" / "ChatGPT.exe"
        try:
            tree = ET.parse(manifest)
        except (OSError, ET.ParseError) as exc:
            raise DesktopProtocolError("Installed Codex Desktop manifest is unavailable") from exc
        app_id = None
        has_protocol = False
        for element in tree.iter():
            name = element.tag.rsplit("}", 1)[-1]
            if name == "Application" and element.attrib.get("Executable", "").lower().replace("\\", "/") == "app/chatgpt.exe":
                app_id = element.attrib.get("Id")
            if name == "Protocol" and element.attrib.get("Name", "").lower() == "codex":
                has_protocol = True
        if not app_id or not has_protocol or not executable.is_file():
            raise DesktopProtocolError("Installed Codex Desktop launch identity could not be verified")
        family = candidate.name.split("_", 1)[0] + "_" + candidate.name.split("__", 1)[1]
        self._package = {"root": candidate.resolve(), "exe": executable.resolve(), "family": family, "app_id": app_id}
        return self._package

    def _processes(self):
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid = ctypes.c_void_p(-1).value
        if snapshot == invalid:
            raise DesktopAdapterError("Codex Desktop process inventory is unavailable")
        rows = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
                return rows
            while True:
                if entry.szExeFile.lower() == "chatgpt.exe":
                    pid = int(entry.th32ProcessID)
                    handle = kernel.OpenProcess(0x1000, False, pid)
                    if handle:
                        try:
                            size = wintypes.DWORD(32768)
                            buffer = ctypes.create_unicode_buffer(size.value)
                            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                                rows.append((pid, int(entry.th32ParentProcessID), pathlib.Path(buffer.value)))
                        finally:
                            kernel.CloseHandle(handle)
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel.CloseHandle(snapshot)
        return rows

    def _matching(self):
        package = self.package()
        expected = os.path.normcase(str(package["exe"]))
        return [(pid, parent, image) for pid, parent, image in self._processes()
                if os.path.normcase(str(image)) == expected]

    def close_app(self, timeout: float = 35.0):
        import ctypes
        from ctypes import wintypes

        rows = self._matching()
        if not rows:
            raise DesktopAdapterError("Codex Desktop is no longer running")
        by_pid = {pid: parent for pid, parent, _image in rows}
        roots = [pid for pid, parent in by_pid.items() if parent not in by_pid]
        if len(roots) != 1:
            raise DesktopAdapterError("Codex Desktop has multiple process roots; refusing global restart")
        package_pids = set(by_pid)
        windows = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

        @enum_proc
        def visit(hwnd, _param):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                class_name = ctypes.create_unicode_buffer(100)
                user32.GetClassNameW(hwnd, class_name, 100)
                if pid.value in package_pids and class_name.value == 'Chrome_WidgetWin_1':
                    windows.append((hwnd, user32.GetWindowLongW(hwnd, -16), user32.GetWindowLongW(hwnd, -20)))
            return True

        user32.EnumWindows(visit, 0)
        if not windows:
            raise DesktopAdapterError("Codex Desktop has no verifiable application window to close")
        # Windows deliberately keeps Codex alive after WM_CLOSE. Its installed
        # File > Quit menu uses Ctrl+Q. Send this only to a verified foreground
        # package window, never to whichever application happens to have focus.
        hwnd = quit_window(windows, user32.GetForegroundWindow())
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if user32.GetForegroundWindow() != hwnd:
            # A scheduled task does not inherit the foreground process's input
            # queue. Share it briefly, then detach before delivering any keys.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentThreadId.restype = wintypes.DWORD
            user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            user32.AttachThreadInput.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            caller = kernel.GetCurrentThreadId()
            foreground = user32.GetForegroundWindow()
            thread_ids = {user32.GetWindowThreadProcessId(h, None) for h in (foreground, hwnd) if h}
            attached = []
            try:
                for thread_id in thread_ids - {caller}:
                    if user32.AttachThreadInput(caller, thread_id, True): attached.append(thread_id)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                for thread_id in reversed(attached): user32.AttachThreadInput(caller, thread_id, False)
            time.sleep(0.15)
        if user32.GetForegroundWindow() != hwnd:
            raise DesktopAdapterError('Codex Desktop could not be focused for its normal Quit action')

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                        ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                        ('dwExtraInfo', ctypes.c_size_t)]
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                        ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]
        class INPUTUNION(ctypes.Union):
            _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]
        class INPUT(ctypes.Structure):
            _anonymous_ = ('data',)
            _fields_ = [('type', wintypes.DWORD), ('data', INPUTUNION)]
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        keys = [(0x11, 0), (0x51, 0), (0x51, 2), (0x11, 2)]
        inputs = (INPUT * len(keys))(*(INPUT(type=1, ki=KEYBDINPUT(wVk=key, dwFlags=flags)) for key, flags in keys))
        if user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT)) != len(inputs):
            raise DesktopAdapterError('Codex Desktop normal Quit action could not be delivered')

        # The app can ask to confirm quitting because scheduled tasks are enabled.
        # Approve only its exact native Quit dialog; other prompts remain untouched.
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        def confirm_quit():
            dialogs = []
            @enum_proc
            def find_dialog(handle, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                title = ctypes.create_unicode_buffer(200)
                cls = ctypes.create_unicode_buffer(100)
                user32.GetWindowTextW(handle, title, 200)
                user32.GetClassNameW(handle, cls, 100)
                if pid.value in package_pids and cls.value == '#32770' and title.value in ('Quit Codex?', 'Quit ChatGPT?'):
                    dialogs.append(handle)
                return True
            user32.EnumWindows(find_dialog, 0)
            for dialog in dialogs:
                buttons = {}
                @enum_proc
                def find_button(handle, _):
                    title = ctypes.create_unicode_buffer(100)
                    cls = ctypes.create_unicode_buffer(100)
                    user32.GetWindowTextW(handle, title, 100)
                    user32.GetClassNameW(handle, cls, 100)
                    if cls.value == 'Button': buttons[title.value.replace('&', '')] = handle
                    return True
                user32.EnumChildWindows(dialog, find_button, 0)
                if 'Quit' in buttons and 'Cancel' in buttons:
                    user32.PostMessageW(buttons['Quit'], 0x00F5, 0, 0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._matching():
                return
            confirm_quit()
            time.sleep(0.25)
        raise DesktopAdapterError("Codex Desktop did not close normally; no process was forced to stop")

    def launch_app(self):
        package = self.package()
        if self._matching():
            raise DesktopAdapterError("Codex Desktop is already running after close")
        try:
            os.startfile("shell:AppsFolder\\" + package["family"] + "!" + package["app_id"])
        except OSError as exc:
            raise DesktopAdapterError("Codex Desktop could not be reopened") from exc
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._matching():
                return
            time.sleep(0.25)
        raise DesktopAdapterError("Codex Desktop did not reopen")

    def open_thread(self, thread_id: str):
        if not isinstance(thread_id,str) or not _UUID.fullmatch(thread_id):
            raise DesktopAdapterError('Invalid original conversation identity')
        package=self.package()
        if not self._matching():raise DesktopAdapterError('Codex Desktop is not running')
        env=dict(os.environ)
        for key in ('CODEX_HOME','ELECTRON_RUN_AS_NODE'):env.pop(key,None)
        try:
            subprocess.Popen([str(package['exe']), 'codex://threads/'+thread_id+'?hostId=local'],
                env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as exc:
            raise DesktopAdapterError('Codex Desktop could not request the original conversation') from exc


class DesktopAdapter:
    """Owner-routed adapter for Codex Desktop; it never uses app-server RPC."""

    def __init__(self, accounts, *, codex_home=None, bridge=None, bridge_factory=None, app_control=None, node=None, bridge_path=None, recovery_timeout=120):
        self.accounts = accounts
        self.codex_home = pathlib.Path(codex_home or (pathlib.Path.home() / ".codex"))
        self.bridge_path = pathlib.Path(bridge_path or pathlib.Path(__file__).with_name("desktop_bridge.cjs"))
        self._bridge = bridge
        self._node = node
        self._bridge_factory = bridge_factory
        self._app_control = app_control or _WindowsDesktopControl()
        self._lock = threading.RLock()
        self._attempted_sends: set[tuple[str, str, str]] = set()
        self._inventory_complete = False
        self._goal_rows = {}
        self.recovery_receipts = {}
        self._last_diagnostics = {}
        self._recovery_timeout = recovery_timeout

    @staticmethod
    def _database(path: pathlib.Path):
        if not path.is_file():
            return None
        uri = path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=2)

    def _catalog(self):
        path = self.codex_home / "state_5.sqlite"
        try:
            connection = self._database(path)
            if connection is None:
                raise DesktopAdapterError("Codex Desktop thread database is unavailable")
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                if "id" not in columns:
                    raise DesktopProtocolError("Codex Desktop thread database schema is incompatible")
                safe_fields = ("title", "name", "cwd", "model", "reasoning_effort", "updated_at", "updated_at_ms",
                               "sandbox_policy", "approval_mode", "is_archived", "archived", "source", "agent_path")
                selected = [field for field in safe_fields if field in columns]
                sql = "SELECT id" + (", " + ", ".join('"' + field + '"' for field in selected) if selected else "") + " FROM threads"
                rows = connection.execute(sql).fetchall()
                if len(rows) > 5000:
                    raise DesktopProtocolError("Codex Desktop thread inventory exceeds the supported limit")
                result = {}
                for row in rows:
                    thread_id = row[0]
                    if isinstance(thread_id, str) and _UUID.fullmatch(thread_id):
                        result[thread_id] = dict(zip(selected, row[1:]))
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise DesktopAdapterError("Codex Desktop thread database could not be read") from exc
        self._goal_rows = self._read_goals()
        return result

    def _read_goals(self):
        path = self.codex_home / "goals_1.sqlite"
        connection = self._database(path)
        if connection is None:
            return {}
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "thread_goals" not in tables:
                raise DesktopProtocolError("Codex Desktop Goal database schema is incompatible")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(thread_goals)")}
            required = {"thread_id", "status"}
            if not required <= columns:
                raise DesktopProtocolError("Codex Desktop Goal database schema is incompatible")
            safe = ("thread_id", "goal_id", "status", "token_budget", "tokens_used", "time_used_seconds",
                    "created_at_ms", "updated_at_ms")
            fields = [field for field in safe if field in columns]
            sql = "SELECT " + ", ".join('"' + field + '"' for field in fields) + " FROM thread_goals"
            rows = connection.execute(sql).fetchall()
            result = {}
            for row in rows:
                value = dict(zip(fields, row))
                thread_id = value.pop("thread_id")
                value["deferred"] = False
                if "thread_goal_continuation_deferrals" in tables:
                    defer_cols = {r[1] for r in connection.execute("PRAGMA table_info(thread_goal_continuation_deferrals)")}
                    if "thread_id" in defer_cols:
                        value["deferred"] = connection.execute(
                            "SELECT 1 FROM thread_goal_continuation_deferrals WHERE thread_id=? LIMIT 1", (thread_id,)
                        ).fetchone() is not None
                result[thread_id] = value
            return result
        except sqlite3.Error as exc:
            raise DesktopAdapterError("Codex Desktop Goal state could not be read safely") from exc
        finally:
            connection.close()

    def _client(self):
        if self._bridge is None:
            self._bridge = self._bridge_factory() if self._bridge_factory else _BridgeClient(self.bridge_path, self._node)
        return self._bridge

    def _scan(self, catalog=None, wait_ms=2500, owner_limit=None, owner_timeout_ms=None):
        catalog = catalog if catalog is not None else self._catalog()
        command = {"op": "scan", "threads": list(catalog), "wait_ms": wait_ms}
        if owner_limit is not None:
            command["owner_limit"] = owner_limit
        if owner_timeout_ms is not None:
            command["owner_timeout_ms"] = owner_timeout_ms
        result = self._client().call(command, timeout=60)
        if not isinstance(result, dict) or result.get("compatible") is not True:
            raise DesktopProtocolError("Installed Codex Desktop protocol is incompatible")
        rows = result.get("sessions")
        unresolved = result.get("unresolved")
        if not isinstance(rows, list) or not isinstance(unresolved, list):
            raise DesktopProtocolError("Codex Desktop returned an invalid session inventory")
        self._inventory_complete = not unresolved
        self._last_diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        current_key = self.accounts.current_desktop_key()
        sessions = []
        for raw in rows:
            if not isinstance(raw, dict) or not _UUID.fullmatch(str(raw.get("id", ""))):
                continue
            thread_id = raw["id"]
            db = catalog.get(thread_id, {})
            goal = self._goal_rows.get(thread_id)
            ipc_goal = raw.get("goal_status")
            db_goal = goal.get("status") if goal else None
            mismatch = ipc_goal is not None and (goal is None or db_goal != ipc_goal)
            settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
            public = {
                "id": thread_id,
                "title": raw.get("title") or db.get("title") or "Codex conversation",
                "app": "codex",
                "status": raw.get("status") if raw.get("status") in {"running", "idle", "failed", "interrupted", "unknown"} else "unknown",
                "goal_status": db_goal if goal else ipc_goal,
                "account_key": current_key,
                "cwd": raw.get("cwd") or db.get("cwd"),
                "waiting": bool(raw.get("waiting")),
                "provider": "codex",
                "model_provider": raw.get("model_provider") or settings.get("modelProvider"),
                "turn_id": raw.get("turn_id"),
                "_turn_started_at_ms": raw.get("turn_started_at_ms"),
                "_client_user_message_id": raw.get("client_user_message_id"),
                "error_kind": raw.get("error_kind"),
                "is_child": bool(raw.get("is_child")),
                "model": raw.get("model") or settings.get("model") or db.get("model"),
                "effort": raw.get("effort") or settings.get("effort") or db.get("reasoning_effort"),
                "supported": not bool(raw.get("ephemeral")) and not mismatch,
                "_owner": raw.get("owner"),
                "_runtime_status": raw.get("runtime_status"),
                "_settings": settings,
                "_settings_fingerprint": raw.get("settings_fingerprint"),
                "_goal_signature": self._goal_signature(goal),
                "_goal_record": dict(goal) if goal else None,
                "_goal_deferred": bool(goal and goal.get("deferred")),
                "_goal_mismatch": mismatch,
                "_ephemeral": bool(raw.get("ephemeral")),
            }
            sessions.append(public)
        return sessions

    @staticmethod
    def _goal_signature(goal):
        if not goal:
            return None
        # Objective text is deliberately excluded; Goal accounting remains in the stable home DB.
        keys = ("goal_id", "status", "token_budget", "tokens_used", "time_used_seconds", "created_at_ms", "updated_at_ms", "deferred")
        return tuple((key, goal.get(key)) for key in keys)

    @staticmethod
    def _goal_progress_preserved(before, after):
        if before is None or after is None:
            return before is None and after is None
        for key in ("goal_id", "status", "token_budget", "created_at_ms", "deferred"):
            if before.get(key) != after.get(key):
                return False
        for key in ("tokens_used", "time_used_seconds"):
            old_value, new_value = before.get(key), after.get(key)
            if old_value is None or new_value is None:
                if old_value != new_value:
                    return False
            elif not isinstance(old_value, (int, float)) or not isinstance(new_value, (int, float)) or new_value < old_value:
                return False
        return True

    @staticmethod
    def _completed_goal_after_restart(original, row, goal):
        before=original.get('_goal_record')
        after=goal
        goal_status=after.get('status') if isinstance(after,dict) else None
        if (original.get('goal_status')!='active' or not isinstance(before,dict)
                or not isinstance(after,dict) or goal_status not in ('complete','completed')
                or row.get('goal_status')!=goal_status or row.get('status')!='idle'
                or not row.get('turn_id') or row.get('turn_id')==original.get('turn_id')
                or not row.get('_owner') or row.get('waiting') or row.get('is_child')
                or row.get('_ephemeral') or row.get('_goal_mismatch') or row.get('_goal_deferred')
                or row.get('_settings_fingerprint')!=original.get('_settings_fingerprint')):
            return False
        for field in ('goal_id','token_budget','created_at_ms','deferred'):
            if before.get(field)!=after.get(field):return False
        for field in ('tokens_used','time_used_seconds'):
            old,new=before.get(field),after.get(field)
            if (not isinstance(old,(int,float)) or isinstance(old,bool)
                    or not isinstance(new,(int,float)) or isinstance(new,bool) or new<old):
                return False
        old_updated,new_updated=before.get('updated_at_ms'),after.get('updated_at_ms')
        if (isinstance(old_updated,(int,float)) and isinstance(new_updated,(int,float))
                and new_updated<old_updated):
            return False
        return True

    def discover(self):
        with self._lock:
            try:
                return self._scan()
            except DesktopAdapterError:
                if isinstance(self._bridge, _BridgeClient):
                    self._bridge.close();self._bridge=None
                raise

    def selectable_sessions(self, live_rows):
        # Saved chats are selectable even without a live IPC owner. Keep this
        # display catalog separate from discover(): it never authorizes a turn,
        # account switch, or an app restart using stale saved metadata.
        live = {r['id']:r for r in live_rows if not r.get('is_child')}
        catalog = self._catalog()
        rows = []
        for sid, saved in catalog.items():
            source = saved.get('source') or ''
            if saved.get('archived') or saved.get('is_archived') or saved.get('agent_path') not in (None, '', '/root'):
                continue
            try: child = isinstance(source, str) and isinstance(json.loads(source), dict) and 'subagent' in json.loads(source)
            except (ValueError, TypeError): child = False
            if child: continue
            row = live.pop(sid, None)
            rows.append({'id':sid, 'title':((row or {}).get('title') or saved.get('name') or saved.get('title') or 'Codex conversation')[:500],
                'status':row.get('status') if row else 'Open in Codex',
                'goal_status':row.get('goal_status') if row else self._goal_rows.get(sid, {}).get('status'),
                'attached':bool(row), 'updated_at':saved.get('updated_at_ms') or (saved.get('updated_at') or 0)*1000})
        for sid, row in live.items():
            rows.append({k:row.get(k) for k in ('id', 'title', 'status', 'goal_status')} | {'attached':True,'updated_at':0})
        return sorted(rows, key=lambda r:(r['attached'], r['updated_at']), reverse=True)

    def get(self, session_id):
        if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
            return None
        return next((session for session in self.discover() if session["id"] == session_id), None)

    @staticmethod
    def _safe_journal(callback, phase, metadata):
        # Callers only pass IDs, stable identity keys, operation IDs, and fingerprints.
        callback(phase, metadata)

    def _validate_selected(self, sessions, ids):
        if len(sessions) != len(ids):
            raise DesktopNoOwnerError("A selected Codex conversation has no live owner")
        current_key = self.accounts.current_desktop_key()
        if not current_key:
            raise DesktopAdapterError("The current Codex Desktop identity could not be verified")
        for session in sessions:
            if session["account_key"] != current_key:
                raise DesktopAdapterError("The selected conversation identity does not match saved Desktop credentials")
            if not session.get("supported") or session.get("_goal_mismatch"):
                raise DesktopProtocolError("The selected conversation state is incompatible")
            if not session.get('_settings_fingerprint'):
                raise DesktopProtocolError('The selected conversation settings cannot be verified')
            if session.get("waiting"):
                raise DesktopAdapterError("A selected Codex conversation is waiting for user input or approval")
            if session.get("is_child") or session.get("_ephemeral"):
                raise DesktopAdapterError("A selected Codex conversation is not a recoverable top-level thread")
            if session.get("goal_status") not in _ACTIVE_GOALS or session.get("_goal_deferred"):
                raise DesktopAdapterError("A selected Goal is paused, complete, limited, or deferred")
            if session.get("status") == "running":
                if not session.get("turn_id") or not session.get("_owner") or not session.get("_settings_fingerprint"):
                    raise DesktopProtocolError("A selected running conversation is missing recovery state")
            elif session.get('status') != 'interrupted' and ((session.get("status") == "failed" and recovery_error(session.get("error_kind"))) or session.get('goal_status') == 'usage_limited'):
                if not session.get("turn_id") or not session.get("_owner"):
                    raise DesktopProtocolError("A selected failed conversation is missing recovery state")
            elif session.get('status')=='interrupted' and self.recovery_receipts.get(session['id']) == (session.get('turn_id'),session.get('_settings_fingerprint')):
                pass
            else:
                raise DesktopAdapterError("A selected conversation was paused or stopped by the user")

    def _assert_safe_group(self, sessions, selected_ids):
        if not self._inventory_complete:
            raise DesktopAdapterError("Codex Desktop session inventory is incomplete; refusing app-wide restart")
        selected = set(selected_ids)
        for session in sessions:
            if session.get("waiting"):
                raise DesktopAdapterError("A Codex conversation is waiting for approval; refusing app-wide restart")
            if session.get("status") == "running" and session["id"] not in selected:
                raise DesktopAdapterError("An unselected Codex conversation is active; refusing app-wide restart")
            if session.get("goal_status") == "active" and session["id"] not in selected:
                raise DesktopAdapterError("An unselected active Goal could resume on reopen; refusing app-wide restart")

    @staticmethod
    def _same_account_recovery_session(session):
        if (session.get('waiting') or session.get('is_child') or session.get('_ephemeral')
                or session.get('_goal_deferred') or session.get('_goal_mismatch') or proxy_session(session)):
            return False
        kind=''.join(c for c in str(session.get('error_kind') or '').lower() if c.isalnum())
        quota_kinds={'quota','usagelimitexceeded','ratelimitexceeded'}
        login_kinds={'login','unauthorized','authenticationerror'}
        if session.get('goal_status')=='usage_limited':
            goal=session.get('_goal_record')
            return bool(session.get('status') in ('failed','idle') and isinstance(goal,dict)
                and goal.get('status')=='usage_limited' and goal.get('goal_id')
                and all(isinstance(goal.get(k),(int,float)) and not isinstance(goal.get(k),bool)
                    for k in ('token_budget','tokens_used','time_used_seconds')))
        return bool(session.get('status')=='failed' and kind in quota_kinds|login_kinds
            and session.get('goal_status') in (None,'active'))

    def _restore_usage_limited_goal(self, session_id, original_goal):
        """Restore only the exact usage-limited Goal snapshot read before shutdown."""
        if (not isinstance(original_goal,dict) or original_goal.get('status')!='usage_limited'
                or not original_goal.get('goal_id') or original_goal.get('deferred')):
            raise DesktopAdapterError('The usage-limited Goal is no longer eligible for restart')
        path=self.codex_home/'goals_1.sqlite'
        try:
            connection=sqlite3.connect(path)
            try:
                with connection:
                    tables={row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'thread_goals' not in tables:
                        raise DesktopAdapterError('Codex Desktop Goal database schema is incompatible')
                    columns={row[1] for row in connection.execute('PRAGMA table_info(thread_goals)')}
                    required={'thread_id','goal_id','status','token_budget','tokens_used','time_used_seconds'}
                    if not required<=columns:
                        raise DesktopAdapterError('Codex Desktop Goal database schema is incompatible')
                    fields=('goal_id','status','token_budget','tokens_used','time_used_seconds',
                        'created_at_ms','updated_at_ms')
                    compared=[field for field in fields if field in columns and field in original_goal]
                    if not required-{'thread_id'}<=set(compared):
                        raise DesktopAdapterError('The Goal accounting snapshot is incomplete')
                    where=['"thread_id"=?']
                    params=[session_id]
                    for field in compared:
                        where.append('"'+field+'" IS ?')
                        params.append(original_goal.get(field))
                    if 'thread_goal_continuation_deferrals' in tables:
                        deferral_columns={row[1] for row in connection.execute(
                            'PRAGMA table_info(thread_goal_continuation_deferrals)')}
                        if 'thread_id' in deferral_columns:
                            where.append('NOT EXISTS (SELECT 1 FROM thread_goal_continuation_deferrals WHERE thread_id=?)')
                            params.append(session_id)
                    count=connection.execute('UPDATE thread_goals SET status=? WHERE '+
                        ' AND '.join(where),['active',*params]).rowcount
                    if count!=1:
                        raise DesktopAdapterError('The usage-limited Goal changed before offline restoration')
                    selected=[field for field in fields if field in columns]
                    row=connection.execute('SELECT '+', '.join('"'+field+'"' for field in selected)+
                        ' FROM thread_goals WHERE thread_id=?',(session_id,)).fetchone()
                    if not row:
                        raise DesktopAdapterError('The restored Goal could not be verified')
                    restored=dict(zip(selected,row));restored['deferred']=False
                    return restored
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise DesktopAdapterError('Codex Desktop Goal state could not be restored safely') from exc

    @staticmethod
    def _continuation_request(session, operation_id):
        # The owner protocol inherits the original thread settings. Passing this request
        # changes only the single synthetic continuation message and its idempotency key.
        return {
            "threadId": session["id"],
            "input": [{
                "type": "text",
                "text": "Continue the interrupted work in this same conversation. Preserve its existing Goal and task context.",
                "text_elements": [],
            }],
            "clientUserMessageId": operation_id,
        }

    def resume_in_place(self, session_ids, journal_callback, recovery_operation=None):
        """Recover an older quota failure after a verified account handoff."""
        ids = list(session_ids)
        if not ids or len(set(ids)) != len(ids) or any(not _UUID.fullmatch(str(x)) for x in ids):
            raise DesktopAdapterError('The selected Codex conversation list is invalid')
        with self._lock:
            before = self._scan(self._catalog())
            selected = [s for s in before if s['id'] in ids]
            account = self.accounts.current_desktop_key()
            if recovery_operation is not None:
                if not set(ids) <= set(recovery_operation.get('sessions',[])):
                    raise DesktopAdapterError('The delayed recovery conversation list changed')
                subset=dict(recovery_operation,sessions=ids)
                if pending_desktop_recovery(subset,selected,account) != ids:
                    raise DesktopAdapterError('The original stopped turns changed before delayed recovery')
                for s in selected:
                    if s['status']=='interrupted':self.recovery_receipts[s['id']]=(s['turn_id'],s['_settings_fingerprint'])
            self._validate_selected(selected, ids)
            for s in selected:
                kind = ''.join(c for c in str(s.get('error_kind') or '').lower() if c.isalnum())
                if recovery_operation is None and (s['status'] != 'failed' or kind not in ('quota','usagelimitexceeded','ratelimitexceeded') or s.get('goal_status') not in (None,'active')):
                    raise DesktopAdapterError('Only a known quota failure can continue without restarting Codex')
            if recovery_operation is None:self._safe_journal(journal_callback, 'desktop-recovery-context', {'context': {
                s['id']: {'turn':s.get('turn_id'),'settings':s.get('_settings_fingerprint'),'settings_snapshot':s.get('_settings'),
                    'goal_record':s.get('_goal_record')} for s in selected}})
            recovered = []
            for s in selected:
                if self.accounts.current_desktop_key() != account:
                    raise DesktopAdapterError('The active Codex identity changed before continuation')
                attempt = (s['id'],s['turn_id'],account)
                if attempt in self._attempted_sends:
                    raise DesktopUncertainError('This continuation was already attempted; do not retry')
                operation = str(uuid.uuid4())
                self._attempted_sends.add(attempt)
                self._safe_journal(journal_callback, 'desktop-continuation-intent', {
                    'session':s['id'],'turn':s['turn_id'],'operation':operation,
                    'target':account,'settings':s['_settings_fingerprint'],'goal':s.get('goal_status')})
                result = self._client().call({'op':'resume','thread':s['id'],'operation_id':operation,
                    'expected_owner':s['_owner'],'expected_turn':s['turn_id'],
                    'settings_fingerprint':s['_settings_fingerprint'],
                    'request':self._continuation_request(s,operation)}, timeout=40)
                row = result.get('session') if isinstance(result,dict) else None
                if (not isinstance(row,dict) or row.get('status') not in ('running','idle') or
                    not row.get('turn_id') or row['turn_id']==s['turn_id'] or
                    row.get('settings_fingerprint')!=s['_settings_fingerprint'] or
                    row.get('client_user_message_id')!=operation):
                    raise DesktopUncertainError('Codex continuation was not confirmed; do not retry')
                self._safe_journal(journal_callback,'desktop-session-recovered',{
                    'session':s['id'],'target':account,'turn':row['turn_id']})
                recovered.append(s['id'])
            return recovered

    def rotate(self, session_ids, target, journal_callback, *, same_account_recovery=False,
            same_account_recovery_ids=None):
        ids = list(session_ids)
        if not ids or len(set(ids)) != len(ids) or any(not isinstance(x, str) or not _UUID.fullmatch(x) for x in ids):
            raise DesktopAdapterError("The selected Codex conversation list is invalid")
        if not isinstance(target, dict) or not isinstance(target.get("key"), str):
            raise DesktopAdapterError("The destination Codex identity is invalid")
        with self._lock:
            catalog = self._catalog()
            before = self._scan(catalog)
            by_id = {item["id"]: item for item in before}
            selected = [by_id[x] for x in ids if x in by_id]
            self._validate_selected(selected, ids)
            self._assert_safe_group(before, ids)
            current_key = self.accounts.current_desktop_key()
            same_account=target['key']==current_key
            recovery_ids=set(same_account_recovery_ids or (ids if same_account_recovery else ()))
            if same_account:
                if (not same_account_recovery or not recovery_ids or not recovery_ids<=set(ids)):
                    raise DesktopAdapterError('Same-account restart was not explicitly authorized')
                selected_by_id={session['id']:session for session in selected}
                if not all(self._same_account_recovery_session(selected_by_id[sid]) for sid in recovery_ids):
                    raise DesktopAdapterError('The selected same-account recovery is not an eligible quota, login, or usage-limited Goal failure')
                for session in selected:
                    if session['id'] in recovery_ids:continue
                    if (session.get('status')!='running' or session.get('waiting') or session.get('is_child')
                            or session.get('_ephemeral') or session.get('_goal_deferred')
                            or session.get('_goal_mismatch') or session.get('goal_status') not in (None,'active')
                            or proxy_session(session)):
                        raise DesktopAdapterError('An extra same-account restart chat is not safe to preserve')
            known = self.accounts.find(target["key"])
            if not known or known.get("home") != target.get("home"):
                raise DesktopAdapterError("The destination Codex account is not a verified saved identity")
            # An interrupted existing user turn is eligible only when its failure is known
            # to be quota/login related. User-paused sessions were rejected above.
            originals = {session["id"]: dict(session) for session in selected}
            self._safe_journal(journal_callback,'desktop-recovery-context',{'context':{
                sid:{'turn':o.get('turn_id'),'settings':o.get('_settings_fingerprint'),
                     'settings_snapshot':o.get('_settings'),'goal_record':o.get('_goal_record')}
                for sid,o in originals.items()}})
            operations = {sid: str(uuid.uuid4()) for sid in ids}
            for session in selected:
                if session["status"] != "running":
                    continue
                sid = session["id"]
                self._safe_journal(journal_callback, "desktop-interrupt-intent", {
                    "session": sid, "turn": session["turn_id"], "operation": operations[sid],
                    "account": current_key, "settings": session["_settings_fingerprint"],
                    "goal_record":session.get('_goal_record'),
                })
                result = self._client().call({
                    "op": "interrupt", "thread": sid, "operation_id": operations[sid],
                    "expected_owner": session["_owner"], "expected_turn": session["turn_id"],
                    "settings_fingerprint": session["_settings_fingerprint"],
                }, timeout=35)
                if not isinstance(result, dict) or not isinstance(result.get("session"), dict):
                    raise DesktopUncertainError("Codex Desktop did not confirm the selected turn pause")
                if result["session"].get("settings_fingerprint") != session["_settings_fingerprint"]:
                    raise DesktopAdapterError("Codex Desktop conversation settings changed while pausing")
                if result['session'].get('turn_id') != session['turn_id'] or result['session'].get('status') != 'interrupted':
                    raise DesktopUncertainError('Codex Desktop did not confirm the exact interrupted turn')
                self.recovery_receipts[sid] = (session['turn_id'], session['_settings_fingerprint'])
                self._safe_journal(journal_callback, 'desktop-interrupt-confirmed', {
                    'session': sid, 'turn': session['turn_id'], 'operation': operations[sid],
                    'account': current_key, 'settings': session['_settings_fingerprint'],
                    'goal':session.get('goal_status'),'goal_record':session.get('_goal_record'),
                    'confirmation':result.get('confirmation','native-acknowledgement'),
                })
            after_pause = self._scan(self._catalog(), wait_ms=1000)
            self._assert_safe_group(after_pause, ids)
            paused_by_id = {row["id"]: row for row in after_pause}
            for sid in ids:
                row = paused_by_id.get(sid)
                original = originals[sid]
                if not row or row.get("waiting") or row.get("_goal_mismatch"):
                    raise DesktopAdapterError("The selected conversation changed before app restart")
                stopped_goal = self._goal_rows.get(sid)
                if row.get("goal_status") != original.get("goal_status") or not self._goal_progress_preserved(original.get("_goal_record"), stopped_goal):
                    raise DesktopAdapterError("The selected Goal identity or budget changed before app restart")
                original["_goal_record"] = dict(stopped_goal) if stopped_goal else None
                original["_goal_signature"] = self._goal_signature(stopped_goal)
                if row.get("_settings_fingerprint") != original.get("_settings_fingerprint"):
                    raise DesktopAdapterError("The selected conversation settings changed before app restart")
                if original["status"] == "running" and row.get("status") != "interrupted":
                    raise DesktopUncertainError("Codex Desktop pause could not be confirmed; do not retry")
            # Quit normally and proceed only after every package process exits.
            if self._bridge:
                self._bridge.close()
                self._bridge = None
            self._safe_journal(journal_callback, "desktop-close-intent", {
                "sessions": ids, "account": current_key, "target": target["key"],
                "originals": {sid: {'turn': o.get('turn_id'), 'settings': o.get('_settings_fingerprint'),
                    'goal': o.get('goal_status'), 'relay_interrupted': o.get('status') == 'running' or sid in self.recovery_receipts}
                    for sid, o in originals.items()},
                "context": {sid: {'turn': o.get('turn_id'), 'settings': o.get('_settings_fingerprint'),
                    'settings_snapshot': o.get('_settings'), 'goal_record': o.get('_goal_record')}
                    for sid, o in originals.items()},
            })
            self._app_control.close_app()
            if self.accounts.current_desktop_key()!=current_key:
                raise DesktopAdapterError('The active Codex identity changed during the normal app close')
            if same_account:
                if self.accounts.current_desktop_key()!=target['key']:
                    raise DesktopAdapterError('The same-account recovery identity changed before restart')
                self._safe_journal(journal_callback,'desktop-same-account-verified',{'target':target['key']})
            else:
                self._safe_journal(journal_callback, "desktop-activate-intent", {"target": target["key"]})
                activated = self.accounts.activate_desktop(known)
                actual_key = self.accounts.current_desktop_key()
                if activated != target["key"] or actual_key != target["key"]:
                    raise DesktopAdapterError("The saved Codex identity did not verify after activation")
            # The app is closed: resume only Goals explicitly stopped by usage limits.
            for sid, original in originals.items():
                if original.get('goal_status') != 'usage_limited': continue
                self._safe_journal(journal_callback,'desktop-resume-usage-goal',{'session':sid,'goal_id':original['_goal_record']['goal_id']})
                original['_goal_record']=dict(original['_goal_record'])
                original['_goal_record']=self._restore_usage_limited_goal(sid,original['_goal_record'])
                original['goal_status']='active'
                original['_goal_signature']=self._goal_signature(original['_goal_record'])
            self._safe_journal(journal_callback, "desktop-reopen-intent", {"target": target["key"]})
            self._app_control.launch_app()
            # The installed AppX package registers this URI protocol. Open only the
            # original selected thread IDs; no unrelated conversation is resumed.
            for sid in ids:
                deadline=time.monotonic()+self._recovery_timeout
                next_navigation=0
                while True:
                    if time.monotonic()>=next_navigation:
                        self._safe_journal(journal_callback, 'desktop-open-thread-intent', {'session':sid,'target':target['key']})
                        self._app_control.open_thread(sid)
                        next_navigation=time.monotonic()+8
                    try:
                        catalog=self._catalog()
                        ready_rows=self._scan({sid:catalog[sid]},wait_ms=1000)
                        if any(r['id']==sid and r.get('_owner') and r.get('_settings_fingerprint')==originals[sid].get('_settings_fingerprint') for r in ready_rows):break
                    except DesktopProtocolError:raise
                    except DesktopAdapterError:
                        if self._bridge:
                            self._bridge.close();self._bridge=None
                    if time.monotonic()>=deadline:
                        raise DesktopAdapterError('Codex Desktop did not restore the original conversation and settings in time')
                    time.sleep(.5)
            # Follow exact originals and compare account, owner, Goal record, and settings.
            # Launch readiness is later than the first package process. The app also
            # restores per-thread visualization roots after a Goal starts. Wait for
            # original settings before confirming recovery or sending a continuation.
            deadline = time.monotonic() + self._recovery_timeout
            while True:
                try:
                    self._client().start()
                    fresh = self._scan(self._catalog(), wait_ms=1000)
                except DesktopProtocolError:
                    raise
                except DesktopAdapterError:
                    if self._bridge:
                        self._bridge.close(); self._bridge = None
                    if time.monotonic() >= deadline: raise
                    time.sleep(.5)
                    continue
                ready = {r['id']:r for r in fresh}
                if all(sid in ready and ready[sid].get('_owner') and
                    ready[sid].get('_settings_fingerprint') == originals[sid].get('_settings_fingerprint') for sid in ids): break
                if time.monotonic() >= deadline:
                    raise DesktopAdapterError('Codex Desktop did not restore the selected owners and settings in time')
                time.sleep(.5)
            self._assert_safe_group(fresh, ids)
            fresh_by_id = {row["id"]: row for row in fresh}
            completed_goals=set()
            for sid in ids:
                row = fresh_by_id.get(sid)
                original = originals[sid]
                if not row or not row.get("_owner"):
                    raise DesktopNoOwnerError("Codex Desktop has no owner for a selected original conversation")
                if self.accounts.current_desktop_key() != target["key"]:
                    raise DesktopAdapterError("The active saved Codex identity changed during recovery")
                goal = self._goal_rows.get(sid)
                completed_goal=self._completed_goal_after_restart(original,row,goal)
                if ((goal.get("status") if goal else None) != original.get("goal_status") or
                        not self._goal_progress_preserved(original.get("_goal_record"), goal)) and not completed_goal:
                    raise DesktopAdapterError("The original Goal identity or budget changed during recovery")
                if row.get("_settings_fingerprint") != original.get("_settings_fingerprint"):
                    raise DesktopAdapterError("The original conversation settings changed during recovery")
                if completed_goal:
                    completed_goals.add(sid)
                    continue
                if original.get("goal_status") == "active" and row.get("goal_status") != "active":
                    raise DesktopAdapterError("Codex Desktop has not restored the existing active Goal state")
                if row.get("waiting") or row.get("is_child") or row.get("_ephemeral") or row.get("goal_status") not in _ACTIVE_GOALS:
                    raise DesktopAdapterError("The original conversation is no longer safe to continue")
                if row.get("status") == "running" and row.get("_runtime_status") == "active":
                    continue
                if row.get("status") not in {"interrupted", "failed"} and not (row.get('status')=='idle' and original.get('goal_status')=='active'):
                    raise DesktopAdapterError("The original selected turn changed during recovery")
            recovered = []
            for sid in ids:
                original = originals[sid]
                latest = fresh_by_id[sid]
                if sid in completed_goals:
                    self._safe_journal(journal_callback, "desktop-session-recovered", {
                        "session": sid, "target": target["key"], "turn": latest.get("turn_id"),
                        "resumed_by_desktop": True, "completed_by_desktop": True,
                    })
                    recovered.append(sid)
                    continue
                if latest.get("status") == "running" and latest.get("_runtime_status") == "active":
                    self._safe_journal(journal_callback, "desktop-session-recovered", {
                        "session": sid, "target": target["key"], "turn": latest.get("turn_id"),
                        "resumed_by_desktop": True,
                    })
                    recovered.append(sid)
                    continue
                if latest.get("status") == "failed" and not recovery_error(latest.get("error_kind")) and original.get('goal_status')!='active':
                    raise DesktopAdapterError("The original turn stopped for a reason that cannot be resumed automatically")
                if latest.get("status") not in {"interrupted", "failed"} and not (latest.get('status')=='idle' and original.get('goal_status')=='active'):
                    raise DesktopAdapterError("The original selected turn is no longer eligible for continuation")
                op = operations[sid]
                attempt = (sid, original.get("turn_id") or "", target["key"])
                if attempt in self._attempted_sends:
                    raise DesktopUncertainError("This continuation was already attempted; inspect the conversation before retrying")
                self._attempted_sends.add(attempt)
                self._safe_journal(journal_callback, "desktop-continuation-intent", {
                    "session": sid, "turn": original.get("turn_id"), "operation": op,
                    "target": target["key"], "settings": original.get("_settings_fingerprint"),
                    "goal": original.get("goal_status"),
                })
                try:
                    result = self._bridge.call({
                        "op": "resume", "thread": sid, "operation_id": op,
                        "expected_owner": latest.get("_owner"),
                        "expected_turn": original.get("turn_id"),
                        "settings_fingerprint": original.get("_settings_fingerprint"),
                        "request": self._continuation_request(original, op),
                    }, timeout=40)
                except DesktopUncertainError:
                    raise
                except DesktopAdapterError as exc:
                    # Only a proven pre-send state rejection is safe to classify as not sent.
                    raise DesktopUncertainError("Codex Desktop continuation could not be confirmed; do not retry") from exc
                if not isinstance(result, dict) or not isinstance(result.get("session"), dict):
                    raise DesktopUncertainError("Codex Desktop continuation outcome is uncertain; do not retry")
                if result["session"].get("settings_fingerprint") != original.get("_settings_fingerprint"):
                    raise DesktopAdapterError("Codex Desktop changed original conversation settings")
                self._safe_journal(journal_callback, "desktop-session-recovered", {
                    "session": sid, "target": target["key"], "turn": result["session"].get("turn_id"),
                })
                recovered.append(sid)
            return recovered

    def close(self):
        with self._lock:
            if self._bridge:
                self._bridge.close()
                self._bridge = None
