$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required for the web frontend."
}

& npm --prefix frontend install --cache frontend\.npm-cache
Write-Host "Environment is ready. Run .\scripts\run-web.ps1 to start the web application."

