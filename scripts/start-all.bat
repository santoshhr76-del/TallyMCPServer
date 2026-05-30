@echo off
REM ───────────────────────────────────────────────────────────────────────
REM  Start the MCP server + the named Cloudflare tunnel together.
REM
REM  Public URL : https://tally.tallymcpclient.com  (stable — named tunnel)
REM  Local URL  : http://localhost:8000            (laptop browser only)
REM
REM  Each runs in its own minimised console window. Closing this script
REM  does NOT stop them — close each window or run shutdown-all.bat.
REM ───────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0\.."

echo Starting MCP server (port 8000) ...
start "TallyPrime MCP Server" /min cmd /c "scripts\start-mcp-server.bat"

REM Give uvicorn a couple of seconds to bind the port before tunnelling.
timeout /t 3 /nobreak >nul

echo Starting Cloudflare named tunnel ...
start "Cloudflare Tunnel" /min cmd /c "scripts\start-tunnel.bat"

echo.
echo ───────────────────────────────────────────────────────────────────
echo  Stack is starting in the background.
echo.
echo    PWA on phone   :  https://tally.tallymcpclient.com/app
echo    Health check   :  https://tally.tallymcpclient.com/health
echo    PWA on laptop  :  http://localhost:8000/app
echo.
echo    /sse endpoint  :  https://tally.tallymcpclient.com/sse
echo                      (use this URL in Claude Desktop config)
echo ───────────────────────────────────────────────────────────────────
echo.
echo Both windows are minimised in your taskbar.
echo Closing this window is safe — server + tunnel keep running.
echo.
pause
endlocal
