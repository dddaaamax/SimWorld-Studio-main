# SimWorld Studio - Windows PowerShell startup script
# Usage: .\start.ps1 [-Dev] [-NoBuild]

param(
    [switch]$Dev,      # Start frontend dev server (hot reload) instead of serving dist
    [switch]$NoBuild   # Skip frontend build step
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host "     SimWorld Studio - Starting Up" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host ""

# -- Config -------------------------------------------------------------------
$env:PORT              = if ($env:PORT)              { $env:PORT }              else { "3002" }
$env:UCV_PORT          = if ($env:UCV_PORT)          { $env:UCV_PORT }          else { "9002" }
$env:UNREAL_PORT       = if ($env:UNREAL_PORT)       { $env:UNREAL_PORT }       else { "55558" }
$env:CIRRUS_HTTP_PORT  = if ($env:CIRRUS_HTTP_PORT)  { $env:CIRRUS_HTTP_PORT }  else { "8685" }
$env:CIRRUS_WS_PORT    = if ($env:CIRRUS_WS_PORT)    { $env:CIRRUS_WS_PORT }    else { "8686" }
$env:LOG_LEVEL         = if ($env:LOG_LEVEL)         { $env:LOG_LEVEL }         else { "info" }
$env:NODE_ENV          = if ($Dev)                   { "development" }          else { "production" }

Write-Host "Config:" -ForegroundColor Yellow
Write-Host "  Server port  : $($env:PORT)"
Write-Host "  UnrealCV     : $($env:UCV_PORT)"
Write-Host "  UnrealMCP    : $($env:UNREAL_PORT)"
Write-Host "  Cirrus HTTP  : $($env:CIRRUS_HTTP_PORT)"
Write-Host "  Mode         : $($env:NODE_ENV)"
Write-Host ""

# -- Build frontend (unless -Dev or -NoBuild) ---------------------------------
if (-not $Dev -and -not $NoBuild) {
    Write-Host "Building frontend..." -ForegroundColor Yellow
    Push-Location "$ScriptDir"
    & npm run build
    if ($LASTEXITCODE -ne 0) { Write-Error "Frontend build failed"; exit 1 }
    Pop-Location
    Write-Host "Frontend built." -ForegroundColor Green
    Write-Host ""
}

# -- Start backend ------------------------------------------------------------
Write-Host "Starting backend server on :$($env:PORT)..." -ForegroundColor Yellow
$serverJob = Start-Job -ScriptBlock {
    param($serverDir, $envVars)
    foreach ($kv in $envVars.GetEnumerator()) { [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value) }
    Set-Location $serverDir
    & node index.js
} -ArgumentList "$ScriptDir\server", @{
    PORT             = $env:PORT
    UCV_PORT         = $env:UCV_PORT
    UNREAL_PORT      = $env:UNREAL_PORT
    CIRRUS_HTTP_PORT = $env:CIRRUS_HTTP_PORT
    CIRRUS_WS_PORT   = $env:CIRRUS_WS_PORT
    LOG_LEVEL        = $env:LOG_LEVEL
    NODE_ENV         = $env:NODE_ENV
}

Start-Sleep -Seconds 3

# -- Start frontend dev server (if -Dev) --------------------------------------
$frontendJob = $null
if ($Dev) {
    Write-Host "Starting frontend dev server..." -ForegroundColor Yellow
    $frontendJob = Start-Job -ScriptBlock {
        param($webDir)
        Set-Location $webDir
        & npm run dev
    } -ArgumentList $ScriptDir
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "  SimWorld Studio is running!" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
$url = if ($Dev) { "http://localhost:5173" } else { "http://localhost:$($env:PORT)" }
Write-Host "  Open: $url" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop all services." -ForegroundColor Gray
Write-Host ""

# -- Wait and stream logs -----------------------------------------------------
try {
    while ($true) {
        $out = Receive-Job $serverJob 2>&1
        if ($out) { Write-Host "[server] $out" }
        if ($frontendJob) {
            $fout = Receive-Job $frontendJob 2>&1
            if ($fout) { Write-Host "[frontend] $fout" }
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Write-Host "`nStopping services..." -ForegroundColor Yellow
    if ($serverJob)   { Stop-Job $serverJob;   Remove-Job $serverJob }
    if ($frontendJob) { Stop-Job $frontendJob; Remove-Job $frontendJob }
    Write-Host "Stopped." -ForegroundColor Green
}
