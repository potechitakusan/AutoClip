param([string]$Python = '3.12', [switch]$BuiltinUpscale, [switch]$CpuOnly)
$ErrorActionPreference='Stop'
if ($CpuOnly -and -not $BuiltinUpscale) { throw 'Use -BuiltinUpscale -CpuOnly to install the built-in CPU upscaler.' }
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:UV_CACHE_DIR=Join-Path (Get-Location) '.cache\uv'
$env:RUST_LOG='error'
function Run-Uv([string[]]$Arguments) {
 & uv @Arguments
 if ($LASTEXITCODE -ne 0) { throw "uv failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path -LiteralPath .venv-tools/Scripts/python.exe)) { Run-Uv -Arguments @('venv','.venv-tools','--python',$Python) }
Run-Uv -Arguments @('pip','install','--python','.venv-tools/Scripts/python.exe','-r','requirements-tools.txt')
if (-not $BuiltinUpscale) {
 Write-Output 'Tools environment ready. Next: .\autoclip.cmd scan (input only). Ask about existing ComfyUI / StabilityMatrix and get permission before scan --discover-env.'
 Write-Output 'Built-in upscaler not installed. Only if selected: .\scripts\setup.ps1 -BuiltinUpscale'
 return
}
if (-not (Test-Path -LiteralPath .venv/Scripts/python.exe)) { Run-Uv -Arguments @('venv','.venv','--python',$Python) }
$wheelIndex = if ($CpuOnly) { 'https://download.pytorch.org/whl/cpu' } else { 'https://download.pytorch.org/whl/cu130' }
Run-Uv -Arguments @('pip','install','--python','.venv/Scripts/python.exe','torch==2.10.0','--index-url',$wheelIndex)
Run-Uv -Arguments @('pip','install','--python','.venv/Scripts/python.exe','-r','requirements-upscale.txt')
& .venv/Scripts/python.exe -c "import torch; print('Torch:',torch.__version__,'CUDA:',torch.cuda.is_available())"
if ($LASTEXITCODE -ne 0) { throw 'Inference environment validation failed' }
