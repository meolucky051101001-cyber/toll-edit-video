Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "       TOOL V1 - AUTO SETUP SCRIPT          " -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

# 1. Check Python
Write-Host "[1/7] Checking Python installation..." -ForegroundColor Yellow
if (!(Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Error: Python is not installed or not in PATH." -ForegroundColor Red
    Exit 1
}
$pythonVersion = python --version 2>&1
Write-Host "Found $pythonVersion" -ForegroundColor Green

# 2. Check FFmpeg
Write-Host "[2/7] Checking FFmpeg installation..." -ForegroundColor Yellow
if (!(Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "Error: FFmpeg is not installed or not in PATH." -ForegroundColor Red
    Exit 1
}
Write-Host "FFmpeg is installed." -ForegroundColor Green

# 3. Create venv
Write-Host "[3/7] Creating Virtual Environment (backend/venv)..." -ForegroundColor Yellow
if (-not (Test-Path "backend\venv")) {
    python -m venv backend\venv
    Write-Host "Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "Virtual environment already exists." -ForegroundColor Green
}

# 4. Copy .env.example
Write-Host "[4/7] Setting up .env file..." -ForegroundColor Yellow
if (-not (Test-Path "backend\.env")) {
    Copy-Item "backend\.env.example" "backend\.env"
    Write-Host "Copied .env.example to .env. Please fill in your API keys." -ForegroundColor Green
} else {
    Write-Host ".env already exists." -ForegroundColor Green
}

# 5. Create runtime folders
Write-Host "[5/7] Creating runtime folders..." -ForegroundColor Yellow
 = @("D:\video phôi", "backend\workspace", "backend\workspace\downloads")
foreach ( in ) {
    if (-not (Test-Path )) {
        New-Item -ItemType Directory -Force -Path  | Out-Null
        Write-Host "Created " -ForegroundColor Green
    }
}

# 6. Install dependencies
Write-Host "[6/7] Upgrading pip and installing requirements..." -ForegroundColor Yellow
& "backend\venv\Scripts\python.exe" -m pip install --upgrade pip
& "backend\venv\Scripts\python.exe" -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
& "backend\venv\Scripts\python.exe" -m pip install -r requirements.txt
Write-Host "Dependencies installed." -ForegroundColor Green

# 7. Run Preflight Health Check
Write-Host "[7/7] Running Preflight Health Check..." -ForegroundColor Yellow
& "backend\venv\Scripts\python.exe" "backend\v1_preflight.py"
 = 

if ( -eq 0) {
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host " SETUP COMPLETE! Tool V1 is ready to run." -ForegroundColor Green
    Write-Host " Start the tool using: backend\venv\Scripts\python.exe backend\main.py" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
} else {
    Write-Host "=============================================" -ForegroundColor Red
    Write-Host " SETUP COMPLETED WITH WARNINGS/ERRORS." -ForegroundColor Red
    Write-Host " Please check the preflight output above." -ForegroundColor Red
    Write-Host "=============================================" -ForegroundColor Red
}
