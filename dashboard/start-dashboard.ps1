$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$port = 8601
$url = "http://127.0.0.1:$port/invoices"

function Test-Up {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$port/"
        return $resp.StatusCode -lt 400
    } catch {
        return $false
    }
}

if (-not (Test-Up)) {
    Write-Host "Starting ENW Construction dashboard on port $port..."
    Start-Process -WindowStyle Hidden `
        -FilePath (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") `
        -ArgumentList "-m","uvicorn","main:app","--port",$port `
        -WorkingDirectory $PSScriptRoot `
        -RedirectStandardOutput (Join-Path $PSScriptRoot "uvicorn.out.log") `
        -RedirectStandardError (Join-Path $PSScriptRoot "uvicorn.err.log")

    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Up) { $ok = $true; break }
    }
    if (-not $ok) {
        Write-Host "Dashboard did not start in time - check uvicorn.err.log in this folder." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
    Write-Host "Dashboard is up."
} else {
    Write-Host "Dashboard already running."
}

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (Test-Path $chrome) {
    Start-Process -FilePath $chrome -ArgumentList $url
} else {
    Start-Process $url
}
