@echo off
REM ───────────────────────────────────────────────────────────────────────
REM  Start the named Cloudflare tunnel that fronts the MCP server.
REM
REM  Public URL :  https://tally.tallymcpclient.com
REM  Forwards to:  http://localhost:8000   (the MCP HTTP server)
REM
REM  ── ONE-TIME SETUP (do once per laptop) ──────────────────────────────
REM  1. cloudflared tunnel login
REM  2. cloudflared tunnel create tally-mcp
REM     (or reuse an existing tunnel; whatever name you used in config.yml)
REM  3. Copy cloudflared\config.yml.example to:
REM        %USERPROFILE%\.cloudflared\config.yml
REM     and fill in the tunnel UUID + credentials path.
REM  4. cloudflared tunnel route dns tally-mcp tally.tallymcpclient.com
REM  ─────────────────────────────────────────────────────────────────────
REM
REM  TUNNEL_NAME defaults to "tally-mcp". Override with first arg, e.g.:
REM     scripts\start-tunnel.bat my-tunnel-name
REM ───────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0\.."
title TallyPrime Cloudflare Tunnel

set "TUNNEL_NAME=%~1"
if "%TUNNEL_NAME%"=="" set "TUNNEL_NAME=tally-mcp"

del /q tunnel.log 2>nul

where cloudflared >nul 2>nul
if errorlevel 1 (
    echo [ERROR] cloudflared is not on PATH.
    echo Install with:  winget install --id Cloudflare.cloudflared
    pause
    exit /b 1
)

echo ───────────────────────────────────────────────────────────────────
echo  Cloudflared NAMED tunnel
echo    Tunnel name : %TUNNEL_NAME%
echo    Public URL  : https://tally.tallymcpclient.com
echo    Forwards to : http://localhost:8000   (MCP server)
echo  Logging to    : %CD%\tunnel.log
echo ───────────────────────────────────────────────────────────────────
echo.

cloudflared tunnel --no-autoupdate --logfile tunnel.log --loglevel info ^
    run %TUNNEL_NAME%

set RC=%errorlevel%
if not "%RC%"=="0" (
    echo.
    echo [ERROR] cloudflared exited with errorlevel %RC%.
    echo Common causes:
    echo   * Tunnel "%TUNNEL_NAME%" doesn't exist — run cloudflared tunnel create %TUNNEL_NAME%
    echo   * config.yml missing or has wrong tunnel UUID
    echo   * DNS route not added — cloudflared tunnel route dns %TUNNEL_NAME% tally.tallymcpclient.com
    echo   * Not logged in — run cloudflared tunnel login
    pause
)
endlocal
