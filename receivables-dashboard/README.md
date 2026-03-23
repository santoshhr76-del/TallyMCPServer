# Receivables Dashboard — Multi-Agent Pipeline

An autonomous 4-agent system built with the **Claude Agent SDK** that connects to your live **TallyPrime** data and produces an interactive receivables dashboard — in one command.

---

## What it does

```
python main.py
```

…and in a few minutes you get `output/dashboard.html` — open it in any browser, no server needed.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR AGENT                           │
│  Coordinates 4 stages. Verifies each output before proceeding. │
│  Tools: Read, Bash, Agent                                       │
└────┬────────────────────────────────────────────────────────────┘
     │  delegates via "Agent" tool (Claude Agent SDK subagents)
     │
     ├── Stage 1 ──▶  RECEIVABLES AGENT
     │                 • Calls TallyPrime MCP: get_outstanding_receivables
     │                 • Output: output/receivables.json
     │                   (party-wise bill-wise data + aging buckets)
     │
     ├── Stage 2 ──▶  PARTY DETAILS AGENT
     │                 • Calls TallyPrime MCP: get_ledger (one per debtor)
     │                 • Output: output/party_details.json
     │                   (address, state, pin code, GSTIN, phone)
     │
     ├── Stage 3 ──▶  MAP AGENT
     │                 • Geocodes Indian pin codes via OpenStreetMap Nominatim
     │                 • Output: output/map_data.json + output/map.html
     │                   (interactive Leaflet.js map)
     │
     └── Stage 4 ──▶  DASHBOARD AGENT
                       • Reads all output files
                       • Output: output/dashboard.html
                         (full responsive dashboard)
```

### The 4 Agents

| # | Agent | Data source | Output file |
|---|---|---|---|
| 1 | **Receivables Agent** | TallyPrime MCP → `get_outstanding_receivables` | `receivables.json` |
| 2 | **Party Details Agent** | TallyPrime MCP → `get_ledger` (per party) | `party_details.json` |
| 3 | **Map Agent** | OpenStreetMap Nominatim (pin code geocoding) | `map_data.json`, `map.html` |
| 4 | **Dashboard Agent** | All output files | `dashboard.html` |

---

## Dashboard Preview

`output/dashboard.html` contains:

- **KPI Cards** — Total Outstanding, Party Count, Overdue >90d, 31–90d, Current
- **Aging Bar Chart** — visual breakdown by overdue bucket (Chart.js)
- **Top 5 Debtors** — quick-glance list of biggest outstanding balances
- **Party-wise Table** — all debtors, sortable by any column
- **Interactive Map** — pin-drop for each debtor, sized by outstanding amount, coloured by overdue severity (Leaflet.js)

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- TallyPrime running with Gateway enabled (default port 9000)
- Anthropic API key from [platform.claude.com](https://platform.claude.com/)

### 2. Install

```bash
cd receivables-dashboard

# Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install claude-agent-sdk python-dotenv

# Install the TallyPrime MCP server from the parent folder
pip install -e ..
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Run

```bash
# Local TallyPrime (default)
python main.py

# Remote TallyPrime (via Cloudflare Tunnel or other)
python main.py --tally-url https://xyz.trycloudflare.com

# If your debtors are under a different Tally group
python main.py --ledger-group "Trade Receivables"

# See tool calls for debugging
python main.py --verbose
```

### 5. Open the dashboard

```bash
# macOS
open output/dashboard.html

# Windows
start output/dashboard.html

# Linux
xdg-open output/dashboard.html
```

---

## Output Files

| File | Description |
|---|---|
| `output/receivables.json` | Raw outstanding receivables from TallyPrime (bill-wise, party-wise, aging buckets, grand total) |
| `output/party_details.json` | Enriched party data — address, state, pin code, GSTIN, phone, email, outstanding |
| `output/map_data.json` | Geocoded coordinates per party (lat/lng from Nominatim) |
| `output/map.html` | Standalone Leaflet.js map (can open independently) |
| `output/dashboard.html` | **Final dashboard** — open this in your browser |

---

## Project Structure

```
receivables-dashboard/
├── main.py                     # CLI entry point
├── pipeline/
│   ├── __init__.py
│   ├── orchestrator.py         # Main query() call, MCP wiring, stage gates
│   └── agents.py               # AgentDefinition for all 4 agents
├── utils/
│   ├── __init__.py
│   └── display.py              # Pretty-print SDK messages
├── output/                     # All generated files land here
├── .env.example
├── requirements.txt
└── README.md
```

---

## Key SDK Concepts Used

| Concept | Where | Purpose |
|---|---|---|
| `query()` | `orchestrator.py` | Main agentic loop |
| `ClaudeAgentOptions` | `orchestrator.py` | Configure tools, MCP, agents |
| `mcp_servers` | `orchestrator.py` | Register TallyPrime MCP server |
| `AgentDefinition` | `agents.py` | Define each specialist subagent |
| `agents={}` | `orchestrator.py` | Register subagents with orchestrator |
| `permission_mode="acceptEdits"` | `orchestrator.py` | Auto-approve file writes |
| `allowed_tools=["Agent"]` | `orchestrator.py` | Allow spawning subagents |

---

## Extending the Pipeline

### Add a new agent (e.g. email debtors)

1. Define in `pipeline/agents.py`:

```python
EMAIL_AGENT = AgentDefinition(
    description="Drafts overdue reminder emails for debtors with >60 day bills",
    prompt="Read output/receivables.json... draft emails...",
    tools=["Read", "Write"],
)
```

2. Register in `pipeline/orchestrator.py`:

```python
agents={
    ...
    "email-agent": EMAIL_AGENT,
},
```

3. Add Stage 5 instructions to `ORCHESTRATOR_SYSTEM_PROMPT`.

### Use a different TallyPrime instance per company

```bash
python main.py --tally-url https://company-a.trycloudflare.com
python main.py --tally-url https://company-b.trycloudflare.com
```

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `API key not found` | `ANTHROPIC_API_KEY` not set | Add to `.env` or export in shell |
| `Connection refused` at port 9000 | TallyPrime Gateway not running | Enable Gateway in TallyPrime → F12 → Configure |
| `Sundry Debtors: no data` | Wrong ledger group | Pass `--ledger-group "Your Group Name"` |
| Geocoding returns no results | Invalid/missing pin code in Tally | Update pin codes in TallyPrime ledger masters |

---

## Resources

- [Claude Agent SDK docs](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Subagents guide](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [MCP servers guide](https://platform.claude.com/docs/en/agent-sdk/mcp)
- [TallyPrime Gateway setup](https://help.tallysolutions.com/tdl-reference/tally-odbc/)
