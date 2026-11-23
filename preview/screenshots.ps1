# Regenerate the README screenshots (docs/ui-*.png) from the local preview.
#
#   powershell -ExecutionPolicy Bypass -File preview\screenshots.ps1
#
# The pages are the static mocks written by preview/build.py, so no Telegram
# session and no running app server are needed. The actual capture runs in
# preview/screenshots.py (DevTools protocol): Chrome's command-line
# --window-size clamps small windows, so a "390px phone" screenshot used to be
# rendered at 512px and cropped.
#
# Chrome/Edge is located automatically or passed explicitly:
#
#   .\preview\screenshots.ps1 -Chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
#
# Keep the sizes in preview/screenshots.py::SHOTS in sync with the README
# layout: desktop 1440x900, login 900x760, mobile 390x760.

param(
    [string]$Chrome = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    if (-not $Chrome) {
        $candidates = @(
            (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
            (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
        )
        $Chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $Chrome -or -not (Test-Path $Chrome)) {
        throw "Chrome/Edge not found; pass -Chrome <path>"
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    $python = if ($env:PYTHON) { $env:PYTHON } elseif (Test-Path $venvPython) { $venvPython } else { "python" }
    & $python "preview/screenshots.py" --chrome $Chrome
    if ($LASTEXITCODE -ne 0) { throw "preview/screenshots.py failed" }
} finally {
    Pop-Location
}

Write-Output "screenshots updated in docs"
