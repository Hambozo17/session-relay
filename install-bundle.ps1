$ErrorActionPreference = 'Stop'

$bundleRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set for the current Windows user.' }
$installRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'SessionRelay'))
$applicationPath = [IO.Path]::GetFullPath((Join-Path $installRoot 'app'))
$upgradeMarker = Join-Path $installRoot 'upgrade-in-progress'
$stopRequest = Join-Path $installRoot 'stop-request.json'
$supervisorStopRequest = Join-Path $installRoot 'supervisor-stop-request.json'
$newAppPath = $null
$backupPath = $null
$markerCreated = $false
$createdStopRequest = $false
$createdSupervisorStopRequest = $false
$monitorStopped = $false
$monitorTaskDisabled = $false

function Assert-ChildPath([string]$Root, [string]$Target) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $targetPath = [IO.Path]::GetFullPath($Target)
    if (-not $targetPath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a path outside the intended directory: $targetPath"
    }
}

function Read-RelayJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { throw "Cannot verify Session Relay operation state in $Path : $_" }
}

function Assert-NoLiveOperations {
    $resetPaths = @((Join-Path $installRoot 'included-reset.json'))
    $resetFolder = Join-Path $installRoot 'reset-operations'
    if (Test-Path -LiteralPath $resetFolder -PathType Container) {
        $resetPaths += @(Get-ChildItem -LiteralPath $resetFolder -Filter '*.json' -File | ForEach-Object FullName)
    }
    foreach ($path in $resetPaths) {
        $operation = Read-RelayJson $path
        if ($operation -and $operation.state -in @('in-progress', 'uncertain')) {
            throw "A Session Relay included reset is still active or needs confirmation. Finish it before installing: $path"
        }
    }

    $handoffPaths = @((Join-Path $installRoot 'journal.json'))
    $operationsFolder = Join-Path $installRoot 'operations'
    if (Test-Path -LiteralPath $operationsFolder -PathType Container) {
        $handoffPaths += @(Get-ChildItem -LiteralPath $operationsFolder -Filter '*.json' -File | ForEach-Object FullName)
    }
    foreach ($path in $handoffPaths) {
        $operation = Read-RelayJson $path
        if ($operation -and $operation.state -eq 'in-progress') {
            throw "A Session Relay handoff is still active. Finish it before installing: $path"
        }
    }
}

