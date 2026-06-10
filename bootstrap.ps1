# KB bootstrap — one-line install/update (Windows PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/junioorosa/kb/main/bootstrap.ps1 | iex
#
# Raw URLs only serve public repos. While the repo is private, collaborators
# install with gh (it carries their auth):
#
#   gh repo clone junioorosa/kb "$env:USERPROFILE\.kb\app"
#   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.kb\app\installer\install.ps1" -Apply
#
# Re-running is always safe: the clone is updated (ff-only) and the installer
# is idempotent (diffs first, backs up what it overwrites).
#
# Overrides via env vars: KB_REPO (clone URL or local path), KB_APP_DIR
# (clone destination, default ~\.kb\app), KB_BOOTSTRAP_NO_INSTALL=1.

$ErrorActionPreference = "Stop"

$repoUrl = if ($env:KB_REPO) { $env:KB_REPO } else { "https://github.com/junioorosa/kb.git" }
$appDir = if ($env:KB_APP_DIR) { $env:KB_APP_DIR } else { Join-Path $env:USERPROFILE ".kb\app" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "bootstrap: git is required"
}

if (Test-Path (Join-Path $appDir ".git")) {
    Write-Host "bootstrap: updating $appDir"
    git -C $appDir pull --ff-only --quiet
    if ($LASTEXITCODE -ne 0) { throw "bootstrap: git pull failed in $appDir" }
} else {
    Write-Host "bootstrap: cloning $repoUrl -> $appDir"
    $parent = Split-Path -Parent $appDir
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force $parent | Out-Null
    }
    git clone --quiet $repoUrl $appDir 2>$null
    if ($LASTEXITCODE -ne 0) {
        # A plain https clone of a private repo fails without credentials; gh
        # carries the collaborator's auth, so retry through it before giving up.
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            Write-Host "bootstrap: plain clone failed (private repo?) - retrying via gh"
            $slug = $repoUrl -replace "^git@github\.com:", "" -replace "^https://github\.com/", "" -replace "\.git$", ""
            gh repo clone $slug $appDir
            if ($LASTEXITCODE -ne 0) { throw "bootstrap: gh clone failed for $slug" }
        } else {
            throw "bootstrap: clone failed. Private repo? Run 'gh auth login' and retry, or set KB_REPO."
        }
    }
}

if ($env:KB_BOOTSTRAP_NO_INSTALL -eq "1") {
    Write-Host "bootstrap: clone ready (install skipped). Next: powershell -ExecutionPolicy Bypass -File `"$appDir\installer\install.ps1`" -Apply"
    return
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $appDir "installer\install.ps1") -Apply
