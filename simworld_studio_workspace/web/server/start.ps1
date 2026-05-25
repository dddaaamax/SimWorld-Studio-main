# SimWorld Studio - Windows startup script
# Usage: .\start.ps1
#   or with overrides: .\start.ps1 -UnrealPort 55558 -CirrusHttpPort 8785

param(
    [int]$Port          = 3002,
    [string]$UcvPort    = "9002",
    [string]$UnrealPort = "55558",
    [string]$UnrealHost = "127.0.0.1",
    [int]$CirrusHttpPort = 8685,
    [int]$CirrusWsPort   = 8686
)

$env:PORT              = $Port
$env:UCV_PORT          = $UcvPort
$env:UNREAL_PORT       = $UnrealPort
$env:UNREAL_HOST       = $UnrealHost
$env:CIRRUS_HTTP_PORT  = $CirrusHttpPort
$env:CIRRUS_WS_PORT    = $CirrusWsPort

Write-Host "Starting SimWorld Studio server..."
Write-Host "  Studio UI  : http://localhost:$Port"
Write-Host "  UE TCP     : ${UnrealHost}:${UnrealPort}"
Write-Host "  UCV broker : ${UnrealHost}:${UcvPort}"
Write-Host "  Cirrus HTTP: $CirrusHttpPort   WS: $CirrusWsPort"
Write-Host ""

node "$PSScriptRoot\index.js"