function Format-CommandToken([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-ExactCommandLines([string]$Executable, [string[]]$Arguments) {
    $normal = (@($Arguments | ForEach-Object { Format-CommandToken ([string]$_) }) -join ' ')
    $allQuoted = (@($Arguments | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' }) -join ' ')
    $exeNormal = Format-CommandToken $Executable
    $exeQuoted = '"' + $Executable + '"'
    $lines = @()
    foreach ($exeToken in @($exeNormal, $exeQuoted)) {
        foreach ($argsText in @($normal, $allQuoted)) {
            $lines += $exeToken + $(if ($argsText) { ' ' + $argsText } else { '' })
        }
    }
    @($lines | Select-Object -Unique)
}

function Get-UpgradeMarkerSnapshot([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $info=Get-Item -LiteralPath $Path -Force
    $bytes=[IO.File]::ReadAllBytes($Path)
    [pscustomobject]@{
        Bytes=[Convert]::ToBase64String($bytes)
        Length=[long]$bytes.Length
        CreationTimeUtcTicks=[long]$info.CreationTimeUtc.Ticks
        LastWriteTimeUtcTicks=[long]$info.LastWriteTimeUtc.Ticks
        Attributes=[int]$info.Attributes
    }
}

function Test-UpgradeMarkerSnapshotEqual($Expected, $Actual) {
    if (-not $Expected -or -not $Actual) { return $false }
    return ([long]$Expected.Length -eq [long]$Actual.Length -and
        [string]$Expected.Bytes -ceq [string]$Actual.Bytes -and
        [long]$Expected.CreationTimeUtcTicks -eq [long]$Actual.CreationTimeUtcTicks -and
        [long]$Expected.LastWriteTimeUtcTicks -eq [long]$Actual.LastWriteTimeUtcTicks -and
        [int]$Expected.Attributes -eq [int]$Actual.Attributes)
}

function Get-UpgradeMarkerDisposition([long]$AgeSeconds, [string]$OwnerState,
        [bool]$InstallerRunning, [bool]$OperationsClear, [bool]$SnapshotStable) {
    if ($AgeSeconds -lt 300) { return 'young' }
    if ($OwnerState -eq 'active') { return 'owner-live' }
    if ($InstallerRunning) { return 'installer-live' }
    if ($OwnerState -eq 'unknown') { return 'owner-unknown' }
    if (-not $OperationsClear) { return 'operation-active' }
    if (-not $SnapshotStable) { return 'marker-changed' }
    return 'stale'
}

function Get-InstallerOwnerState([int]$OwnerPid, [string]$InstallerPath) {
    if ($OwnerPid -le 0) { return 'unknown' }
    if ($OwnerPid -eq $PID) { return 'dead' }
    try { $process=Get-CimInstance Win32_Process -Filter "ProcessId = $OwnerPid" }
    catch { return 'unknown' }
    if (-not $process) { return 'dead' }
    $exeName=[IO.Path]::GetFileName([string]$process.ExecutablePath)
    if ($exeName -notin @('powershell.exe','pwsh.exe') -or -not $process.CommandLine) { return 'unknown' }
    $pattern='(?i)(?:^|\s)-file\s+"?'+[Regex]::Escape($InstallerPath)+'"?(?=\s|$)'
    if ([Regex]::IsMatch([string]$process.CommandLine,$pattern)) { return 'active' }
    return 'unknown'
}

function Test-OtherInstallerRunning([string]$InstallerPath) {
    $pattern='(?i)(?:^|\s)-file\s+"?'+[Regex]::Escape($InstallerPath)+'"?(?=\s|$)'
    foreach ($process in Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' or Name = 'pwsh.exe'") {
        if ([int]$process.ProcessId -eq $PID -or -not $process.CommandLine) { continue }
        if ([Regex]::IsMatch([string]$process.CommandLine,$pattern)) { return $true }
    }
    return $false
}

function Reconcile-AbandonedUpgradeMarker {
    $snapshot=Get-UpgradeMarkerSnapshot $upgradeMarker
    if (-not $snapshot) { return }
    $age=[long][Math]::Floor(((Get-Date).ToUniversalTime()-
        (Get-Item -LiteralPath $upgradeMarker -Force).LastWriteTimeUtc).TotalSeconds)
    $bytes=[Convert]::FromBase64String($snapshot.Bytes)
    $ownerText=[Text.Encoding]::UTF8.GetString($bytes).Trim()
    $ownerState='dead'
    if ($ownerText) {
        if ($ownerText -notmatch '^\d+$') { $ownerState='unknown' }
        else { $ownerState=Get-InstallerOwnerState ([int]$ownerText) ([IO.Path]::GetFullPath((Join-Path $bundleRoot 'install-bundle.ps1'))) }
    }
    $installerPath=[IO.Path]::GetFullPath((Join-Path $bundleRoot 'install-bundle.ps1'))
    $installerRunning=Test-OtherInstallerRunning $installerPath
    $operationsClear=$true
    try { Assert-NoLiveOperations } catch { $operationsClear=$false }
    $latest=Get-UpgradeMarkerSnapshot $upgradeMarker
    $stable=Test-UpgradeMarkerSnapshotEqual $snapshot $latest
    $disposition=Get-UpgradeMarkerDisposition $age $ownerState $installerRunning $operationsClear $stable
    if ($disposition -ne 'stale') {
        throw "An existing Session Relay upgrade marker is not safely stale ($disposition); resolve the active upgrade or operation and retry."
    }
    # Recheck exact content and metadata immediately before removing only this marker.
    $final=Get-UpgradeMarkerSnapshot $upgradeMarker
    if (-not (Test-UpgradeMarkerSnapshotEqual $snapshot $final)) {
        throw 'The Session Relay upgrade marker changed during stale-marker reconciliation; retry after the other installer finishes.'
    }
    Remove-Item -LiteralPath $upgradeMarker
}

function Get-RelayRoleProcesses([ValidateSet('monitor', 'panel', 'supervisor')][string]$Role) {
    switch ($Role) {
        'monitor' { $scriptName = 'controller.py'; $frozenArgument = '--monitor' }
        'panel' { $scriptName = 'app.py'; $frozenArgument = '--panel' }
        'supervisor' { $scriptName = 'watchdog.py'; $frozenArgument = '--supervisor' }
    }
    $sourceScript = [IO.Path]::GetFullPath((Join-Path $applicationPath $scriptName))
    $exePath = [IO.Path]::GetFullPath((Join-Path $applicationPath 'SessionRelay.exe'))
    $matches = @()
    foreach ($process in Get-CimInstance Win32_Process -Filter "Name = 'SessionRelay.exe' or Name = 'python.exe' or Name = 'pythonw.exe'") {
        if (-not $process.ExecutablePath -or -not $process.CommandLine) { continue }
        try { $actualExecutable = [IO.Path]::GetFullPath($process.ExecutablePath) } catch { continue }
        $arguments = $null
        if ([StringComparer]::OrdinalIgnoreCase.Equals($actualExecutable, $exePath)) {
            $arguments = @($frozenArgument)
        } elseif ([IO.Path]::GetFileName($actualExecutable) -in @('python.exe', 'pythonw.exe')) {
            $arguments = @($sourceScript)
        }
        if ($null -ne $arguments -and
            @(Get-ExactCommandLines $actualExecutable $arguments) -contains [string]$process.CommandLine) {
            $matches += [pscustomobject]@{ ProcessId = [int]$process.ProcessId; Executable = $actualExecutable; CommandLine = [string]$process.CommandLine }
        }
    }
    @($matches | Sort-Object ProcessId -Unique)
}

function Wait-ForRelayRoleExit([ValidateSet('monitor', 'panel', 'supervisor')][string]$Role, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-RelayRoleProcesses $Role).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return (@(Get-RelayRoleProcesses $Role).Count -eq 0)
}

function Stop-RelayMonitorGracefully {
    $monitors = @(Get-RelayRoleProcesses 'monitor')
    if (-not $monitors.Count) { $script:monitorStopped = $true; return }
    if (-not (Test-Path -LiteralPath $stopRequest)) {
        '{"reason":"upgrade"}' | Set-Content -LiteralPath $stopRequest -Encoding UTF8
        $script:createdStopRequest = $true
    }
    if (-not (Wait-ForRelayRoleExit 'monitor' 90)) {
        throw 'Session Relay monitor did not exit after its graceful stop request. No process was force-stopped.'
    }
    $script:monitorStopped = $true
    if ($script:createdStopRequest -and (Test-Path -LiteralPath $stopRequest)) {
        Remove-Item -LiteralPath $stopRequest -ErrorAction SilentlyContinue
    }
}

function Close-RelayPanelGracefully {
    $panels = @(Get-RelayRoleProcesses 'panel')
    if (-not $panels.Count) { return }
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class RelayBundlePanelWindow { [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l); }'
    foreach ($item in $panels) {
        $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
        if ($process -and $process.MainWindowHandle -ne [IntPtr]::Zero) {
            [RelayBundlePanelWindow]::PostMessage($process.MainWindowHandle, 0x10, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
        }
    }
    if (-not (Wait-ForRelayRoleExit 'panel' 30)) {
        throw 'Session Relay panel did not close after WM_CLOSE. No process was force-stopped.'
    }
}

function Get-VerifiedMonitorTask($TaskFolder) {
    try { $task = $TaskFolder.GetTask('SessionRelayMonitor') }
    catch {
        if ($_.Exception.HResult -eq -2147024894) { return $null }
        throw "Could not verify the SessionRelayMonitor scheduled task: $_"
    }
    $actions = $task.Definition.Actions
    if ($actions.Count -ne 1) { throw 'SessionRelayMonitor has an unexpected action list; refusing to stop or replace it.' }
    $action = $actions.Item(1)
    if ($action.Type -ne 0) { throw 'SessionRelayMonitor is not an executable task; refusing to stop or replace it.' }
    $execute = [Environment]::ExpandEnvironmentVariables([string]$action.Path).Trim().Trim('"')
    if (-not [IO.Path]::IsPathRooted($execute)) { throw 'SessionRelayMonitor executable path is not absolute; refusing to stop or replace it.' }
    $execute = [IO.Path]::GetFullPath($execute)
    $arguments = ([string]$action.Arguments).Trim()
    $workingDirectory = [Environment]::ExpandEnvironmentVariables([string]$action.WorkingDirectory).Trim().Trim('"')
    if ($workingDirectory) { $workingDirectory = [IO.Path]::GetFullPath($workingDirectory) }

    $expectedExe = [IO.Path]::GetFullPath((Join-Path $applicationPath 'SessionRelay.exe'))
    $expectedScript = [IO.Path]::GetFullPath((Join-Path $applicationPath 'watchdog.py'))
    $sourceArgument = $arguments.Trim('"')
    $isFrozen = [StringComparer]::OrdinalIgnoreCase.Equals($execute, $expectedExe) -and $arguments -ieq '--supervisor'
    $isSource = [IO.Path]::GetFileName($execute) -in @('python.exe', 'pythonw.exe') -and
        [StringComparer]::OrdinalIgnoreCase.Equals($sourceArgument, $expectedScript)
    if ((-not $isFrozen -and -not $isSource) -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($workingDirectory, $applicationPath)) {
        throw 'SessionRelayMonitor is not an exact Session Relay supervisor action; refusing to stop or replace it.'
    }
    [pscustomobject]@{ Task = $task; Executable = $execute; Arguments = $arguments; IsFrozen = $isFrozen }
}

function Stop-RelaySupervisorAfterMonitor {
    if (-not $script:monitorStopped) { throw 'The Relay monitor must exit before its supervisor can be stopped.' }
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $verified = Get-VerifiedMonitorTask $folder
    $supervisors = @(Get-RelayRoleProcesses 'supervisor')
    if (-not $verified) {
        if ($supervisors.Count) {
            throw 'A Session Relay supervisor is running without a verifiable SessionRelayMonitor task. Close it normally and retry; it was not stopped.'
        }
        return
    }
    foreach ($process in $supervisors) {
        $expectedLines = if ($verified.IsFrozen) {
            @(Get-ExactCommandLines $verified.Executable @('--supervisor'))
        } else {
            @(Get-ExactCommandLines $verified.Executable @((Join-Path $applicationPath 'watchdog.py')))
        }
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($process.Executable, $verified.Executable) -or
            $expectedLines -notcontains $process.CommandLine) {
            throw 'A Session Relay supervisor does not match the registered task action. Close the unmanaged supervisor normally and retry.'
        }
    }
    if ($supervisors.Count -gt 1) {
        throw 'More than one Session Relay supervisor identity is running; refusing to stop an ambiguous task instance.'
    }
    if ($verified.Task.State -eq 4 -and -not $supervisors.Count) {
        throw 'Task Scheduler reports SessionRelayMonitor running, but its exact supervisor process could not be verified.'
    }
    if ($supervisors.Count -and $verified.Task.State -ne 4) {
        throw 'A Session Relay supervisor is running outside the verified scheduled task. Close it normally and retry.'
    }

    $verified.Task.Enabled = $false
    $script:monitorTaskDisabled = $true
    if (-not $supervisors.Count) { return }
    $verifiedSupervisorPid = [int]$supervisors[0].ProcessId

    if (Test-Path -LiteralPath $supervisorStopRequest) {
        $oldRequest = Read-RelayJson $supervisorStopRequest
        $oldPid = 0
        if ($oldRequest) { [void][int]::TryParse([string]$oldRequest.pid, [ref]$oldPid) }
        if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
            throw 'Another Session Relay supervisor stop request is still active.'
        }
        Remove-Item -LiteralPath $supervisorStopRequest
    }
    '{"reason":"upgrade","pid":' + [string]$PID + '}' | Set-Content -LiteralPath $supervisorStopRequest -Encoding UTF8
    $script:createdSupervisorStopRequest = $true
    if (Wait-ForRelayRoleExit 'supervisor' 20) { return }

    $verified = Get-VerifiedMonitorTask $folder
    $supervisors = @(Get-RelayRoleProcesses 'supervisor')
    if (-not $verified -or -not $supervisors.Count) {
        throw 'Session Relay supervisor did not exit through its stop request and is no longer a verified running task; no other process was stopped.'
    }
    foreach ($process in $supervisors) {
        if ($process.ProcessId -ne $verifiedSupervisorPid) {
            throw 'A different supervisor process appeared while upgrading. Refusing to stop an unmanaged instance.'
        }
        $expectedLines = if ($verified.IsFrozen) {
            @(Get-ExactCommandLines $verified.Executable @('--supervisor'))
        } else {
            @(Get-ExactCommandLines $verified.Executable @((Join-Path $applicationPath 'watchdog.py')))
        }
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($process.Executable, $verified.Executable) -or
            $expectedLines -notcontains $process.CommandLine) {
            throw 'The running supervisor no longer matches SessionRelayMonitor; refusing to stop it.'
        }
    }
    # The monitor has already completed its graceful stop. This is the verified
    # Task Scheduler instance whose sole action is the Session Relay supervisor.
    $verified.Task.Stop(0)
    if (-not (Wait-ForRelayRoleExit 'supervisor' 30)) {
        throw 'Task Scheduler could not stop the verified Session Relay supervisor. No process was force-stopped.'
    }
}

Assert-ChildPath $installRoot $applicationPath
Assert-ChildPath $installRoot $upgradeMarker
Assert-ChildPath $installRoot $stopRequest
Assert-ChildPath $installRoot $supervisorStopRequest
if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot 'SessionRelay.exe') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $bundleRoot '_internal\node.exe') -PathType Leaf)) {
    throw 'The extracted standalone bundle is incomplete; SessionRelay.exe or bundled Node.js is missing.'
}
if (Test-Path -LiteralPath $upgradeMarker) { Reconcile-AbandonedUpgradeMarker }
Assert-NoLiveOperations
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null

