# SimWorld Studio - Windows PowerShell Launcher
# Launches: Cirrus (Pixel Streaming) -> UE Editor -> Web Server
# Usage: .\SimWorld-Studio.ps1 [options]

param(
    [int]   $Port            = 3002,
    [int]   $McpPort         = 55558,
    [int]   $CirrusHttpPort  = 8685,
    [int]   $CirrusWsPort    = 8686,
    [int]   $CirrusSfuPort   = 8989,
    [int]   $Gpu             = 0,
    [string]$Map             = "/Game/Maps/Empty",
    [switch]$RenderOffscreen,
    [switch]$NoBuild,
    [switch]$Help
)

if ($Help) {
    Write-Host @"
SimWorld Studio Launcher (PowerShell)

Usage: .\SimWorld-Studio.ps1 [OPTIONS]

Options:
  -Port            Web UI port          (default: 3002)
  -McpPort         UE MCP/TCP port      (default: 55558)
  -CirrusHttpPort  Cirrus HTTP port      (default: 8685)
  -CirrusWsPort    Cirrus WS port        (default: 8686)
  -CirrusSfuPort   Cirrus SFU port       (default: 8989)
  -Gpu             GPU index             (default: 0)
  -Map             UE map path           (default: /Game/Maps/Empty)
  -RenderOffscreen Run UE without window
  -NoBuild         Skip frontend build
  -Help            Show this help
"@
    exit 0
}

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace  = Join-Path $ScriptDir "simworld_studio_workspace"

# ============================================================
#  USER CONFIGURATION - edit these for your environment
# ============================================================
$UeRoot    = "D:\Unreal Engine\UE_5.3"
$UeProject = "D:\Unreal Engine\logisticsue\Studio\Studio.uproject"
# ============================================================

$UeEditor  = Join-Path $UeRoot "Engine\Binaries\Win64\UnrealEditor.exe"
$CirrusDir = Join-Path $UeRoot "Engine\Plugins\Media\PixelStreaming\Resources\WebServers\SignallingWebServer"
$CirrusJs  = Join-Path $CirrusDir "cirrus.js"

Write-Host ""
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host "   SimWorld Studio - Starting Up" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  UE Editor  : $UeEditor"
Write-Host "  Project    : $UeProject"
Write-Host "  Workspace  : $Workspace"
Write-Host "  Web Port   : $Port"
Write-Host "  MCP Port   : $McpPort"
Write-Host "  Cirrus     : HTTP=$CirrusHttpPort  WS=$CirrusWsPort  SFU=$CirrusSfuPort"
Write-Host "  GPU        : $Gpu"
Write-Host ""

# ============================================================
#  VALIDATE
# ============================================================
if (-not (Test-Path $UeEditor)) {
    Write-Error "[!!] UnrealEditor.exe not found at: $UeEditor`n     Edit UeRoot in this script."
    exit 1
}
if (-not (Test-Path $UeProject)) {
    Write-Error "[!!] Project file not found at: $UeProject`n     Edit UeProject in this script."
    exit 1
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Error "[!!] Node.js not found. Please install Node.js."
    exit 1
}

# Create directories
New-Item -ItemType Directory -Force -Path (Join-Path $Workspace "logs")       | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Workspace "tmp\screens") | Out-Null

# ============================================================
#  RELEASE STALE PORTS
# ============================================================
Write-Host "Releasing stale ports..." -ForegroundColor Yellow
foreach ($p in @($Port, $McpPort, $CirrusHttpPort, $CirrusWsPort, $CirrusSfuPort, 9002)) {
    Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Get-WmiObject Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "cirrus\.js" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# ============================================================
#  SYNC CIRRUS player.js -> web/public/ue-assets/
# ============================================================
$UeAssetsDir      = Join-Path $Workspace "web\public\ue-assets"
$needRebuild = $false
New-Item -ItemType Directory -Force -Path $UeAssetsDir | Out-Null
# Sync player.js and uiless.js from Cirrus public dir
foreach ($jsFile in @("player.js", "uiless.js")) {
    $src  = Join-Path $CirrusDir "Public\$jsFile"
    $dest = Join-Path $UeAssetsDir $jsFile
    if (Test-Path $src) {
        if (-not (Test-Path $dest) -or
            (Get-Item $src).LastWriteTime -gt (Get-Item $dest).LastWriteTime) {
            Copy-Item $src $dest -Force
            Write-Host "[OK] Synced Cirrus $jsFile -> ue-assets" -ForegroundColor Green
            $needRebuild = $true
        }
    } else {
        Write-Warning "[!!] Cirrus $jsFile not found at $src"
    }
}

# ============================================================
#  BUILD FRONTEND (unless -NoBuild)
# ============================================================
if (-not $NoBuild) {
    $webDir = Join-Path $Workspace "web"
    if (-not (Test-Path (Join-Path $webDir "node_modules"))) {
        Write-Host "Installing npm dependencies..." -ForegroundColor Yellow
        Push-Location $webDir
        npm install --no-audit --no-fund
        Pop-Location
    }
    Write-Host "Building frontend..." -ForegroundColor Yellow
    Push-Location $webDir
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Error "Frontend build failed"; exit 1 }
    Pop-Location
    Write-Host "Frontend built." -ForegroundColor Green
}

