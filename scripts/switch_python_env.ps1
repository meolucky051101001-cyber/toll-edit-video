# Manual environment switch. Never stops services or deletes environment data.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Cutover', 'Rollback')]
    [string]$Mode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backendRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'backend'))

function Get-EnvironmentPath([string]$Name) {
    if ($Name -notin @('venv', 'venv_py310', 'venv_py312')) {
        throw 'Unexpected environment name.'
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $backendRoot $Name))
    if (-not [IO.Path]::GetDirectoryName($resolved).Equals($backendRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Environment path is outside the project backend.'
    }
    return $resolved
}

function Assert-NormalDirectory([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Expected a regular directory, not a junction or symlink.'
    }
}

function Assert-ServicesStopped {
    $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($listeners | Where-Object { $_.Port -in @(8088, 8090) }).Count -gt 0) {
        throw 'Port 8088 or 8090 is still listening. Stop Tool V1 manually first.'
    }
    # Do not print command lines: they can contain secrets.
    $processes = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
    $projectPattern = [regex]::Escape($projectRoot)
    $servicePattern = '(?i)(?:^|[\\/\s"''])(?:main|telegram_bot|telegram_script_bot|background_service|ops_watchdog|tool_control)\.py(?:[\s"'']|$)'
    foreach ($process in $processes) {
        $name = [string]$process.Name
        if ($name -notmatch '(?i)^(?:python(?:w|[0-9.]*)?|uvicorn|ffmpeg|ffprobe)\.exe$') {
            continue
        }
        $command = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        if (-not $command -and -not $executable) {
            throw 'Cannot inspect all Python/media processes. Retry from an elevated console.'
        }
        if ($command -match $projectPattern -or $executable -match $projectPattern -or $command -match $servicePattern) {
            throw ('Tool V1 process is still running (PID {0}). Stop it manually first.' -f $process.ProcessId)
        }
    }
}

function Assert-PythonVersion([string]$EnvironmentPath, [string]$Expected) {
    Assert-NormalDirectory $EnvironmentPath
    $configuration = Join-Path $EnvironmentPath 'pyvenv.cfg'
    $version = Select-String -LiteralPath $configuration -Pattern '^version\s*=\s*(\d+\.\d+)(?:\.|\s|$)'
    if (-not $version -or $version.Matches[0].Groups[1].Value -ne $Expected) {
        throw ('Expected a Python {0} environment.' -f $Expected)
    }
    $python = Join-Path $EnvironmentPath 'Scripts\python.exe'
    # Use the explicit interpreter: pip.exe and activation files keep old paths after a rename.
    $actual = & $python -B -I -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
    if ($LASTEXITCODE -ne 0 -or ([string]$actual).Trim() -ne $Expected) {
        throw ('Python {0} interpreter probe failed.' -f $Expected)
    }
}

function Rename-Environment([string]$Source, [string]$Destination) {
    foreach ($path in @($Source, $Destination)) {
        if (-not [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path)).Equals($backendRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing a rename outside the project backend.'
        }
    }
    Assert-NormalDirectory $Source
    if (Test-Path -LiteralPath $Destination) {
        throw 'Destination already exists; no environment will be overwritten.'
    }
    Rename-Item -LiteralPath $Source -NewName ([IO.Path]::GetFileName($Destination)) -ErrorAction Stop
}

try {
    Assert-NormalDirectory $projectRoot
    Assert-NormalDirectory $backendRoot
    $current = Get-EnvironmentPath 'venv'
    if ($Mode -eq 'Cutover') {
        $incoming = Get-EnvironmentPath 'venv_py312'
        $parked = Get-EnvironmentPath 'venv_py310'
        $currentVersion = '3.10'
        $incomingVersion = '3.12'
    } else {
        $incoming = Get-EnvironmentPath 'venv_py310'
        $parked = Get-EnvironmentPath 'venv_py312'
        $currentVersion = '3.12'
        $incomingVersion = '3.10'
    }
    if (Test-Path -LiteralPath $parked) {
        throw 'Backup destination already exists. No changes were made.'
    }
    Assert-ServicesStopped
    Assert-PythonVersion $current $currentVersion
    Assert-PythonVersion $incoming $incomingVersion
    if ($Mode -eq 'Cutover') {
        $python = Join-Path $incoming 'Scripts\python.exe'
        & $python -B -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 pip check failed. No changes were made.' }
    }

    Assert-ServicesStopped
    $firstRenamed = $false
    $activated = $false
    try {
        Rename-Environment $current $parked
        $firstRenamed = $true
        Assert-ServicesStopped
        Rename-Environment $incoming $current
        $activated = $true
        Assert-PythonVersion $current $incomingVersion
    } catch {
        $failure = $_
        try {
            if ($activated) { Rename-Environment $current $incoming }
            if ($firstRenamed) { Rename-Environment $parked $current }
            Write-Warning 'Switch failed; original directory names were restored.'
        } catch {
            Write-Warning 'Could not restore directory names. Keep services stopped and inspect backend/venv* manually; no data was deleted.'
        }
        throw $failure
    }
    Write-Host ('Environment switched to Python {0}. Services remain stopped.' -f $incomingVersion)
    Write-Host 'Use backend\venv\Scripts\python.exe -m pip / -m pytest / -m uvicorn.'
    Write-Host 'Do not use activation files or the old pip.exe/pytest.exe/uvicorn.exe launchers after renaming.'
    Write-Host 'No data was deleted. Start Tool V1 manually only after your checks.'
    exit 0
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