try {
    $markerStream = [IO.File]::Open($upgradeMarker, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $markerBytes = [Text.Encoding]::UTF8.GetBytes([string]$PID)
    $markerStream.Write($markerBytes, 0, $markerBytes.Length)
    $markerStream.Dispose()
    $markerCreated = $true
} catch [IO.IOException] {
    throw 'Another Session Relay installation is already in progress.'
}

try {
    Stop-RelayMonitorGracefully
    Assert-NoLiveOperations
    Close-RelayPanelGracefully
    Stop-RelaySupervisorAfterMonitor

    $newAppPath = [IO.Path]::GetFullPath((Join-Path $installRoot ('app.staging.' + [Guid]::NewGuid().ToString('N'))))
    $backupPath = [IO.Path]::GetFullPath((Join-Path $installRoot ('app.backup.' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.' + [Guid]::NewGuid().ToString('N').Substring(0, 6))))
    Assert-ChildPath $installRoot $newAppPath
    Assert-ChildPath $installRoot $backupPath
    New-Item -ItemType Directory -Path $newAppPath | Out-Null
    Get-ChildItem -LiteralPath $bundleRoot -Force | Copy-Item -Destination $newAppPath -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $newAppPath 'SessionRelay.exe') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $newAppPath '_internal\node.exe') -PathType Leaf)) {
        throw 'The staged standalone app is incomplete.'
    }
    Assert-NoLiveOperations

    $oldAppMoved = $false
    try {
        if (Test-Path -LiteralPath $applicationPath) {
            if (-not (Test-Path -LiteralPath $applicationPath -PathType Container)) { throw "Install target is not a directory: $applicationPath" }
            Move-Item -LiteralPath $applicationPath -Destination $backupPath
            $oldAppMoved = $true
        }
        Move-Item -LiteralPath $newAppPath -Destination $applicationPath
        $newAppPath = $null
    } catch {
        if ($oldAppMoved -and -not (Test-Path -LiteralPath $applicationPath) -and (Test-Path -LiteralPath $backupPath)) {
            Move-Item -LiteralPath $backupPath -Destination $applicationPath
        }
        throw
    }

    $executable = Join-Path $applicationPath 'SessionRelay.exe'
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $taskFolder = $scheduler.GetFolder('\')

    $monitorTask = $scheduler.NewTask(0)
    $monitorTask.RegistrationInfo.Description = 'Keep Session Relay monitoring independently of Orca and Codex chats'
    $monitorTask.Principal.UserId = $currentUser
    $monitorTask.Principal.LogonType = 3
    $monitorTask.Principal.RunLevel = 0
    $monitorTask.Settings.Enabled = $true
    $monitorTask.Settings.DisallowStartIfOnBatteries = $false
    $monitorTask.Settings.StopIfGoingOnBatteries = $false
    $monitorTask.Settings.ExecutionTimeLimit = 'PT0S'
    $monitorTask.Settings.MultipleInstances = 2
    $monitorTask.Settings.RestartCount = 3
    $monitorTask.Settings.RestartInterval = 'PT1M'
    $monitorTask.Settings.StartWhenAvailable = $true
    $logonTrigger = $monitorTask.Triggers.Create(9)
    $logonTrigger.UserId = $currentUser
    $periodicTrigger = $monitorTask.Triggers.Create(1)
    $periodicTrigger.StartBoundary = (Get-Date).AddSeconds(5).ToString('s')
    $periodicTrigger.Repetition.Interval = 'PT1M'
    $monitorAction = $monitorTask.Actions.Create(0)
    $monitorAction.Path = $executable
    $monitorAction.Arguments = '--supervisor'
    $monitorAction.WorkingDirectory = $applicationPath
    $registeredMonitor = $taskFolder.RegisterTaskDefinition('SessionRelayMonitor', $monitorTask, 6, $currentUser, $null, 3)
    $monitorTaskDisabled = $false

    $panelTask = $scheduler.NewTask(0)
    $panelTask.RegistrationInfo.Description = 'Open the Session Relay control window independently of chats'
    $panelTask.Principal.UserId = $currentUser
    $panelTask.Principal.LogonType = 3
    $panelTask.Principal.RunLevel = 0
    $panelTask.Settings.DisallowStartIfOnBatteries = $false
    $panelTask.Settings.StopIfGoingOnBatteries = $false
    $panelTask.Settings.ExecutionTimeLimit = 'PT0S'
    $panelTask.Settings.MultipleInstances = 2
    $panelAction = $panelTask.Actions.Create(0)
    $panelAction.Path = $executable
    $panelAction.Arguments = '--panel'
    $panelAction.WorkingDirectory = $applicationPath
    $registeredPanel = $taskFolder.RegisterTaskDefinition('SessionRelayPanel', $panelTask, 6, $currentUser, $null, 3)

    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-Item -Path $runKey -Force | Out-Null
    $startupCommand = '"' + $executable + '" --supervisor'
    New-ItemProperty -Path $runKey -Name 'SessionRelay' -Value $startupCommand -PropertyType String -Force | Out-Null

    $desktopPath = [Environment]::GetFolderPath('Desktop')
    $startMenuPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    foreach ($folder in @($desktopPath, $startMenuPath)) {
        if (-not $folder) { continue }
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder 'Session Relay.lnk'))
        $shortcut.TargetPath = $executable
        $shortcut.Arguments = '--open'
        $shortcut.WorkingDirectory = $applicationPath
        $shortcut.Description = 'Automatic account handoff for selected Orca and Codex chats'
        $shortcut.IconLocation = "$executable,0"
        $shortcut.Save()
    }

    if ($markerCreated -and (Test-Path -LiteralPath $upgradeMarker)) {
        Remove-Item -LiteralPath $upgradeMarker
        $markerCreated = $false
    }
    $registeredMonitor.Run($null) | Out-Null
    $registeredPanel.Run($null) | Out-Null
    Write-Output "Installed Session Relay at $applicationPath"
    if ($oldAppMoved) { Write-Output "Previous app folder retained at $backupPath" }
} catch {
    if ($monitorTaskDisabled) {
        try {
            if ($oldAppMoved -and $backupPath -and (Test-Path -LiteralPath $backupPath)) {
                if (Test-Path -LiteralPath $applicationPath) {
                    $failedPath = [IO.Path]::GetFullPath((Join-Path $installRoot ('app.failed.' + [Guid]::NewGuid().ToString('N'))))
                    Assert-ChildPath $installRoot $failedPath
                    Move-Item -LiteralPath $applicationPath -Destination $failedPath
                }
                Move-Item -LiteralPath $backupPath -Destination $applicationPath
            }
            $restoreScheduler = New-Object -ComObject Schedule.Service
            $restoreScheduler.Connect()
            $restoreTask = Get-VerifiedMonitorTask ($restoreScheduler.GetFolder('\'))
            if ($restoreTask) { $restoreTask.Task.Enabled = $true }
        } catch {
            Write-Warning "Could not restore the verified SessionRelayMonitor task after install failure: $_"
        }
    }
    throw
} finally {
    if ($createdStopRequest -and (Test-Path -LiteralPath $stopRequest)) {
        Remove-Item -LiteralPath $stopRequest -ErrorAction SilentlyContinue
    }
    if ($createdSupervisorStopRequest -and (Test-Path -LiteralPath $supervisorStopRequest)) {
        Remove-Item -LiteralPath $supervisorStopRequest -ErrorAction SilentlyContinue
    }
    if ($markerCreated -and (Test-Path -LiteralPath $upgradeMarker)) {
        Remove-Item -LiteralPath $upgradeMarker -ErrorAction SilentlyContinue
    }
}
