$ErrorActionPreference = 'Stop'

$workspace = [IO.Path]::GetFullPath($PSScriptRoot)
$buildRoot = [IO.Path]::GetFullPath((Join-Path $workspace 'build'))
$distRoot = [IO.Path]::GetFullPath((Join-Path $workspace 'dist'))
$venvRoot = [IO.Path]::GetFullPath((Join-Path $workspace '.venv-build'))
function Assert-PathWithinRoot([string]$Root, [string]$Target) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $targetPath = [IO.Path]::GetFullPath($Target)
    if (-not $targetPath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build output path escapes the intended directory: $targetPath"
    }
}
function Invoke-FrozenCheck([string]$Executable, [string]$Option) {
    # Windowed executables need an explicit wait for a reliable exit code.
    $checkId = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $buildRoot ($checkId + '.stdout')
    $stderrPath = Join-Path $buildRoot ($checkId + '.stderr')
    try {
        $process = Start-Process -FilePath $Executable -ArgumentList $Option -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath }
        if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath }
        if ($process.ExitCode -ne 0) { throw "Frozen executable failed $Option with exit code $($process.ExitCode)." }
    } finally {
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
        }
    }
}
foreach ($path in @($buildRoot, $distRoot)) {
    Assert-PathWithinRoot $workspace $path
}
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

if ($env:OS -ne 'Windows_NT') { throw 'This build script requires Windows.' }
$pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    $basePython = $pythonLauncher.Source
    $pythonLauncherArgs = @('-3.11')
} else {
    $pythonLauncher = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) { throw 'Python 3.11 x64 is required to build Session Relay.' }
    $basePython = $pythonLauncher.Source
    $pythonLauncherArgs = @()
}
$pythonIdentityOutput = & $basePython @pythonLauncherArgs -c 'import sys,struct; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1])+chr(124)+str(struct.calcsize(chr(80))*8))'
$pythonExitCode = $LASTEXITCODE
$pythonIdentity = ($pythonIdentityOutput -join '').Trim()
if ($pythonExitCode -ne 0 -or $pythonIdentity -ne '3.11|64') {
    throw "Expected Python 3.11 x64, found '$pythonIdentity'."
}

if (-not (Test-Path -LiteralPath (Join-Path $venvRoot 'Scripts\python.exe') -PathType Leaf)) {
    & $basePython @pythonLauncherArgs -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv-build.' }
}
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$venvIdentityOutput = & $venvPython -c 'import sys,struct; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1])+chr(124)+str(struct.calcsize(chr(80))*8))'
$venvExitCode = $LASTEXITCODE
$venvIdentity = ($venvIdentityOutput -join '').Trim()
if ($venvExitCode -ne 0 -or $venvIdentity -ne '3.11|64') {
    throw ".venv-build must contain Python 3.11 x64; found '$venvIdentity'."
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $workspace 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing pinned build requirements failed.' }

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand) { $nodeCommand = Get-Command node -ErrorAction SilentlyContinue }
if (-not $nodeCommand) { throw 'Node.js x64 must be available on PATH to build the standalone app.' }
$nodeExecutable = [IO.Path]::GetFullPath($nodeCommand.Source)
$nodeVersion = (& $nodeExecutable --version).Trim() -replace '^v', ''
if ($LASTEXITCODE -ne 0 -or -not $nodeVersion) { throw 'Could not read the Node.js version.' }
$nodeArchitecture = (& $nodeExecutable -p 'process.arch').Trim()
if ($LASTEXITCODE -ne 0 -or $nodeArchitecture -ne 'x64') { throw "Expected Node.js x64; found '$nodeArchitecture'." }

$buildId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$pyinstallerDist = Join-Path $buildRoot ('pyinstaller-dist-' + $buildId)
$pyinstallerWork = Join-Path $buildRoot ('pyinstaller-work-' + $buildId)
$packageRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot ('package-' + $buildId)))
$bundleRoot = Join-Path $packageRoot 'SessionRelay-Windows-x64'
New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null

