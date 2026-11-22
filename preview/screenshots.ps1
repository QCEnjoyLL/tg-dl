# Regenerate the README screenshots (docs/ui-*.png) from the local preview.
#
#   powershell -ExecutionPolicy Bypass -File preview\screenshots.ps1
#
# The preview pages are static mocks (preview/build.py), so no Telegram session
# and no running app server are needed. Chrome/Edge is located automatically or
# passed explicitly:
#
#   .\preview\screenshots.ps1 -Chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
#
# Keep the sizes in $shots in sync with the README layout: desktop 1440x900,
# login 900x760, mobile 390x760.

param(
    [string]$Chrome = "",
    [int]$Port = 8123,
    [int]$BudgetMs = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Preview = Join-Path $Root "preview"
$Docs = Join-Path $Root "docs"

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

# Build the static preview pages, then add the config-dialog variant.
Push-Location $Root
try {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    $python = if ($env:PYTHON) { $env:PYTHON } elseif (Test-Path $venvPython) { $venvPython } else { "python" }
    & $python "preview/build.py"
    if ($LASTEXITCODE -ne 0) { throw "preview/build.py failed" }

    $index = Get-Content (Join-Path $Preview "index.html") -Raw -Encoding UTF8
    $click = "<script>window.addEventListener('load',function(){setTimeout(function(){var b=document.getElementById('config-btn');if(b){b.click();}},400);});</script>"
    $index.Replace("</body>", "$click</body>") | Set-Content (Join-Path $Preview "index-config.html") -Encoding UTF8 -NoNewline

    $shots = @(
        @{ Name = "ui-light.png";  Width = 1440; Height = 900; Url = "preview/index.html?theme=light" },
        @{ Name = "ui-dark.png";   Width = 1440; Height = 900; Url = "preview/index.html?theme=dark" },
        @{ Name = "ui-config.png"; Width = 1440; Height = 900; Url = "preview/index-config.html?theme=light" },
        @{ Name = "ui-failed.png"; Width = 1440; Height = 900; Url = "preview/index.html?theme=light#failed" },
        @{ Name = "ui-login.png";  Width = 900;  Height = 760; Url = "preview/login.html?theme=light" },
        @{ Name = "ui-mobile.png"; Width = 390;  Height = 760; Url = "preview/index.html?theme=light" }
    )

    $server = Start-Process -FilePath $python -ArgumentList @("-m", "http.server", "$Port", "--bind", "127.0.0.1") -PassThru -WindowStyle Hidden
    try {
        Start-Sleep -Seconds 1
        foreach ($shot in $shots) {
            $profile = Join-Path $env:TEMP ("tdl-shot-" + [guid]::NewGuid().ToString("N"))
            $outFile = Join-Path $Docs $shot.Name
            $args = @(
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--user-data-dir=$profile",
                "--window-size=$($shot.Width),$($shot.Height)",
                "--virtual-time-budget=$BudgetMs",
                "--screenshot=$outFile",
                "http://127.0.0.1:$Port/$($shot.Url)"
            )
            try {
                $proc = Start-Process -FilePath $Chrome -ArgumentList $args -Wait -PassThru -NoNewWindow
                if ($proc.ExitCode -ne 0) { throw "chrome exited with $($proc.ExitCode) for $($shot.Url)" }
            } finally {
                Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue
            }
            Write-Output ("wrote docs/{0}" -f $shot.Name)
        }
    } finally {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
} finally {
    Pop-Location
}

Write-Output "screenshots updated in $Docs"
