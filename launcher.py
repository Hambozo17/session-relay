"""Open the independent panel, or restore its existing window."""
import ctypes,pathlib,subprocess,sys
from local_rpc import HIDDEN
from relay_runtime import bundle_root, command

def open_panel():
    window=ctypes.windll.user32.FindWindowW(None,'Session Relay · Orca & Codex')
    if window:
        ctypes.windll.user32.ShowWindow(window,9)
        ctypes.windll.user32.SetForegroundWindow(window)
        return
    result=subprocess.run(['schtasks.exe','/Run','/TN','SessionRelayPanel'],capture_output=True,creationflags=HIDDEN)
    if result.returncode:
        subprocess.Popen(command('panel'),cwd=str(bundle_root()),creationflags=HIDDEN)

if __name__=='__main__':open_panel()
