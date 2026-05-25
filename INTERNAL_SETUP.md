# SimWorld Studio — Internal Setup Guide

This guide covers setup for both **Linux (shared server)** and **Windows (local dev)**.

---

## Linux — Shared Server

- **Server IP:** `132.239.95.132`
- **GPU:** Everyone uses GPU 0 (`--gpu 0`)

## Shared Resources (DO NOT MODIFY)

| Resource | Path |
|---|---|
| UE Engine | `/data/murray/ue/UE_5.3.2` |
| UE Project | `/data/murray/simworld_projects/SimWorld.uproject` |

## Port Assignments

Each person **must** use their assigned ports to avoid conflicts. Find your name below:

| User | Web UI | MCP | Cirrus HTTP | Cirrus WS | Cirrus SFU |
|------|--------|-----|-------------|-----------|------------|
| murray | 3002 | 55560 | 8685 | 8686 | 8989 |
| james | 3003 | 55779 | 8585 | 8586 | 8889 |
| (available) | 3004 | 55561 | 8687 | 8688 | 8990 |
| (available) | 3005 | 55562 | 8689 | 8690 | 8991 |
| (available) | 3006 | 55563 | 8691 | 8692 | 8992 |
| (available) | 3007 | 55564 | 8693 | 8694 | 8993 |
| (available) | 3008 | 55565 | 8695 | 8696 | 8994 |
| (available) | 3009 | 55566 | 8697 | 8698 | 8995 |
| (available) | 3010 | 55567 | 8699 | 8700 | 8996 |

Tell Murray which slot you're taking so the table stays up to date.

## Setup (One-Time)

### 1. Extract the code

You'll receive a zip file. Extract it to your home directory:

```bash
cd ~
unzip SimWorld-Studio.zip
cd SimWorld-Studio
```

### 2. Install

```bash
pip install ./packaging
npm install -g @anthropic-ai/claude-code
```

### 3. Authenticate with Claude

**Option A — API Key (recommended):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Get your key at https://console.anthropic.com

**Option B — Claude Code Login:**
```bash
claude
```

### 4. Build the Frontend (first time only)

```bash
cd ~/SimWorld-Studio/simworld_studio_workspace/web
npm install
npm run build
```

## Launch

Replace the port numbers with **your assigned ports** from the table above:

```bash
export UE_ROOT=/data/koe/UE_5.3.2
export UE_PROJECT_PATH=/data/koe/simworld_studio_projects

simworld-studio start \
  --data-dir ~/SimWorld-Studio-Dev/simworld_studio_workspace \
  --gpu 0 \
  --port 3004 \
  --mcp-port 55561 \
  --cirrus-http-port 8687 \
  --cirrus-ws-port 8688 \
  --cirrus-sfu-port 8990
```

**Example (slot 3, ports 3004/55561/8687/8688/8990):**

```bash
export UE_ROOT=/data/murray/ue/UE_5.3.2
export UE_PROJECT_PATH=/data/murray/simworld_projects

simworld-studio start \
  --data-dir ~/SimWorld-Studio-Dev/simworld_studio_workspace \
  --gpu 0 \
  --port 3004 \
  --mcp-port 55561 \
  --cirrus-http-port 8687 \
  --cirrus-ws-port 8688 \
  --cirrus-sfu-port 8990
```

## Access the UI

Open in your browser directly:

```
http://132.239.95.132:<YOUR_WEB_PORT>
```

For example, if your Web UI port is 3004:

```
http://132.239.95.132:3004
```

### Pixel Streaming (live UE viewport)

The Pixel Streaming panel in the UI connects to the Cirrus HTTP port. For it to work from your browser, you need to access the Cirrus port as well.

If you see **"WebSocket disconnected"** in the Pixel Streaming panel:
1. Wait ~60 seconds after launch for UE to fully initialize and connect to Cirrus
2. Make sure the Cirrus HTTP port is accessible — try opening `http://132.239.95.132:<YOUR_CIRRUS_HTTP>` directly
3. If the server firewall blocks the Cirrus port, use an SSH tunnel as a fallback:
   ```bash
   ssh -L <YOUR_WEB_PORT>:localhost:<YOUR_WEB_PORT> -L <YOUR_CIRRUS_HTTP>:localhost:<YOUR_CIRRUS_HTTP> <your_user>@132.239.95.132
   ```
   Then open `http://localhost:<YOUR_WEB_PORT>` instead.

## Troubleshooting

### Check if your ports are free

