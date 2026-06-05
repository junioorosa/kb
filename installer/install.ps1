# install.ps1 -- Windows bootstrap for KB.
#
# The only Windows-specific layer: locate Python, install optional deps, then
# hand off to the OS-agnostic orchestrator (install.py), which does the real work.
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File installer\install.ps1            # dry-run
#   powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Apply     # install/update
#   powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Apply -Time 02:30

#Requires -Version 5.1
param(
    [switch]$Apply,
    [string]$Time = "01:00"
)
$ErrorActionPreference = "Stop"

$InstallerDir = $PSScriptRoot

# --- Locate Python ----------------------------------------------------------
function Resolve-Python {
    # Prefer the py launcher (handles multiple versions), fall back to python.
    $cand = @(
        @{ exe = "py";     args = @("-3") },
        @{ exe = "python"; args = @() },
        @{ exe = "python3"; args = @() }
    )
    foreach ($c in $cand) {
        $cmd = Get-Command $c.exe -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                & $c.exe @($c.args + @("--version")) *> $null
                if ($LASTEXITCODE -eq 0) { return $c }
            } catch {}
        }
    }
    return $null
}

$py = Resolve-Python
if ($null -eq $py) {
    Write-Host "ERROR: Python 3 not found on PATH. Install Python 3, then re-run." -ForegroundColor Red
    exit 1
}
$PyExe = $py.exe
$PyArgs = $py.args
Write-Host "Python: $PyExe $($PyArgs -join ' ')"

# --- Optional deps (graceful: KB degrades to BM25 without them) -------------
Write-Host "Installing optional deps (fastembed, numpy, tiktoken)..."
try {
    & $PyExe @($PyArgs + @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "fastembed", "numpy", "tiktoken"))
    if ($LASTEXITCODE -ne 0) { Write-Host "  deps install returned non-zero -- continuing (semantic retrieval will degrade)." -ForegroundColor Yellow }
} catch {
    Write-Host "  deps install failed -- continuing (semantic retrieval will degrade to BM25)." -ForegroundColor Yellow
}

# --- Hand off to the orchestrator -------------------------------------------
$installPy = Join-Path $InstallerDir "install.py"
$callArgs = $PyArgs + @($installPy, "--time", $Time)
if ($Apply) { $callArgs += "--apply" }

Write-Host ""
& $PyExe @callArgs
$code = $LASTEXITCODE

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry-run only. Re-run with -Apply to install/update." -ForegroundColor Cyan
    exit $code
}

if ($code -ne 0) {
    Write-Host "Install reported errors." -ForegroundColor Yellow
    exit $code
}

Write-Host ""
Write-Host "Done. Press the Windows key and type 'KB Manager' to open the config UI." -ForegroundColor Green
exit $code
