@echo off
REM Route the DNS record tally.tallymcpclient.com to the tally-mcp tunnel.
REM Hostname is hardcoded here so it can't get mangled by chat copy-paste.
setlocal
echo Routing DNS:  tally.tallymcpclient.com  -^>  tunnel "tally-mcp"
echo.
cloudflared tunnel route dns --overwrite-dns tally-mcp tally.tallymcpclient.com
echo.
pause
endlocal
