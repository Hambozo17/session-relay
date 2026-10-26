import pathlib
import tempfile
import unittest
import base64
import os
import json
import shutil
import subprocess
import time
from unittest.mock import patch

import relay_runtime
import session_relay
import watchdog


class RuntimeCommands(unittest.TestCase):
    def test_source_commands_use_absolute_module_paths_and_pythonw(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            python = root / "python.exe"
            pythonw = root / "pythonw.exe"
            pythonw.touch()
            with patch.object(relay_runtime, "_SOURCE_ROOT", root), \
                    patch.object(relay_runtime.sys, "executable", str(python)), \
                    patch("relay_runtime.os.name", "nt"):
                command = relay_runtime.command("monitor")
            self.assertEqual(command, [str(pythonw), str((root / "controller.py").resolve())])

    def test_frozen_commands_dispatch_by_role(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = str(pathlib.Path(folder) / "SessionRelay.exe")
            with patch.object(relay_runtime.sys, "executable", executable), \
                    patch.object(relay_runtime.sys, "frozen", True, create=True):
                self.assertEqual(relay_runtime.command("panel"), [executable, "--panel"])
                self.assertEqual(relay_runtime.command("open"), [executable, "--open"])

    def test_frozen_installer_path_is_next_to_the_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            package = pathlib.Path(folder)
            with patch.object(relay_runtime.sys, "executable", str(package / "SessionRelay.exe")), \
                    patch.object(relay_runtime.sys, "frozen", True, create=True):
                self.assertEqual(relay_runtime.installer_script(), package / "install-bundle.ps1")

    def test_install_flag_passes_the_adjacent_script_to_powershell(self):
        with tempfile.TemporaryDirectory() as folder:
            package = pathlib.Path(folder)
            installer = package / "install-bundle.ps1"
            installer.touch()
            result = type("Result", (), {"returncode": 0})()
            with patch.object(session_relay, "installer_script", return_value=installer), \
                    patch("session_relay.shutil.which", return_value="C:/Windows/powershell.exe"), \
                    patch("session_relay.subprocess.run", return_value=result) as run:
                self.assertEqual(session_relay._run_installer(), 0)
            self.assertEqual(pathlib.Path(run.call_args.args[0][-1]), installer)

    def test_process_specs_match_exact_executable_and_role(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = str(pathlib.Path(folder) / "SessionRelay.exe")
            with patch.object(relay_runtime.sys, "executable", executable), \
                    patch.object(relay_runtime.sys, "frozen", True, create=True):
                spec = relay_runtime.process_query("monitor")
            self.assertEqual(spec[0]["executable"], str(pathlib.Path(executable).resolve()))
            self.assertIn('"' + executable + '" --monitor', spec[0]["command_lines"])
            self.assertNotIn('"' + executable + '" --panel', spec[0]["command_lines"])

    def test_bundled_node_precedes_path_in_frozen_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            internal = root / "_internal"
            internal.mkdir()
            node = internal / "node.exe"
            node.touch()
            with patch.object(relay_runtime.sys, "executable", str(root / "SessionRelay.exe")), \
                    patch.object(relay_runtime.sys, "_MEIPASS", str(internal), create=True), \
                    patch.object(relay_runtime.sys, "frozen", True, create=True), \
                    patch("relay_runtime.shutil.which", return_value="C:/Node/node.exe"):
                self.assertEqual(relay_runtime.node_binary(), str(node.resolve()))

    def test_unknown_roles_are_rejected(self):
        with self.assertRaises(ValueError):
            relay_runtime.command("arbitrary")

    def test_old_upgrade_marker_is_active_only_while_owner_process_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = pathlib.Path(folder) / "upgrade-in-progress"
            marker.write_text("1234")
            stale = time.time() - 600
            os.utime(marker, (stale, stale))
            with patch("watchdog._pid_is_running", return_value=True):
                self.assertTrue(watchdog._upgrade_is_active(marker))
            with patch("watchdog._pid_is_running", return_value=False):
                self.assertFalse(watchdog._upgrade_is_active(marker))

    def test_stale_supervisor_stop_request_recovers_after_its_owner_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = pathlib.Path(folder) / "supervisor-stop-request.json"
            marker.write_text('{"reason":"upgrade","pid":2147483647}')
            stale = time.time() - 600
            os.utime(marker, (stale, stale))
            with patch("watchdog._pid_is_running", return_value=False):
                self.assertFalse(watchdog._supervisor_stop_requested(marker))
            marker.write_text('{"reason":"upgrade","pid":1234}')
            os.utime(marker, (stale, stale))
            with patch("watchdog._pid_is_running", return_value=True):
                self.assertTrue(watchdog._supervisor_stop_requested(marker))

    def test_windows_pid_probe_uses_waitable_handle_and_always_closes_it(self):
        from unittest.mock import Mock, patch

        kernel = type("Kernel", (), {})()
        kernel.OpenProcess = Mock(return_value=123)
        kernel.WaitForSingleObject = Mock(return_value=0x00000102)
        kernel.CloseHandle = Mock(return_value=1)
        with patch.object(watchdog.ctypes, "WinDLL", return_value=kernel, create=True):
            self.assertTrue(watchdog._windows_pid_is_running(456))
        kernel.OpenProcess.assert_called_once_with(0x00100000, False, 456)
        kernel.WaitForSingleObject.assert_called_once_with(123, 0)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_pid_probe_treats_missing_and_access_denied_conservatively(self):
        from unittest.mock import Mock, patch

        for error, expected in ((87, False), (1168, False), (5, True), (999, True)):
            kernel = type("Kernel", (), {})()
            kernel.OpenProcess = Mock(return_value=None)
            kernel.WaitForSingleObject = Mock(return_value=0xFFFFFFFF)
            kernel.CloseHandle = Mock(return_value=1)
            with self.subTest(error=error):
                with patch.object(watchdog.ctypes, "WinDLL", return_value=kernel, create=True), \
                        patch.object(watchdog.ctypes, "get_last_error", return_value=error, create=True):
                    self.assertEqual(watchdog._windows_pid_is_running(456), expected)

    def test_installer_verifies_then_stops_only_the_scheduler_supervisor(self):
        source = (pathlib.Path(__file__).with_name("install-bundle.ps1")).read_text(encoding="utf-8")
        self.assertIn("if (-not $script:monitorStopped)", source)
        self.assertIn("Get-VerifiedMonitorTask $folder", source)
        self.assertIn("$verified.Task.Stop(0)", source)
        self.assertIn("Get-RelayRoleProcesses 'supervisor'", source)
        self.assertNotIn("Stop-Process", source)

    def test_stale_marker_decisions_are_isolated_from_installation_and_tasks(self):
        powershell=shutil.which('powershell.exe')
        if not powershell:
            self.skipTest('Windows PowerShell is required for the installer-helper AST check')
        source_path=pathlib.Path(__file__).with_name('install-bundle.ps1')
        source_literal=str(source_path).replace("'","''")
        script=f'''\
$sourcePath='{source_literal}'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($sourcePath,[ref]$tokens,[ref]$errors)
if($errors){{throw ($errors -join "`n")}}
$wanted=@('Test-UpgradeMarkerSnapshotEqual','Get-UpgradeMarkerDisposition')
$functions=$ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name}},$true)
foreach($function in $functions){{. ([ScriptBlock]::Create($function.Extent.Text))}}
if($functions.Count -ne 2){{throw 'The pure marker helpers are missing'}}
$original=[pscustomobject]@{{Bytes='YQ==';Length=1;CreationTimeUtcTicks=100;LastWriteTimeUtcTicks=200;Attributes=32}}
$same=[pscustomobject]@{{Bytes='YQ==';Length=1;CreationTimeUtcTicks=100;LastWriteTimeUtcTicks=200;Attributes=32}}
$newBytes=[pscustomobject]@{{Bytes='Yg==';Length=1;CreationTimeUtcTicks=100;LastWriteTimeUtcTicks=200;Attributes=32}}
$newTime=[pscustomobject]@{{Bytes='YQ==';Length=1;CreationTimeUtcTicks=100;LastWriteTimeUtcTicks=201;Attributes=32}}
$cases=[ordered]@{{
  deadStale=(Get-UpgradeMarkerDisposition 301 'dead' $false $true $true)
  liveOwner=(Get-UpgradeMarkerDisposition 301 'active' $false $true $true)
  young=(Get-UpgradeMarkerDisposition 299 'dead' $false $true $true)
  unknownOwner=(Get-UpgradeMarkerDisposition 301 'unknown' $false $true $true)
  otherInstaller=(Get-UpgradeMarkerDisposition 301 'dead' $true $true $true)
  liveOperation=(Get-UpgradeMarkerDisposition 301 'dead' $false $false $true)
  replaced=(Get-UpgradeMarkerDisposition 301 'dead' $false $true (Test-UpgradeMarkerSnapshotEqual $original $newBytes))
  identical=(Test-UpgradeMarkerSnapshotEqual $original $same)
  bytesChanged=(Test-UpgradeMarkerSnapshotEqual $original $newBytes)
  metadataChanged=(Test-UpgradeMarkerSnapshotEqual $original $newTime)
}}
[Console]::WriteLine(($cases | ConvertTo-Json -Compress))
'''
        encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
        result=subprocess.run([powershell,'-NoProfile','-NonInteractive','-EncodedCommand',encoded],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)
        cases=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(cases['deadStale'],'stale')
        self.assertEqual(cases['liveOwner'],'owner-live')
        self.assertEqual(cases['young'],'young')
        self.assertEqual(cases['unknownOwner'],'owner-unknown')
        self.assertEqual(cases['otherInstaller'],'installer-live')
        self.assertEqual(cases['liveOperation'],'operation-active')
        self.assertEqual(cases['replaced'],'marker-changed')
        self.assertTrue(cases['identical'])
        self.assertFalse(cases['bytesChanged'])
        self.assertFalse(cases['metadataChanged'])
        source=source_path.read_text(encoding='utf-8')
        self.assertIn('Reconcile-AbandonedUpgradeMarker',source)
        self.assertIn('Test-UpgradeMarkerSnapshotEqual $snapshot $final',source)
        self.assertIn('[IO.FileMode]::CreateNew',source)

if __name__ == "__main__":
    unittest.main()
