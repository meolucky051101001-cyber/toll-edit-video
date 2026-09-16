$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE). See the error above." }
}

try {
    Write-Host "=== Setting up isolated environments for local video download services ===" -ForegroundColor Cyan

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
    if (-not $pythonExe) { throw 'Python 3.12+ was not found to create virtualenvs for upstream downloaders.' }

    $pinnedXhsCommit = 'cc7c78088afc09082f54ea6263a9fd07c2fa510f'
    $pinnedDouyinCommit = '42784ffc83a72a516bfe952153ad7e2a3998d16c'

    # 1. Setup XHS-Downloader venv
    $xhsDir = Join-Path $projectRoot 'scratch\XHS-Downloader'
    if (-not (Test-Path -LiteralPath $xhsDir)) {
        throw "XHS-Downloader directory not found at $xhsDir. Please clone the repo first."
    }

    $actualXhsCommit = (& git -C $xhsDir rev-parse HEAD).Trim()
    if ($actualXhsCommit -ne $pinnedXhsCommit) {
        throw "Commit for XHS-Downloader ($actualXhsCommit) does not match pinned commit ($pinnedXhsCommit)."
    }

    Write-Host "Setting up venv for XHS-Downloader at: $xhsDir (commit: $($actualXhsCommit.Substring(0,7)))" -ForegroundColor Yellow
    $xhsVenv = Join-Path $xhsDir '.venv'
    $xhsPython = Join-Path $xhsVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $xhsPython)) {
        & $pythonExe -m venv $xhsVenv
        Check-Exit 'Create virtualenv for XHS-Downloader'
    }
    $xhsReq = Join-Path $xhsDir 'requirements.txt'
    if (Test-Path -LiteralPath $xhsReq) {
        Write-Host 'Installing dependencies for XHS-Downloader...'
        & $xhsPython -m pip install --upgrade pip
        & $xhsPython -m pip install -r $xhsReq
        Check-Exit 'Install requirements for XHS-Downloader'
    }

    # 2. Setup Douyin_TikTok_Download_API venv
    $douyinDir = Join-Path $projectRoot 'scratch\Douyin_TikTok_Download_API'
    if (-not (Test-Path -LiteralPath $douyinDir)) {
        throw "Douyin_TikTok_Download_API directory not found at $douyinDir. Please clone the repo first."
    }

    $actualDouyinCommit = (& git -C $douyinDir rev-parse HEAD).Trim()
    if ($actualDouyinCommit -ne $pinnedDouyinCommit) {
        throw "Commit for Douyin_TikTok_Download_API ($actualDouyinCommit) does not match pinned commit ($pinnedDouyinCommit)."
    }

    Write-Host "Setting up venv for Douyin_TikTok_Download_API at: $douyinDir (commit: $($actualDouyinCommit.Substring(0,7)))" -ForegroundColor Yellow
    $douyinVenv = Join-Path $douyinDir '.venv'
    $douyinPython = Join-Path $douyinVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $douyinPython)) {
        & $pythonExe -m venv $douyinVenv
        Check-Exit 'Create virtualenv for Douyin_TikTok_Download_API'
    }
    $douyinReq = Join-Path $douyinDir 'requirements.txt'
    if (Test-Path -LiteralPath $douyinReq) {
        Write-Host 'Installing dependencies for Douyin_TikTok_Download_API...'
        & $douyinPython -m pip install --upgrade pip
        & $douyinPython -m pip install -r $douyinReq
        Check-Exit 'Install requirements for Douyin_TikTok_Download_API'
    }

    Write-Host '=== Setup completed successfully for local download services! ===' -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
