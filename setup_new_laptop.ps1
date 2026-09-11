# setup_new_laptop.ps1
# Chạy trên LAPTOP MỚI (RTX 5060) sau khi đã giải nén tool vào C:\tool v1
# Yêu cầu: Python 3.10, NVIDIA Driver R570+, FFmpeg đã cài sẵn
# Chạy: powershell -ExecutionPolicy Bypass -File setup_new_laptop.ps1

$ErrorActionPreference = "Stop"
$toolDir = "C:\tool v1"
$backendDir = "$toolDir\backend"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AutoDub Tool - Setup on New Laptop" -ForegroundColor Cyan
Write-Host "  (RTX 5060 / Blackwell Edition)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ===== CHECK PREREQUISITES =====
Write-Host "[1/7] Checking prerequisites..." -ForegroundColor Yellow

# Python
$pythonVer = & python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Python not found! Install Python 3.10 first." -ForegroundColor Red
    Write-Host "  Download: https://www.python.org/downloads/release/python-31011/" -ForegroundColor Gray
    exit 1
}
Write-Host "  Python: $pythonVer" -ForegroundColor Green

# nvidia-smi
$nvidiaSmi = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: nvidia-smi failed! Install NVIDIA driver R570+ first." -ForegroundColor Red
    exit 1
}
Write-Host "  GPU: $nvidiaSmi" -ForegroundColor Green

# Check driver version >= R570
$driverVer = ($nvidiaSmi -split ",")[1].Trim()
$driverMajor = [int]($driverVer -split "\.")[0]
if ($driverMajor -lt 570) {
    Write-Host "  WARNING: Driver $driverVer may be too old for RTX 5060." -ForegroundColor Red
    Write-Host "  RTX 5060 (Blackwell) requires driver R570+." -ForegroundColor Red
    Write-Host "  Download latest: https://www.nvidia.com/drivers" -ForegroundColor Gray
}

# FFmpeg
try {
    $ffmpegVer = & ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Host "  FFmpeg: OK" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: FFmpeg not found! Install and add to PATH." -ForegroundColor Red
    Write-Host "  Download: https://www.gyan.dev/ffmpeg/builds/" -ForegroundColor Gray
    exit 1
}

# ===== CHECK TOOL FILES =====
Write-Host ""
Write-Host "[2/7] Checking tool files..." -ForegroundColor Yellow

$checks = @(
    @{ Path = "$backendDir\telegram_bot.py"; Name = "Source code" },
    @{ Path = "$backendDir\.env"; Name = ".env config" },
    @{ Path = "$toolDir\MyVoiceModel_v2\mi-giong_cua_toi_v2.pth"; Name = "RVC voice model" },
    @{ Path = "$toolDir\models\v1\faster_whisper\model.bin"; Name = "Whisper model" }
)

$allOk = $true
foreach ($check in $checks) {
    if (Test-Path $check.Path) {
        $size = [math]::Round((Get-Item $check.Path).Length / 1MB, 1)
        Write-Host "  OK: $($check.Name) ($size MB)" -ForegroundColor Green
    } else {
        Write-Host "  MISSING: $($check.Name) - $($check.Path)" -ForegroundColor Red
        $allOk = $false
    }
}

if (-not $allOk) {
    Write-Host ""
    Write-Host "  Some files are missing. Make sure you extracted the full ZIP." -ForegroundColor Red
    $continue = Read-Host "  Continue anyway? (y/n)"
    if ($continue -ne "y") { exit 1 }
}

# ===== CREATE VENV =====
Write-Host ""
Write-Host "[3/7] Creating virtual environment..." -ForegroundColor Yellow
Set-Location $backendDir

if (Test-Path "venv") {
    Write-Host "  venv already exists. Delete and recreate? (y/n)" -ForegroundColor Yellow
    $recreate = Read-Host
    if ($recreate -eq "y") {
        Remove-Item "venv" -Recurse -Force
        & python -m venv venv
        Write-Host "  venv recreated." -ForegroundColor Green
    } else {
        Write-Host "  Using existing venv." -ForegroundColor Gray
    }
} else {
    & python -m venv venv
    Write-Host "  venv created." -ForegroundColor Green
}

