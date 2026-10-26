$ErrorActionPreference = 'Stop'
$sourcePath = $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA 'SessionRelay'
$applicationPath = Join-Path $installRoot 'app'
$pythonPath = (Get-Command python.exe).Source
$pythonWindowPath = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowPath)) { throw 'Python for Windows is missing pythonw.exe' }
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { throw 'Node.js is required' }
New-Item -ItemType Directory -Path $applicationPath -Force | Out-Null
$upgradeMarker = Join-Path $installRoot 'upgrade-in-progress'
if (Test-Path -LiteralPath (Join-Path $installRoot 'included-reset.json')) {
    $resetOperation = Get-Content -LiteralPath (Join-Path $installRoot 'included-reset.json') -Raw | ConvertFrom-Json
    if ($resetOperation.state -eq 'in-progress') { throw 'An included reset is being applied. Upgrade after it finishes.' }
}
New-Item -ItemType File -Path $upgradeMarker -Force | Out-Null
$statusPath = Join-Path $installRoot 'status.json'
if (Test-Path -LiteralPath $statusPath) {
    $monitorStatus = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    $operationPath = Join-Path $installRoot 'journal.json'
    if (Test-Path -LiteralPath $operationPath) {
        $operation = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json
        if ($operation.state -eq 'in-progress') { throw 'A handoff is in progress. Upgrade after it finishes.' }
    }
    $expectedScript = Join-Path $applicationPath 'controller.py'
    $monitorProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' or Name = 'python.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($expectedScript) })
    foreach ($monitorProcess in $monitorProcesses) {
        $monitorPid = $monitorProcess.ProcessId
        $stopRequest = Join-Path $installRoot 'stop-request.json'
        '{"reason":"upgrade"}' | Set-Content -LiteralPath $stopRequest -Encoding UTF8
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Process -Id $monitorPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
        if (Get-Process -Id $monitorPid -ErrorAction SilentlyContinue) {
            if (Test-Path -LiteralPath (Join-Path $installRoot 'included-reset.json')) {
                $resetOperation = Get-Content -LiteralPath (Join-Path $installRoot 'included-reset.json') -Raw | ConvertFrom-Json
                if ($resetOperation.state -eq 'in-progress') { throw 'An included reset is still being applied. Upgrade after it finishes.' }
            }
            if (Test-Path -LiteralPath $operationPath) {
                $operation = Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json
                if ($operation.state -eq 'in-progress') { throw 'A handoff is still in progress. Upgrade after it finishes.' }
            }
            $confirmedMonitor = Get-CimInstance Win32_Process -Filter "ProcessId = $monitorPid"
            if ($confirmedMonitor -and $confirmedMonitor.CommandLine.Contains($expectedScript)) { Stop-Process -Id $monitorPid -Force }
            if (Test-Path -LiteralPath $stopRequest) { Remove-Item -LiteralPath $stopRequest }
        }
    }
}
Get-ChildItem -LiteralPath $sourcePath -File | Where-Object { $_.Extension -in '.py', '.cjs', '.md', '.txt', '.ps1' -or $_.Name -eq 'desktop-settings-policy.json' } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $applicationPath $_.Name) -Force
}
& $pythonPath -m pip install -r (Join-Path $applicationPath 'requirements.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
$configPath = Join-Path $installRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    '{"threshold":2,"orca":{"enabled":false,"sessions":[]},"codex":{"enabled":false,"sessions":[]}}' | Set-Content -LiteralPath $configPath -Encoding UTF8
}
$startupCommand = '"' + $pythonWindowPath + '" "' + (Join-Path $applicationPath 'watchdog.py') + '"'
$startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-ItemProperty -Path $startupKey -Name 'SessionRelay' -Value $startupCommand -PropertyType String -Force | Out-Null
$shellObject = New-Object -ComObject WScript.Shell
$shortcutLocations = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
foreach ($folder in $shortcutLocations) {
    $shortcut = $shellObject.CreateShortcut((Join-Path $folder 'Session Relay.lnk'))
    $shortcut.TargetPath = $pythonWindowPath
    $shortcut.Arguments = '"' + (Join-Path $applicationPath 'launcher.py') + '"'
    $shortcut.WorkingDirectory = $applicationPath
    $shortcut.Description = 'Automatic account handoff for selected Orca and Codex chats'
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,44"
    $shortcut.Save()
}
# An interactive-user task starts the supervisor outside Orca's process tree.
# No administrator account or stored password is required.
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$taskFolder = $scheduler.GetFolder('\')
$task = $scheduler.NewTask(0)
$task.RegistrationInfo.Description = 'Keep Session Relay monitoring independently of Orca and Codex chats'
$task.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$task.Principal.LogonType = 3
$task.Settings.Enabled = $true
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
$task.Settings.ExecutionTimeLimit = 'PT0S'
$task.Settings.MultipleInstances = 2
$task.Settings.RestartCount = 3
$task.Settings.RestartInterval = 'PT1M'
$task.Settings.StartWhenAvailable = $true
$logon = $task.Triggers.Create(9)
$logon.UserId = $task.Principal.UserId
$periodic = $task.Triggers.Create(1)
$periodic.StartBoundary = (Get-Date).AddSeconds(5).ToString('s')
$periodic.Repetition.Interval = 'PT1M'
$action = $task.Actions.Create(0)
$action.Path = $pythonWindowPath
$action.Arguments = '"' + (Join-Path $applicationPath 'watchdog.py') + '"'
$action.WorkingDirectory = $applicationPath
$registered = $taskFolder.RegisterTaskDefinition('SessionRelayMonitor', $task, 6, $task.Principal.UserId, $null, 3)
Remove-Item -LiteralPath $upgradeMarker
$registered.Run($null) | Out-Null
$panelTask = $scheduler.NewTask(0)
$panelTask.RegistrationInfo.Description = 'Open the Session Relay control window independently of chats'
$panelTask.Principal.UserId = $task.Principal.UserId
$panelTask.Principal.LogonType = 3
$panelTask.Settings.DisallowStartIfOnBatteries = $false
$panelTask.Settings.StopIfGoingOnBatteries = $false
$panelTask.Settings.ExecutionTimeLimit = 'PT0S'
$panelTask.Settings.MultipleInstances = 2
$panelAction = $panelTask.Actions.Create(0)
$panelAction.Path = $pythonWindowPath
$panelAction.Arguments = '"' + (Join-Path $applicationPath 'app.py') + '"'
$panelAction.WorkingDirectory = $applicationPath
$registeredPanel = $taskFolder.RegisterTaskDefinition('SessionRelayPanel', $panelTask, 6, $task.Principal.UserId, $null, 3)
# Refresh our own existing panel normally, without touching either chat application.
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class RelayPanelWindow { [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l); }'
$panelProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains((Join-Path $applicationPath 'app.py')) })
foreach ($panelProcess in $panelProcesses) {
    $panel = Get-Process -Id $panelProcess.ProcessId -ErrorAction SilentlyContinue
    if ($panel -and $panel.MainWindowHandle) { [RelayPanelWindow]::PostMessage($panel.MainWindowHandle, 0x10, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null }
}
Start-Sleep -Seconds 1
$registeredPanel.Run($null) | Out-Null
Write-Output ('Installed Session Relay at ' + $applicationPath)
