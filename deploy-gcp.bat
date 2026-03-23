REM ═══════════════════════════════════════════════════════════════════════════
REM TallyPrime MCP Server — Google Cloud Run Deployment (Windows)
REM ═══════════════════════════════════════════════════════════════════════════
REM Usage:
REM   1. Fill in the CONFIG section below
REM   2. Double-click this file OR run from Command Prompt:
REM        deploy-gcp.bat
REM ═══════════════════════════════════════════════════════════════════════════

REM ── CONFIG — edit these before running ─────────────────────────────────────

REM Your GCP Project ID (find it at console.cloud.google.com)
set GCP_PROJECT=nifty-expanse-487907-h5

REM Cloud Run region — asia-south1 = Mumbai (good for India)
set REGION=asia-south1

REM Name for your Cloud Run service
set SERVICE_NAME=tallyprime-mcp

REM Your Cloudflare Tunnel URL (the https:// URL from cloudflared output)
set TALLY_URL=https://bruce-offset-absorption-weblogs.trycloudflare.com

REM A strong random secret to protect your MCP endpoint
REM Generate one by running:  python -c "import secrets; print(secrets.token_hex(32))"
set MCP_API_KEY=683c7073861d17e874043e5cb39436cd1a6e8e5a421fdf0e02e7cc46c2bd02dd

REM ── END CONFIG ─────────────────────────────────────────────────────────────

set IMAGE=gcr.io/%GCP_PROJECT%/%SERVICE_NAME%

echo.
echo ╔═══════════════════════════════════════════════════╗
echo ║   TallyPrime MCP -^> Google Cloud Run Deployer    ║
echo ╚═══════════════════════════════════════════════════╝
echo.
echo   Project  : %GCP_PROJECT%
echo   Region   : %REGION%
echo   Service  : %SERVICE_NAME%
echo   Image    : %IMAGE%
echo   Tally URL: %TALLY_URL%
echo.

REM ── Step 1: Login and set project ──────────────────────────────────────────
echo [1/6] Logging into Google Cloud...
call gcloud auth login
if %ERRORLEVEL% neq 0 ( echo ERROR: gcloud login failed & pause & exit /b 1 )

call gcloud config set project %GCP_PROJECT%
if %ERRORLEVEL% neq 0 ( echo ERROR: Could not set project & pause & exit /b 1 )
echo       Done.

REM ── Step 2: Enable APIs ────────────────────────────────────────────────────
echo.
echo [2/6] Enabling required GCP APIs ^(may take ~1 min first time^)...
call gcloud services enable run.googleapis.com containerregistry.googleapis.com cloudbuild.googleapis.com --project %GCP_PROJECT%
if %ERRORLEVEL% neq 0 ( echo ERROR: Could not enable APIs & pause & exit /b 1 )
echo       Done.

REM ── Step 3: Configure Docker auth ─────────────────────────────────────────
echo.
echo [3/6] Configuring Docker authentication for GCR...
call gcloud auth configure-docker --quiet
if %ERRORLEVEL% neq 0 ( echo ERROR: Docker auth failed & pause & exit /b 1 )
echo       Done.

REM ── Step 4: Build and push image ──────────────────────────────────────────
echo.
echo [4/6] Building Docker image with Cloud Build and pushing to GCR...
echo       ^(This takes 2-4 minutes. No local Docker needed.^)
call gcloud builds submit --tag %IMAGE% --project %GCP_PROJECT% --timeout=10m .
if %ERRORLEVEL% neq 0 ( echo ERROR: Cloud Build failed & pause & exit /b 1 )
echo       Done.

REM ── Step 5: Deploy to Cloud Run ───────────────────────────────────────────
echo.
echo [5/6] Deploying to Cloud Run in %REGION%...
call gcloud run deploy %SERVICE_NAME% --image %IMAGE% --platform managed --region %REGION% --port 8000 --allow-unauthenticated --set-env-vars "TALLY_URL=%TALLY_URL%,MCP_API_KEY=%MCP_API_KEY%,TALLY_TIMEOUT=120,MCP_PORT=8000" --memory 512Mi --cpu 1 --min-instances 0 --max-instances 5 --timeout 300 --project %GCP_PROJECT%
if %ERRORLEVEL% neq 0 ( echo ERROR: Cloud Run deploy failed & pause & exit /b 1 )
echo       Done.

REM ── Step 6: Get the service URL ───────────────────────────────────────────
echo.
echo [6/6] Fetching your live service URL...
for /f "delims=" %%i in ('gcloud run services describe %SERVICE_NAME% --platform managed --region %REGION% --project %GCP_PROJECT% --format "value(status.url)"') do set SERVICE_URL=%%i

echo.
echo ╔══════════════════════════════════════════════════════════════════╗
echo ║   SUCCESS - TallyPrime MCP is live on Google Cloud Run!        ║
echo ╠══════════════════════════════════════════════════════════════════╣
echo.
echo   Health check : https://tallyprime-mcp-mqup2h4wzq-el.a.run.app/health
echo   MCP SSE URL  : https://tallyprime-mcp-mqup2h4wzq-el.a.run.app/sse
echo.
echo   Add this to your Claude Desktop config:
echo   (%APPDATA%\Claude\claude_desktop_config.json)
echo.
echo   {
echo     "mcpServers": {
echo       "tallyprime": {
echo         "url": "%SERVICE_URL%/sse",
echo         "headers": {
echo           "Authorization": "Bearer %MCP_API_KEY%"
echo         }
echo       }
echo     }
echo   }
echo.
echo ╚══════════════════════════════════════════════════════════════════╝
echo.
pause
