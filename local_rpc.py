from __future__ import annotations
import json, os, pathlib, subprocess
from relay_runtime import node_binary, resource_root

ROOT = resource_root()
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

class RpcError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class OrcaRpc:
    def application_running(self):
        """False only when Windows confirms no Orca UI or daemon process exists."""
        if os.name != 'nt': return None
        try:
            result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                "@(Get-CimInstance Win32_Process -Filter \"Name = 'Orca.exe'\").Count"],
                capture_output=True, text=True, timeout=8, creationflags=HIDDEN)
            count = result.stdout.strip()
            return int(count) > 0 if result.returncode == 0 and count.isdigit() else None
        except (OSError, subprocess.TimeoutExpired): return None

    def call(self, method, params=None, timeout=25, envelope=None):
        request = {'method': method, 'params': params, 'timeout_ms': int(timeout * 1000)}
        if envelope:
            request['envelope'] = envelope
        try:
            result = subprocess.run([node_binary() or 'node', str(ROOT / 'orca_pipe.cjs')], input=json.dumps(request),
                capture_output=True, text=True, encoding='utf-8', timeout=timeout + 5, creationflags=HIDDEN)
            body = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise RpcError('uncertain', 'Orca request could not be confirmed.') from exc
        if not body.get('ok'):
            error = body.get('error') or {}
            raise RpcError(error.get('code', 'unknown'), error.get('message', 'Orca refused the request.'))
        return body.get('result')
