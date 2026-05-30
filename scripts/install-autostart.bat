@echo off
REM ───────────────────────────────────────────────────────────────────────
REM  Add a shortcut to start-all.bat in the Windows Startup folder so the
REM  MCP server and tunnel launch automatically when you log in.
REM  Re-run safely — it overwrites any existing shortcut of the same name.
REM ───────────────────────────────────────────────────────────────────────
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%~dp0start-all.bat"
set "SHORTCUT=%STARTUP%\TallyPrime MCP.lnk"

if not exist "%TARGET%" (
    echo [ERROR] start-all.bat not found at:
    echo   %TARGET%
    pause
    exit /b 1
)

powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Description = 'Start TallyPrime MCP server + Cloudflare tunnel'; $s.Save()"

if exist "%SHORTCUT%" (
    echo [OK] Auto-start installed:
    echo      %SHORTCUT%
    echo.
    echo The MCP server and Cloudflare tunnel will start on your next Windows login.
    echo To remove, run scripts\uninstall-autostart.bat
) else (
    echo [ERROR] Failed to create the Startup shortcut.
)
echo.
pause
endlocal
