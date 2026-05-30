@echo off
setlocal
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TallyPrime MCP.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo [OK] Auto-start removed.
) else (
    echo [INFO] Auto-start was not installed.
)
echo.
pause
endlocal