# ===== INSTALL PYTORCH WITH CUDA 12.8 (for Blackwell/RTX 5060) =====
Write-Host ""
Write-Host "[4/7] Installing PyTorch 2.7+ with CUDA 12.8 (for RTX 5060)..." -ForegroundColor Yellow
Write-Host "  This will download ~2.5 GB, please wait..." -ForegroundColor Gray
& .\venv\Scripts\pip.exe install --upgrade pip
& .\venv\Scripts\pip.exe install torch torchaudio torchvision --index-url https://download.pytorch.org/whl/cu128

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: PyTorch installation failed!" -ForegroundColor Red
    exit 1
}

# ===== INSTALL REQUIREMENTS =====
Write-Host ""
Write-Host "[5/7] Installing project requirements..." -ForegroundColor Yellow
& .\venv\Scripts\pip.exe install -r ..\requirements.txt

# ===== INSTALL EXTRA PACKAGES =====
Write-Host ""
Write-Host "[6/7] Installing extra packages (RVC, torchcrepe, psutil)..." -ForegroundColor Yellow
& .\venv\Scripts\pip.exe install rvc-python torchcrepe psutil

# ===== VERIFY =====
Write-Host ""
Write-Host "[7/7] Verifying setup..." -ForegroundColor Yellow

# Test CUDA
$cudaCheck = & .\venv\Scripts\python.exe -c @"
import torch
cuda_ok = torch.cuda.is_available()
if cuda_ok:
    gpu = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    vram = torch.cuda.get_device_properties(0).total_mem / 1024**3
    print(f'CUDA=True | GPU={gpu} | Capability=sm_{cap[0]}{cap[1]} | VRAM={vram:.1f}GB')
else:
    print('CUDA=False | GPU not detected!')
"@ 2>&1
Write-Host "  $cudaCheck" -ForegroundColor $(if ($cudaCheck -match "CUDA=True") { "Green" } else { "Red" })

if ($cudaCheck -match "CUDA=False") {
    Write-Host ""
    Write-Host "  CUDA NOT DETECTED! Possible fixes:" -ForegroundColor Red
    Write-Host "  1. Update NVIDIA driver to R570+" -ForegroundColor Yellow
    Write-Host "  2. Reinstall PyTorch: pip install torch --index-url https://download.pytorch.org/whl/cu128" -ForegroundColor Yellow
    Write-Host "  3. Restart the computer after driver update" -ForegroundColor Yellow
}

# Create necessary directories
New-Item -ItemType Directory -Force -Path "D:\banve" -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path "$toolDir\workspace" -ErrorAction SilentlyContinue | Out-Null

# Create auto-start VBS
$startupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$vbsContent = @"
WScript.Sleep 10000
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\tool v1\backend"
WshShell.Run """C:\tool v1\backend\venv\Scripts\python.exe"" telegram_bot.py", 0, False
"@
$vbsPath = "$startupDir\StartVideoDubbingBot_Hidden.vbs"
$vbsContent | Out-File -FilePath $vbsPath -Encoding ASCII
Write-Host "  Auto-start VBS: $vbsPath" -ForegroundColor Green

# ===== DONE =====
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  To test now:" -ForegroundColor White
Write-Host "    cd C:\tool v1\backend" -ForegroundColor Cyan
Write-Host "    .\venv\Scripts\python.exe telegram_bot.py" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Then send a video link on Telegram to test the full pipeline." -ForegroundColor White
Write-Host ""
Write-Host "  NOTE: Make sure the bot is NOT running on the old laptop" -ForegroundColor Yellow
Write-Host "  if you're using the same BOT_TOKEN!" -ForegroundColor Yellow
Write-Host ""
