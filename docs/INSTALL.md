# Windows installation

The standalone package supports Windows x64. It includes its Python and Node.js
runtimes, so a separate Python or Node.js installation is not needed.

1. Download `SessionRelay-Windows-x64.zip` and extract the complete archive to a
   folder you can write to.
2. Double-click `Install.cmd`. If Windows asks whether to run PowerShell, allow
   it to run. The installer works for the current Windows user and does not
   require administrator rights.
3. Open **Session Relay** from the desktop or Start menu shortcut.

Install and sign in to the app or apps you plan to enable in Session Relay:

- Orca for Orca sessions.
- Codex Desktop and the Codex CLI for Codex sessions.

Only the app you enable needs to be available. Session Relay uses the existing
saved sign-ins in those apps; it does not create a new sign-in.

The installer places the app in `%LOCALAPPDATA%\SessionRelay\app`, registers
the `SessionRelayMonitor` and `SessionRelayPanel` scheduled tasks for the
interactive user, and starts the monitor and panel. It preserves the user's
configuration and account state in `%LOCALAPPDATA%\SessionRelay`. When replacing
an earlier app folder, it keeps that folder as a timestamped `app.backup.*`
directory.

To install or upgrade from PowerShell instead, run
`.\SessionRelay.exe --install` from the extracted package directory. The
installer requests a graceful stop from Session Relay and waits for it to exit.
It refuses to replace the app while a handoff or included reset is active.

Windows builds are currently unsigned. Windows may show a SmartScreen or
publisher warning because the release has no code-signing certificate.
`SHA256SUMS.txt` is published beside the ZIP as a separate release asset. Save
both files, then check the downloaded ZIP in PowerShell and compare its hash to
the matching line in `SHA256SUMS.txt`:

```powershell
Get-FileHash .\SessionRelay-Windows-x64.zip -Algorithm SHA256
```

## Uninstall

1. Close the Session Relay panel with its **X** button.
2. In PowerShell, create a recent upgrade marker so the supervisor does not
   restart the monitor, then request a graceful monitor stop:

   ```powershell
   $relayData = Join-Path $env:LOCALAPPDATA 'SessionRelay'
   Set-Content (Join-Path $relayData 'upgrade-in-progress') $PID
   Set-Content (Join-Path $relayData 'stop-request.json') '{"reason":"uninstall"}'
   ```

   Wait for the monitor to exit before continuing.
3. In Task Scheduler Library, end and disable only the `SessionRelayMonitor`
   and `SessionRelayPanel` tasks.
4. Remove the `SessionRelay` value from
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, then remove the
   Session Relay desktop and Start menu shortcuts.
5. Remove `%LOCALAPPDATA%\SessionRelay\app` and any timestamped
   `%LOCALAPPDATA%\SessionRelay\app.backup.*` folders if you no longer need
   the installed program files.

Leave `%LOCALAPPDATA%\SessionRelay` itself in place to retain Session Relay
settings, account inventory, and history. Delete that data folder only if you
explicitly want to remove those personal files. Session Relay does not remove
Orca or Codex sign-ins, chat data, or app installations.

This is a Windows x64 distribution. No support for other operating systems is
claimed.