$oldNodePath = $env:SESSION_RELAY_NODE_PATH
try {
    $env:SESSION_RELAY_NODE_PATH = $nodeExecutable
    & $venvPython -m PyInstaller --noconfirm --distpath $pyinstallerDist --workpath $pyinstallerWork (Join-Path $workspace 'SessionRelay.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller did not complete successfully.' }
} finally {
    if ($null -eq $oldNodePath) { Remove-Item Env:SESSION_RELAY_NODE_PATH -ErrorAction SilentlyContinue }
    else { $env:SESSION_RELAY_NODE_PATH = $oldNodePath }
}

$frozenApp = Join-Path $pyinstallerDist 'SessionRelay'
$frozenExe = Join-Path $frozenApp 'SessionRelay.exe'
if (-not (Test-Path -LiteralPath $frozenExe -PathType Leaf)) { throw "PyInstaller output is missing: $frozenExe" }
if (-not (Test-Path -LiteralPath (Join-Path $frozenApp '_internal\node.exe') -PathType Leaf)) {
    throw 'PyInstaller output is missing bundled _internal\node.exe.'
}
Invoke-FrozenCheck $frozenExe '--version'
Invoke-FrozenCheck $frozenExe '--smoke-test'

Get-ChildItem -LiteralPath $frozenApp -Force | Copy-Item -Destination $bundleRoot -Recurse -Force
foreach ($file in @('install-bundle.ps1', 'Install.cmd', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    $source = Join-Path $workspace $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Required distribution file is missing: $source" }
    Copy-Item -LiteralPath $source -Destination $bundleRoot
}
New-Item -ItemType Directory -Path (Join-Path $bundleRoot 'docs') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $workspace 'docs\INSTALL.md') -Destination (Join-Path $bundleRoot 'docs')
New-Item -ItemType Directory -Path (Join-Path $bundleRoot 'licenses') -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot 'SessionRelay.exe') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $bundleRoot 'install-bundle.ps1') -PathType Leaf)) {
    throw 'Distribution layout check failed: the installer must be adjacent to SessionRelay.exe.'
}

$basePrefix = (& $venvPython -c 'import sys; print(sys.base_prefix)').Trim()
$sitePackages = Join-Path $venvRoot 'Lib\site-packages'
function Copy-RequiredLicense([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Required upstream license file is missing: $Source" }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination
}

Copy-RequiredLicense (Join-Path $basePrefix 'LICENSE.txt') (Join-Path $bundleRoot 'licenses\Python\LICENSE.txt')
$tkLicense = Get-ChildItem -LiteralPath (Join-Path $basePrefix 'tcl') -Filter 'license.terms' -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '[\\/]tk[^\\/]*[\\/]license\.terms$' -and $_.FullName -notmatch '[\\/]demos[\\/]' } |
    Select-Object -First 1
if (-not $tkLicense) { throw 'The Tcl/Tk license.terms file was not found in the selected Python installation.' }
Copy-RequiredLicense $tkLicense.FullName (Join-Path $bundleRoot 'licenses\Tk\license.terms')
$tclVersionOutput = & $venvPython -c 'import tkinter; t=tkinter.Tcl(); print(t.eval(chr(105)+chr(110)+chr(102)+chr(111)+chr(32)+chr(112)+chr(97)+chr(116)+chr(99)+chr(104)+chr(108)+chr(101)+chr(118)+chr(101)+chr(108)))'
$tclVersionExitCode = $LASTEXITCODE
$tclVersion = ($tclVersionOutput -join '').Trim()
if ($tclVersionExitCode -ne 0 -or $tclVersion -notmatch '^\d+\.\d+\.\d+$') { throw "Could not determine the bundled Tcl version: '$tclVersion'." }
$tclLicense = Get-ChildItem -LiteralPath (Join-Path $basePrefix 'tcl') -Filter 'license.terms' -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '[\\/]tcl[^\\/]*[\\/]license\.terms$' -and $_.FullName -notmatch '[\\/](tk|demos)[^\\/]*[\\/]' } |
    Select-Object -First 1
