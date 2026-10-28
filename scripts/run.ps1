param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Run .\scripts\setup.ps1 first."
}

$arguments = @("-m", "uvicorn", "app.main:app", "--host", $HostAddress, "--port", $Port)
if ($Reload) {
    $arguments += "--reload"
}

& $python @arguments

