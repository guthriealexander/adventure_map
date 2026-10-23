# Rebuild the Adventure Map pipeline environment on a fresh Windows install.
# Run from anywhere (no admin):
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# What it does (all idempotent — safe to re-run):
#   1. creates a Python venv OFF OneDrive (C:\uv\venvs\adventure-map) with uv
#   2. installs requirements.txt into it
#   3. checks the other tools the nightly job needs: ogr2ogr (QGIS/OSGeo4W),
#      Node/npx + a cached `wrangler login`, Garmin token, R2 credentials
#   4. registers the nightly scheduled task (register_task.ps1)
#
# Prereqs you install by hand: uv (winget install astral-sh.uv), Node.js LTS,
# QGIS (OSGeo4W per-user is fine). Credentials live next to this script
# (storage.env, site.env, .garmin.env, .garmin\) - keep that folder backed up so they survive a reinstall.

$ErrorActionPreference = 'Stop'
$PipeDir = $PSScriptRoot
$Venv    = 'C:\uv\venvs\adventure-map'
$Python  = Join-Path $Venv 'Scripts\python.exe'

Write-Host "== Python venv ($Venv)" -ForegroundColor Cyan
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv not found: winget install astral-sh.uv' }
if (-not (Test-Path $Python)) { uv venv --python 3.12 $Venv }
uv pip install --python $Python -r (Join-Path $PipeDir 'requirements.txt')
if ($LASTEXITCODE) { throw 'pip install failed' }

Write-Host "`n== Tool checks" -ForegroundColor Cyan
Push-Location $PipeDir
try {
    & $Python -c "import build_pmtiles as b; print('ogr2ogr:', b.OGR)"
    & $Python -c "import auto_import as a, os; print('Camera Roll:', a.DEFAULT_DIR, '(exists)' if os.path.isdir(a.DEFAULT_DIR) else '(MISSING)')"
} finally { Pop-Location }
foreach ($f in 'site.env', 'storage.env') {
    $ok = Test-Path (Join-Path $PipeDir $f)
    Write-Host ("{0,-28} {1}" -f $f, $(if ($ok) { 'ok' } else { 'MISSING' }))
}
Write-Host "Cloudflare hosting only: check the login with  npx wrangler whoami  (in web\)"
Write-Host "If the Garmin token is missing/expired:  & '$Python' garmin_sync.py login"

Write-Host "`n== Scheduled task" -ForegroundColor Cyan
& (Join-Path $PipeDir 'register_task.ps1') -Python $Python
