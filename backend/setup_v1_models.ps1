param(
    [string]$PythonExe = "python",
    [switch]$Preload
)

$ErrorActionPreference = "Stop"
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $BackendDir "model_venv"
$Requirements = Join-Path $BackendDir "requirements-v1-models.txt"
$RuntimePython = Join-Path $RuntimeDir "Scripts\python.exe"
$ProjectDir = Split-Path -Parent $BackendDir
$ModelCache = Join-Path $ProjectDir "models\v1\paddlex"

Write-Host "[Tool V1] Creating isolated PP-OCRv6 runtime..."
if (-not (Test-Path -LiteralPath $RuntimePython)) {
    & $PythonExe -m venv $RuntimeDir
}

& $RuntimePython -m pip install --upgrade pip
& $RuntimePython -m pip install -r $Requirements
& $RuntimePython -c "import paddleocr; print('PaddleOCR', paddleocr.__version__, 'ready for Tool V1')"

if ($Preload) {
    Write-Host "[Tool V1] Preloading PP-OCRv6 Tiny models..."
    $env:PADDLE_PDX_CACHE_HOME = $ModelCache
    & $RuntimePython -c "from paddleocr import PaddleOCR; PaddleOCR(text_detection_model_name='PP-OCRv6_tiny_det', text_recognition_model_name='PP-OCRv6_tiny_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, engine='onnxruntime'); print('PP-OCRv6 Tiny cached')"
}

Write-Host "[Tool V1] Model runtime is ready: $RuntimePython"
