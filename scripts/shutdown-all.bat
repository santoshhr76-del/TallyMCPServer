@echo off
REM Stop the MCP server and the cloudflared tunnel started by start-all.bat.
setlocal

echo Stopping cloudflared ...
taskkill /im cloudflared.exe /f >nul 2>nul
if errorlevel 1 (echo   (cloudflared was not running)) else (echo   stopped.)

echo Stopping MCP server window ...
taskkill /fi "WINDOWTITLE eq TallyPrime MCP Server*" /t /f >nul 2>nul
if errorlevel 1 (echo   (MCP server window not found)) else (echo   stopped.)

echo Stopping tunnel window ...
taskkill /fi "WINDOWTITLE eq TallyPrime Cloudflare Tunnel*" /t /f >nul 2>nul
if errorlevel 1 (echo   (tunnel window not found)) else (echo   stopped.)

echo.
echo Done.
pause
endlocal