# ============================================================
#  GENERATE MCP CONFIG
# ============================================================
$McpServerJs = Join-Path $Workspace "web\server\mcp-server.js"
$McpConfig   = Join-Path $Workspace "web\mcp.json"
$McpServerJsEscaped = $McpServerJs.Replace("\", "\\")
$mcpJson = '{"mcpServers":{"simworld":{"command":"node","args":["' + $McpServerJsEscaped + '"],"env":{"UNREAL_HOST":"127.0.0.1","UNREAL_PORT":"' + $McpPort + '"}}}}'
[System.IO.File]::WriteAllText($McpConfig, $mcpJson, (New-Object System.Text.UTF8Encoding $false))

# ============================================================
#  STEP 1: CIRRUS (Pixel Streaming signaling server)
# ============================================================
$cirrusJob = $null
if (Test-Path $CirrusJs) {
    if (-not (Test-Path (Join-Path $CirrusDir "node_modules"))) {
        Write-Host "Installing Cirrus dependencies..." -ForegroundColor Yellow
        Push-Location $CirrusDir
        npm install --no-audit --no-fund | Out-Null
        Pop-Location
    }

    $CirrusConfig = Join-Path $Workspace "cirrus-config.json"
    $cirrusJson = [ordered]@{
        UseFrontend   = $false   # We serve our own frontend; no need for Cirrus built-in
        UseMatchmaker = $false
        HttpPort      = $CirrusHttpPort
        StreamerPort  = $CirrusWsPort
        SFUPort       = $CirrusSfuPort
    } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($CirrusConfig, $cirrusJson, (New-Object System.Text.UTF8Encoding $false))

    Write-Host "Starting Cirrus signaling server..." -ForegroundColor Yellow
    $cirrusJob = Start-Job -Name "cirrus" -ScriptBlock {
        param($dir, $config)
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $OutputEncoding = [System.Text.Encoding]::UTF8
        Set-Location $dir
        & node cirrus.js --configFile=$config 2>&1
    } -ArgumentList $CirrusDir, $CirrusConfig

    $waited = 0
    while ($waited -lt 15) {
        try { $tc = New-Object Net.Sockets.TcpClient; $tc.Connect("127.0.0.1", $CirrusWsPort); $tc.Close(); break } catch { }
        Start-Sleep -Seconds 1; $waited++
    }
    if ($waited -ge 15) { Write-Warning "[!!] Cirrus did not start in time." }
    else { Write-Host "[OK] Cirrus (HTTP:$CirrusHttpPort  WS:$CirrusWsPort)" -ForegroundColor Green }
} else {
    Write-Warning "[!!] Cirrus not found at $CirrusJs - Pixel Streaming will not work."
}

# ============================================================
#  STEP 2: LAUNCH UE EDITOR
# ============================================================
Write-Host "Launching Unreal Engine (GPU $Gpu)..." -ForegroundColor Yellow

$ueArgs = @(
    "`"$UeProject`"",
    $Map,
    "-MCPPort=$McpPort",
    "-NOSPLASH", "-NOSOUND",
    "-ResX=1920", "-ResY=1080",
    "-ExecCmds=DisableAllScreenMessages 0",
    "-graphicsadapter=$Gpu",
    "-EditorPixelStreamingRes=1920x1080",
    "-EditorPixelStreamingStartOnLaunch=true",
    "-EditorPixelStreamingUseRemoteSignallingServer=true",
    "-PixelStreamingURL=ws://127.0.0.1:$CirrusWsPort",
    # Encoder: H264 + CBR for consistent low latency (vs VBR default)
    "-PixelStreamingEncoderCodec=h264",
    "-PixelStreamingEncoderRateControl=CBR",
    "-PixelStreamingEncoderKeyframeInterval=0",
    "-PixelStreamingEncoderTargetBitrate=50000000",
    "-PixelStreamingEncoderMaxBitrate=100000000",
    "-PixelStreamingEncoderMinQP=15",
    "-PixelStreamingEncoderMaxQP=25",
    # WebRTC: start at max bitrate, 60fps, maintain framerate under congestion
    "-PixelStreamingWebRTCFps=60",
    "-PixelStreamingWebRTCStartBitrate=50000000",
    "-PixelStreamingWebRTCMaxBitrate=100000000",
    "-PixelStreamingWebRTCMinBitrate=10000000",
    "-PixelStreamingWebRTCDegradationPreference=MAINTAIN_FRAMERATE",
    "-PixelStreamingWebRTCDisableReceiveAudio=true",
    "-log"
)
if ($RenderOffscreen) { $ueArgs += "-RenderOffScreen" }

$ueProc = Start-Process -FilePath $UeEditor -ArgumentList $ueArgs -PassThru
Write-Host "[OK] UE Editor launched (PID $($ueProc.Id))" -ForegroundColor Green

# ============================================================
#  STEP 3: WAIT FOR MCP PORT
# ============================================================
Write-Host "Waiting for MCP port $McpPort (UE can take ~90s)..." -ForegroundColor Yellow
$waited = 0
while ($waited -lt 120) {
    try { $tc = New-Object Net.Sockets.TcpClient; $tc.Connect("127.0.0.1", $McpPort); $tc.Close(); break } catch { }
    Start-Sleep -Seconds 2
    $waited += 2
    Write-Host -NoNewline "."
}
Write-Host ""
if ($waited -ge 120) {
    Write-Warning "[!!] Timeout waiting for MCP port $McpPort. UE may have crashed."
    exit 1
}
Write-Host "[OK] MCP port $McpPort ready!" -ForegroundColor Green

# ============================================================
#  STEP 4: START WEB SERVER
# ============================================================
Write-Host "Starting web server on port $Port..." -ForegroundColor Yellow

$webJob = Start-Job -Name "server" -ScriptBlock {
    param($dir, $envVars)
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    Set-Location $dir
    foreach ($kv in $envVars.GetEnumerator()) {
        [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value)
    }
    & node server\index.js 2>&1
} -ArgumentList (Join-Path $Workspace "web"), @{
    PORT                = "$Port"
    UNREAL_HOST         = "127.0.0.1"
    UNREAL_PORT         = "$McpPort"
    UCV_PORT            = "9002"
    CIRRUS_HTTP_PORT    = "$CirrusHttpPort"
    CIRRUS_WS_PORT      = "$CirrusWsPort"
    PIXEL_STREAMING_URL = "http://127.0.0.1:$CirrusHttpPort"
    NODE_ENV            = "production"
}

Start-Sleep -Seconds 2
Write-Host "[OK] Web server started" -ForegroundColor Green

# ============================================================
#  DONE
# ============================================================
Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "   SimWorld Studio is running!" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host "  Open: http://localhost:$Port" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop all services." -ForegroundColor Gray
Write-Host ""

# ============================================================
#  STREAM LOGS (all jobs, tagged by module name)
# ============================================================
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logSources = @(
    @{ Job = $cirrusJob; Tag = "cirrus"; Color = "DarkCyan" }
    @{ Job = $webJob;    Tag = "server"; Color = "Gray" }
)

try {
    while ($true) {
        foreach ($src in $logSources) {
            if (-not $src.Job) { continue }
            $lines = Receive-Job $src.Job 2>&1
            foreach ($line in $lines) {
                $text = "$line".Trim()
                if (-not $text) { continue }
                # Strip ANSI escape codes (e.g. [1m[32m from Cirrus color output)
                $text = $text -replace '\x1b\[[0-9;]*[mGKHF]', ''
                $text = $text -replace '\[[0-9;]*m', ''
                # Strip remaining non-printable / box-drawing chars, keep ASCII + CJK
                $text = ($text.ToCharArray() | Where-Object {
                    ([int]$_ -ge 0x20 -and [int]$_ -le 0x7E) -or
                    ([int]$_ -ge 0x4E00 -and [int]$_ -le 0x9FFF)
                }) -join ''
                $text = $text.Trim()
                if ($text) { Write-Host "[$($src.Tag)] $text" -ForegroundColor $src.Color }
            }
        }
        Start-Sleep -Milliseconds 200
    }
} finally {
    Write-Host "`nStopping services..." -ForegroundColor Yellow
    foreach ($src in $logSources) {
        if ($src.Job) {
            Stop-Job   $src.Job -ErrorAction SilentlyContinue
            Remove-Job $src.Job -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Stopped. (UE Editor left running - close it manually)" -ForegroundColor Yellow
}
