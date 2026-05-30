@echo off
REM ───────────────────────────────────────────────────────────────────────
REM  Start the TallyPrime MCP HTTP/SSE server on port 8000.
REM  Reads .env from the repo root automatically (via python-dotenv inside
REM  the package). Press Ctrl+C in this window to stop the server.
REM ───────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0\.."
title TallyPrime MCP Server

echo ───────────────────────────────────────────────────────────────────
echo  TallyPrime MCP Server  (port 8000)
echo  Repo: %CD%
echo ───────────────────────────────────────────────────────────────────
echo.

python -m tallyprime_mcp.server_http
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo [ERROR] Server exited with errorlevel %RC%.
    echo Check above for traceback. Common causes:
    echo   * tallyprime-mcp not installed yet — run scripts\setup-once.bat
    echo   * Port 8000 already in use — change MCP_PORT in .env
    echo   * ANTHROPIC_API_KEY missing — only needed for /chat
    pause
)
endlocal