if ($tclLicense) {
    Copy-RequiredLicense $tclLicense.FullName (Join-Path $bundleRoot 'licenses\Tcl\license.terms')
} else {
    $tclTag = 'core-' + ($tclVersion -replace '\.', '-')
    $tclLicense = Join-Path $buildRoot ('tcl-license-' + $buildId + '.terms')
    $tclLicenseUrl = "https://raw.githubusercontent.com/tcltk/tcl/$tclTag/license.terms"
    try {
        Invoke-WebRequest -Uri $tclLicenseUrl -OutFile $tclLicense -UseBasicParsing
    } catch {
        throw "Could not retrieve Tcl $tclVersion license.terms from the official Tcl source tag $tclTag : $_"
    }
    Copy-RequiredLicense $tclLicense (Join-Path $bundleRoot 'licenses\Tcl\license.terms')
}

$websocketLicense = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter 'websocket_client-*.dist-info' |
    ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File | Where-Object { $_.Name -match '^(LICENSE|COPYING)(\.txt)?$' } } |
    Select-Object -First 1
if (-not $websocketLicense) { throw 'The installed websocket-client license file was not found.' }
Copy-RequiredLicense $websocketLicense.FullName (Join-Path $bundleRoot 'licenses\websocket-client\LICENSE')

$pyinstallerDistInfo = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter 'pyinstaller-*.dist-info' |
    Select-Object -First 1
$pyinstallerLicense = if ($pyinstallerDistInfo) {
    Join-Path $pyinstallerDistInfo.FullName 'licenses\COPYING.txt'
} else { $null }
if (-not $pyinstallerLicense -or -not (Test-Path -LiteralPath $pyinstallerLicense -PathType Leaf)) {
    throw 'The PyInstaller bootloader license and exception text were not found in the installed distribution.'
}
Copy-RequiredLicense $pyinstallerLicense (Join-Path $bundleRoot 'licenses\PyInstaller\COPYING.txt')

$nodeLicenseCandidates = @(
    (Join-Path (Split-Path -Parent $nodeExecutable) 'LICENSE'),
    (Join-Path (Split-Path -Parent $nodeExecutable) 'LICENSE.txt')
)
$nodeLicense = $nodeLicenseCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $nodeLicense) {
    $nodeLicense = Join-Path $buildRoot ('node-license-' + $buildId + '.txt')
    $licenseUrl = "https://raw.githubusercontent.com/nodejs/node/v$nodeVersion/LICENSE"
    try {
        Invoke-WebRequest -Uri $licenseUrl -OutFile $nodeLicense -UseBasicParsing
    } catch {
        throw "Could not retrieve the official Node.js $nodeVersion license from its matching release tag at $licenseUrl : $_"
    }
}
Copy-RequiredLicense $nodeLicense (Join-Path $bundleRoot 'licenses\Node\LICENSE')

$zipPath = [IO.Path]::GetFullPath((Join-Path $distRoot 'SessionRelay-Windows-x64.zip'))
$checksumPath = [IO.Path]::GetFullPath((Join-Path $distRoot 'SHA256SUMS.txt'))
foreach ($target in @($zipPath, $checksumPath)) {
    if (-not $target.StartsWith($distRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Distribution output path escapes dist/: $target"
    }
}
Compress-Archive -Path $bundleRoot -DestinationPath $zipPath -CompressionLevel Optimal -Force
$archiveCheckRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot ('archive-check-' + $buildId)))
Assert-PathWithinRoot $buildRoot $archiveCheckRoot
New-Item -ItemType Directory -Path $archiveCheckRoot | Out-Null
Expand-Archive -LiteralPath $zipPath -DestinationPath $archiveCheckRoot
$extractedRoot = Join-Path $archiveCheckRoot 'SessionRelay-Windows-x64'
$extractedExe = Join-Path $extractedRoot 'SessionRelay.exe'
$extractedInstaller = Join-Path $extractedRoot 'install-bundle.ps1'
if (-not (Test-Path -LiteralPath $extractedExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $extractedInstaller -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $extractedRoot '_internal\node.exe') -PathType Leaf)) {
    throw 'Archive verification failed: the EXE, adjacent installer, or bundled Node.js is missing.'
}
Invoke-FrozenCheck $extractedExe '--version'
Invoke-FrozenCheck $extractedExe '--smoke-test'
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $checksumPath -Value ($hash + '  SessionRelay-Windows-x64.zip') -Encoding Ascii
Write-Output "Created $zipPath"
Write-Output "Created $checksumPath"
