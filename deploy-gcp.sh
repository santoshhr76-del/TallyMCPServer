#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TallyPrime MCP Server — Google Cloud Run Deployment Script
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   1. Fill in the CONFIG section below
#   2. Run:  bash deploy-gcp.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── CONFIG — edit these before running ────────────────────────────────────────

# Your GCP Project ID (find it at console.cloud.google.com)
GCP_PROJECT="nifty-expanse-487907-h5"

# Cloud Run region — asia-south1 = Mumbai (good for India)
# Other options: us-central1, europe-west1, asia-east1
REGION="asia-south1"

# Name for your Cloud Run service
SERVICE_NAME="tallyprime-mcp"

# Your Cloudflare Tunnel URL (the https:// URL from cloudflared output)
TALLY_URL="https://moral-consultants-programming-varieties.trycloudflare.com"

# A strong random secret to protect your MCP endpoint
# Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"
MCP_API_KEY="change-me-to-a-strong-random-secret"

# ── END CONFIG ─────────────────────────────────────────────────────────────────

IMAGE="gcr.io/${GCP_PROJECT}/${SERVICE_NAME}"

echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║   TallyPrime MCP → Google Cloud Run Deployer     ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""
echo "  Project  : $GCP_PROJECT"
echo "  Region   : $REGION"
echo "  Service  : $SERVICE_NAME"
echo "  Image    : $IMAGE"
echo "  Tally URL: $TALLY_URL"
echo ""

# ── Step 1: Auth check ────────────────────────────────────────────────────────
echo "▶ Step 1/6 — Checking gcloud authentication..."
if ! gcloud auth print-access-token &>/dev/null; then
  echo "  Not logged in. Running gcloud auth login..."
  gcloud auth login
fi
gcloud config set project "$GCP_PROJECT"
echo "  ✓ Authenticated as $(gcloud config get-value account)"

# ── Step 2: Enable APIs ───────────────────────────────────────────────────────
echo ""
echo "▶ Step 2/6 — Enabling required GCP APIs (this may take ~1 min first time)..."
gcloud services enable \
  run.googleapis.com \
  containerregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --project "$GCP_PROJECT"
echo "  ✓ APIs enabled"

# ── Step 3: Configure Docker for GCR ─────────────────────────────────────────
echo ""
echo "▶ Step 3/6 — Configuring Docker authentication for GCR..."
gcloud auth configure-docker --quiet
echo "  ✓ Docker configured"

# ── Step 4: Build & push image ────────────────────────────────────────────────
echo ""
echo "▶ Step 4/6 — Building Docker image and pushing to Google Container Registry..."
echo "  (Using Cloud Build — no local Docker required)"
gcloud builds submit \
  --tag "$IMAGE" \
  --project "$GCP_PROJECT" \
  --timeout=10m \
  .
echo "  ✓ Image pushed: $IMAGE"

# ── Step 5: Deploy to Cloud Run ───────────────────────────────────────────────
echo ""
echo "▶ Step 5/6 — Deploying to Cloud Run in $REGION..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --port 8000 \
  --allow-unauthenticated \
  --set-env-vars "TALLY_URL=${TALLY_URL},MCP_API_KEY=${MCP_API_KEY},TALLY_TIMEOUT=30,MCP_PORT=8000" \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 60 \
  --project "$GCP_PROJECT"
echo "  ✓ Deployment complete"

# ── Step 6: Print results ─────────────────────────────────────────────────────
echo ""
echo "▶ Step 6/6 — Fetching service URL..."
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed \
  --region "$REGION" \
  --project "$GCP_PROJECT" \
  --format "value(status.url)")

echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║   ✅  DEPLOYMENT SUCCESSFUL                                   ║"
echo "╠═══════════════════════════════════════════════════════════════╣"
echo "║                                                               ║"
echo "  Service URL : $SERVICE_URL"
echo "  Health check: $SERVICE_URL/health"
echo "  MCP SSE URL : $SERVICE_URL/sse"
echo ""
echo "  Add to your Claude / MCP client config:"
echo ""
echo '  {'
echo '    "mcpServers": {'
echo '      "tallyprime": {'
echo "        \"url\": \"${SERVICE_URL}/sse\","
echo '        "headers": {'
echo "          \"Authorization\": \"Bearer ${MCP_API_KEY}\""
echo '        }'
echo '      }'
echo '    }'
echo '  }'
echo ""
echo "╚═══════════════════════════════════════════════════════════════╝"
