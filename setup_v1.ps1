param(
    [string]$PythonExe = "python",
    [switch]$PreloadOCR
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $ProjectDir "backend"
$VenvDir = Join-Path $BackendDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $PythonExe -m venv $VenvDir
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu130
& $VenvPython -m pip install -r (Join-Path $ProjectDir "requirements.txt")

$OcrSetup = Join-Path $BackendDir "setup_v1_models.ps1"
& $OcrSetup -PythonExe $VenvPython -Preload:$PreloadOCR

& $VenvPython -c "import torch; print('Tool V1 CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
Write-Host "[Tool V1] Setup complete. Copy backend\.env.example to backend\.env before starting the bot."
