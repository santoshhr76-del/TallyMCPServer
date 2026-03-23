# Deploying to Google Cloud Run

Follow these steps **in order**. Each step is self-contained.

---

## Prerequisites checklist

- [ ] Google account with billing enabled at [console.cloud.google.com](https://console.cloud.google.com)
- [ ] `cloudflared tunnel --url http://localhost:9000` is running on your Tally machine and you have the `https://` URL
- [ ] `gcloud` CLI installed (see Step 0 below)

---

## Step 0 — Install gcloud CLI (if not already installed)

Download and install from: https://cloud.google.com/sdk/docs/install

**Windows:** Download the installer from the link above, run it, and restart your terminal.

Verify:
```
gcloud --version
```

---

## Step 1 — Create a GCP Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Name it something like `tallyprime-mcp`
4. **Copy the Project ID** (shown below the project name — it may look like `tallyprime-mcp-123456`)
5. Make sure **billing is enabled** on the project (Cloud Run requires billing, but has a generous free tier: 2 million requests/month free)

---

## Step 2 — Edit the deploy script

Open `deploy-gcp.sh` in any text editor and fill in the CONFIG section at the top:

```bash
GCP_PROJECT="tallyprime-mcp-123456"        # ← Your actual Project ID from Step 1
REGION="asia-south1"                        # ← Mumbai. Change if needed.
SERVICE_NAME="tallyprime-mcp"              # ← Leave as-is (or rename)
TALLY_URL="https://xyz.trycloudflare.com"  # ← Your cloudflared URL
MCP_API_KEY="paste-a-strong-random-secret" # ← Generate one (see below)
```

### Generate a strong API key

Run this in any terminal:
```
python -c "import secrets; print(secrets.token_hex(32))"
```
Paste the output as `MCP_API_KEY`.

---

## Step 3 — Run the deploy script

Open a terminal **in the `tallyprime-mcp` folder** and run:

```bash
bash deploy-gcp.sh
```

The script will:
1. Log you into Google Cloud (opens browser)
2. Enable Cloud Run, Cloud Build, and Container Registry APIs
3. Build your Docker image using Cloud Build (no Docker needed locally)
4. Push the image to Google Container Registry
5. Deploy to Cloud Run
6. Print your live service URL

The first deployment takes about **3–5 minutes**.

---

## Step 4 — Verify deployment

Once the script finishes, test the health endpoint:

```
https://your-service-url.run.app/health
```

You should see:
```json
{
  "status": "ok",
  "tally_url": "https://xyz.trycloudflare.com",
  "version": "0.1.0"
}
```

---

## Step 5 — Connect to Claude

Add this to your Claude Desktop config file:

**Windows path:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "tallyprime": {
      "url": "https://your-service-url.run.app/sse",
      "headers": {
        "Authorization": "Bearer your-mcp-api-key"
      }
    }
  }
}
```

Restart Claude Desktop. You should see TallyPrime tools appear.

---

## Updating the server after code changes

Just run the deploy script again:
```bash
bash deploy-gcp.sh
```
Cloud Run does a zero-downtime rolling update.

---

## Keeping cloudflared running

The Cloudflare tunnel must stay running on your Tally machine for the MCP server to reach TallyPrime. To keep it running permanently on Windows:

**Run as a Windows Service (recommended):**
```
cloudflared service install
net start cloudflared
```

Or just keep the terminal window open while working.

---

## Cost estimate

Google Cloud Run has a very generous free tier:
- **2 million requests/month** free
- **360,000 GB-seconds** of compute free
- You only pay if you exceed the free tier

For typical TallyPrime usage (a few hundred requests/day), **cost is $0**.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied` on gcloud | Run `gcloud auth login` again |
| `Billing account not found` | Enable billing at console.cloud.google.com/billing |
| Health check returns error | Check that cloudflared tunnel is running |
| `Image not found` during deploy | Re-run script; Cloud Build may have timed out |
| Claude can't connect | Verify the `/sse` URL and Authorization header in config |
