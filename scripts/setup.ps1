$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE). See the error above." }
}
try {
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22 LTS or newer from https://nodejs.org then run setup again.' }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'npm not found. Reinstall Node.js with npm enabled.' }
    $nodeVersion = (& node.exe --version).TrimStart('v').Split('.')
    if ([int]$nodeVersion[0] -lt 22) { throw 'Node.js 22+ is required.' }
    $pythonCandidates = @()
    if ($env:RESEARCH_PYTHON) { $pythonCandidates += $env:RESEARCH_PYTHON }
    $pythonCandidates += (Join-Path $projectRoot '.venv\Scripts\python.exe')
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $candidate = (& py.exe -3 -c 'import sys; print(sys.executable)' | Select-Object -First 1)
        if ($candidate) { $pythonCandidates += $candidate }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notmatch 'WindowsApps') { $pythonCandidates += $pythonCommand.Source }
    $pythonCandidates += (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    $pythonExe = $null
    foreach ($candidate in $pythonCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'
            if ($LASTEXITCODE -eq 0) { $pythonExe = $candidate; break }
        }
    }
    if (-not $pythonExe) { throw 'Python 3.12+ was not found. Install it from https://www.python.org/downloads/windows/ and enable Add Python to PATH.' }
    Write-Host "Using Python: $pythonExe"
    Write-Host 'Creating isolated Python environment...'
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & $pythonExe -m venv .venv
        Check-Exit 'Create .venv. If access is blocked, check Windows Security > Ransomware protection > Protection history'
    }
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $requirementsFile = if (Test-Path -LiteralPath 'backend/requirements.lock.txt') { 'backend/requirements.lock.txt' } else { 'backend/requirements.txt' }
    & $venvPython -m pip install -r $requirementsFile
    Check-Exit 'Install backend dependencies'
    & $venvPython -m playwright install chromium
    Check-Exit 'Install Playwright Chromium'
    Push-Location frontend
    try {
        if (Test-Path -LiteralPath 'package-lock.json') { & npm.cmd ci } else { & npm.cmd install }
        Check-Exit 'Install frontend dependencies'
        & npx.cmd playwright install chromium
        Check-Exit 'Install E2E Chromium (matching frontend Playwright version)'
        & npm.cmd run build
        Check-Exit 'Build frontend'
    } finally { Pop-Location }
    foreach ($folder in @('data', 'logs')) {
        New-Item -ItemType Directory -Path (Join-Path $projectRoot $folder) -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    Write-Host 'Setup complete. Double-click start.bat.' -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
