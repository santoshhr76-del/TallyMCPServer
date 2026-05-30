@echo off
REM ───────────────────────────────────────────────────────────────────────
REM  TallyPrime MCP — One-time setup
REM
REM  Installs the Python package in editable mode and checks for
REM  cloudflared on PATH. Run this once after cloning the repo, or
REM  again after dependencies change.
REM ───────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0\.."

echo.
echo === [1/3]  Checking Python ===
python --version 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/windows/
    echo and tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

echo.
echo === [2/3]  Installing tallyprime-mcp (editable) ===
python -m pip install --upgrade pip
python -m pip install -e .
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo === [3/3]  Checking cloudflared ===
where cloudflared >nul 2>nul
if errorlevel 1 (
    echo [WARN] cloudflared not found on PATH.
    echo Install with one of:
    echo   winget install --id Cloudflare.cloudflared
    echo   choco install cloudflared
    echo Or download from:
    echo   https://github.com/cloudflare/cloudflared/releases/latest
) else (
    echo cloudflared OK:
    cloudflared --version
)

echo.
echo ───────────────────────────────────────────────────────────────────
echo  Setup complete. Next steps:
echo    1. Make sure TallyPrime is running with Gateway on port 9000.
echo    2. Edit .env and set ANTHROPIC_API_KEY (needed for /chat endpoint).
echo    3. Run  scripts\start-all.bat   to start the server + tunnel.
echo    4. (Optional) Run  scripts\install-autostart.bat   to launch on boot.
echo ───────────────────────────────────────────────────────────────────
echo.
pause
endlocal