```bash
ss -tlnp | grep -E '<your_web_port>|<your_mcp_port>|<your_cirrus_http>'
```

### View logs

```bash
# UE log
tail -f ~/SimWorld-Studio/simworld_studio_workspace/logs/ue.log

# Cirrus (pixel streaming) log
tail -f ~/SimWorld-Studio/simworld_studio_workspace/logs/cirrus.log
```

### Common issues

| Issue | Fix |
|---|---|
| `ENOENT: web/dist/index.html` | Run `cd ~/SimWorld-Studio/simworld_studio_workspace/web && npm install && npm run build` |
| WebSocket disconnected | Wait ~60s for UE to load. Check Cirrus log: `tail -f ~/SimWorld-Studio/simworld_studio_workspace/logs/cirrus.log` — look for `streamer connected`. |
| Port already in use | Someone is using your port. Check with `ss -tlnp \| grep <port>`. Make sure you're using your assigned ports. |
| Cirrus says "already running" | Another user's Cirrus is on the same ports. Make sure you pass your unique `--cirrus-*-port` flags. |
| PixelStreaming not working | Already fixed in the shared project. If using a different `.uproject`, add `{"Name": "PixelStreaming", "Enabled": true}` to its Plugins list. |

## Stopping

Press `Ctrl+C` in the terminal where `simworld-studio start` is running. This stops UE, Cirrus, and the web server.

If processes linger:

```bash
# Find your processes
ps aux | grep $USER | grep -E 'UnrealEditor|cirrus|node.*server'

# Kill them
kill <pid>
```

---

## Windows — Local Development

### Prerequisites

- **Unreal Engine 5.3** installed via Epic Games Launcher
- **Node.js** (v18+): https://nodejs.org
- **Claude Code** or **Anthropic API Key**

### 1. Extract the code

Unzip `SimWorld-Studio.zip` to any directory, e.g. `C:\SimWorld-Studio`.

### 2. Install dependencies

```powershell
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Install pip package (for MCP server)
pip install ./packaging

# Install web dependencies
cd SimWorld-Studio\simworld_studio_workspace\web
npm install
```

### 3. Authenticate with Claude

**Option A — API Key (recommended):**
```powershell
set ANTHROPIC_API_KEY=sk-ant-...
```
Get your key at https://console.anthropic.com

**Option B — Claude Code Login:**
```powershell
claude
```

### 4. Build the Frontend (first time only)

```powershell
cd SimWorld-Studio\simworld_studio_workspace\web
npm install
npm run build
```

### 5. Configure the launch script

Edit `SimWorld-Studio.bat` at the top — set these three paths:

```bat
REM Path to UE installation root
set "UE_ROOT=C:\Program Files\Epic Games\UE_5.3"

REM Path to UE project file (.uproject)
set "UE_PROJECT=E:\UE\SimWorld\SimWorld.uproject"

REM Workspace directory
set "WORKSPACE=%~dp0simworld_studio_workspace"
```

### 6. Launch

Double-click `SimWorld-Studio.bat`, or from terminal:

```powershell
SimWorld-Studio.bat
```

With custom ports (same port scheme as Linux):

```powershell
SimWorld-Studio.bat --port 3002 --mcp-port 55560 --cirrus-http-port 8685 --cirrus-ws-port 8686 --cirrus-sfu-port 8989
```

The script will:
1. Start Cirrus signaling server (Pixel Streaming)
2. Launch UE Editor with MCP + Pixel Streaming
3. Wait for MCP port to be ready
4. Start the web server

### Access the UI

```
http://localhost:3002
```

(Replace `3002` with your `--port` value if changed.)

### Stopping

Close the command prompt window, then end any remaining processes in Task Manager:
- `UnrealEditor.exe`
- `node.exe` (Cirrus and Web Server)

Or from PowerShell:

```powershell
taskkill /im UnrealEditor.exe /f
taskkill /im node.exe /f
```

### Windows Troubleshooting

| Issue | Fix |
|---|---|
| `UnrealEditor.exe not found` | Edit `UE_ROOT` in `SimWorld-Studio.bat` to your UE install path |
| `node not found` | Install Node.js and restart your terminal |
| MCP port timeout | UE is slow to start — wait longer, or check if `UnrealEditor.exe` crashed in Task Manager |
| `ENOENT: web/dist/index.html` | Run `cd simworld_studio_workspace\web && npm install && npm run build` |
| Port already in use | Another process is using the port. Check with `netstat -ano | findstr <port>` |
