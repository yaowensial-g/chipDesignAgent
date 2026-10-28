param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath "frontend\node_modules")) {
    throw "Frontend dependencies not found. Run .\scripts\setup.ps1 first."
}

$api = Start-Process `
    -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $ApiPort `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

try {
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if ($api.HasExited) {
            throw "Backend failed to start. Port $ApiPort may already be in use."
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/v1/health" -TimeoutSec 1
            if ($health.status -eq "ok") {
                $apiReady = $true
                break
            }
        }
        catch {
            # Uvicorn may still be starting; retry within the bounded loop.
        }
    }
    if (-not $apiReady) {
        throw "Backend did not become healthy on port $ApiPort."
    }

    & npm --prefix frontend run dev -- --port $WebPort --strictPort
}
finally {
    if (-not $api.HasExited) {
        Stop-Process -Id $api.Id
    }
}

