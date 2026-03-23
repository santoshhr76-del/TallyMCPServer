# TallyPrime MCP Server

A **Model Context Protocol (MCP) server** that exposes TallyPrime accounting operations as AI tools. Works with Claude, Cursor, and any MCP-compatible AI client.

---

## Features

| Category | Tools |
|---|---|
| **Company** | `get_active_company` |
| **Ledgers & Groups** | `get_all_ledgers`, `get_ledger`, `create_ledger`, `get_all_groups` |
| **Vouchers** | `get_vouchers`, `create_sales_voucher`, `create_purchase_voucher`, `create_payment_voucher`, `create_receipt_voucher`, `create_journal_voucher` |
| **Reports** | `get_trial_balance`, `get_balance_sheet`, `get_profit_loss`, `get_stock_summary`, `get_daybook`, `get_outstanding_receivables` |

---

## Architecture

```
┌─────────────────────┐        HTTP/SSE (MCP)       ┌────────────────────────┐
│   AI Client         │ ◄────────────────────────── │  Cloud MCP Server       │
│ (Claude / Cursor)   │                              │  (this repo)            │
└─────────────────────┘                              └───────────┬────────────┘
                                                                 │  XML HTTP
                                                        ┌────────▼────────────┐
                                                        │  TallyPrime          │
                                                        │  Gateway Server      │
                                                        │  (port 9000)         │
                                                        └─────────────────────┘
```

> **Key challenge:** TallyPrime runs on a local Windows machine, but the MCP server is in the cloud.
> You need to expose TallyPrime's port 9000 to the internet securely. See [Connecting Tally to the Cloud](#-connecting-tally-to-the-cloud).

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/yourname/tallyprime-mcp
cd tallyprime-mcp
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set TALLY_URL, MCP_API_KEY
```

### 3. Run locally (stdio mode for Claude Desktop)

```bash
tallyprime-mcp
```

### 4. Run as HTTP/SSE server (cloud mode)

```bash
tallyprime-mcp-http
# or:
python -m tallyprime_mcp.server_http
```

Server starts at `http://0.0.0.0:8000`
- `GET /health` → health check
- `GET /sse` → MCP SSE stream (clients connect here)
- `POST /messages` → MCP message endpoint

---

## 🐳 Docker (Recommended for Cloud)

```bash
# Build
docker build -t tallyprime-mcp .

# Run
docker run -d \
  -p 8000:8000 \
  -e TALLY_URL=https://tally.yourdomain.com \
  -e MCP_API_KEY=your-strong-secret \
  tallyprime-mcp
```

---

## ☁️ Cloud Deployment

### Railway (easiest)

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables:
   - `TALLY_URL` → your Tally tunnel URL
   - `MCP_API_KEY` → strong random secret
4. Railway auto-detects the Dockerfile and deploys

### Render

1. New → Web Service → connect GitHub repo
2. Runtime: Docker
3. Add env vars same as above
4. Deploy

### AWS / GCP / Azure

Use the Dockerfile with ECS, Cloud Run, or App Service. Set the env vars in your cloud's secret manager.

---

## 🔌 Connecting Tally to the Cloud

TallyPrime's Gateway Server listens on `localhost:9000` by default. To let your cloud MCP server reach it, you need to expose that port securely.

### Option A: Cloudflare Tunnel (Recommended — Free)

1. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) on the Windows machine running Tally
2. Run:
   ```bash
   cloudflared tunnel --url http://localhost:9000
   ```
3. You'll get a URL like `https://random-name.trycloudflare.com`
4. Set `TALLY_URL=https://random-name.trycloudflare.com` on your cloud server

For a permanent URL, set up a named tunnel with your own domain.

### Option B: ngrok

```bash
ngrok http 9000
```
Use the HTTPS URL provided as `TALLY_URL`.

### Option C: VPN / Static IP

If your office has a static IP or VPN, open port 9000 in your firewall and set:
```
TALLY_URL=http://YOUR.OFFICE.IP:9000
```

### TallyPrime Gateway Settings

In TallyPrime, go to **F12 → Advanced Configuration** and ensure:
- **Enable ODBC Server**: Yes
- **Port**: 9000
- **Enable for**: Remote (if you want cloud access without a tunnel)

---

## Connecting AI Clients

### Claude Desktop (local stdio mode)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tallyprime": {
      "command": "tallyprime-mcp",
      "env": {
        "TALLY_URL": "http://localhost:9000"
      }
    }
  }
}
```

### Claude / Any MCP client (cloud SSE mode)

```json
{
  "mcpServers": {
    "tallyprime": {
      "url": "https://your-cloud-server.railway.app/sse",
      "headers": {
        "Authorization": "Bearer your-strong-secret"
      }
    }
  }
}
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TALLY_URL` | `http://localhost:9000` | TallyPrime Gateway URL |
| `TALLY_TIMEOUT` | `30` | HTTP timeout in seconds |
| `MCP_HOST` | `0.0.0.0` | Server bind host |
| `MCP_PORT` | `8000` | Server bind port |
| `MCP_API_KEY` | _(empty)_ | Bearer token for auth (leave blank to disable) |

---

## Tool Reference

### Date format
All dates use `YYYYMMDD` — e.g., April 1 2024 = `"20240401"`

### Example: Create a sales invoice
```
Tool: create_sales_voucher
Args:
  date: "20250201"
  party_ledger: "ABC Traders"
  sales_ledger: "Sales Accounts"
  amount: 50000
  tax_ledger: "GST @ 18%"
  tax_amount: 9000
  narration: "Invoice #INV-001 for consulting services"
```

### Example: Get P&L for FY 2024-25
```
Tool: get_profit_loss
Args:
  from_date: "20240401"
  to_date: "20250331"
```

### Example: Get Outstanding Receivables as of today
```
Tool: get_outstanding_receivables
Args: {}
```

### Example: Get Outstanding Receivables as of a specific date
```
Tool: get_outstanding_receivables
Args:
  as_of_date: "31-03-2026"
```

### Example: Get Outstanding Receivables for a specific customer
```
Tool: get_outstanding_receivables
Args:
  as_of_date: "16-03-2026"
  party_name: "Acme"
```

**Response structure:**
```json
{
  "as_of_date": "20260316",
  "total_outstanding": 1500000.00,
  "party_count": 12,
  "party_summary": [
    { "party": "Acme Traders Pvt Ltd", "outstanding": 500000.00 },
    ...
  ],
  "aging_summary": {
    "current_not_due":  300000.00,
    "overdue_1_30":     200000.00,
    "overdue_31_60":    150000.00,
    "overdue_61_90":    100000.00,
    "overdue_above_90": 750000.00
  },
  "bills": [
    {
      "party":       "Acme Traders Pvt Ltd",
      "bill_ref":    "INV-2025-0042",
      "bill_date":   "20250901",
      "due_date":    "20251001",
      "outstanding": 500000.00
    },
    ...
  ],
  "bill_count": 35
}
```

---

## Requirements

- Python 3.11+
- TallyPrime 3.x or later with Gateway Server enabled
- `mcp`, `httpx`, `uvicorn`, `starlette` (installed automatically)

---

## License

MIT
