# Web Data Processing Pipeline

A production-ready **multi-agent system** built with the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) that autonomously fetches, analyses, and reports on any web topic or URL.

---

## Architecture

```
User Input (topic / URL)
        │
        ▼
┌───────────────────────────────────────────┐
│           ORCHESTRATOR AGENT              │
│  Coordinates the pipeline in sequence.   │
│  Tools: Read, Bash, Agent                │
└──────┬───────────────────────────────────┘
       │ delegates via Agent tool
       │
       ├──▶ ① INGESTION AGENT
       │       • WebSearch + WebFetch raw sources
       │       • Cleans & saves → output/raw_data.json
       │
       ├──▶ ② ANALYSIS AGENT
       │       • Reads raw_data.json
       │       • Extracts themes, entities, insights
       │       • Saves → output/analysis.json
       │
       └──▶ ③ REPORTER AGENT
               • Reads both JSON files
               • Writes polished Markdown report
               • Saves → output/report.md
```

### The 4 Agents

| Agent | Role | Tools |
|---|---|---|
| **Orchestrator** | Coordinates pipeline stages in order | `Read`, `Bash`, `Agent` |
| **Ingestion Agent** | Fetches & validates web/API data | `WebSearch`, `WebFetch`, `Write`, `Bash` |
| **Analysis Agent** | Extracts themes, entities, stats, insights | `Read`, `Write`, `Bash` |
| **Reporter Agent** | Produces a polished Markdown report | `Read`, `Write`, `Bash` |

---

## Project Structure

```
web-data-pipeline/
├── main.py                   # CLI entry point
├── pipeline/
│   ├── __init__.py
│   ├── orchestrator.py       # Main query() call + agent wiring
│   └── agents.py             # AgentDefinition for each specialist
├── utils/
│   ├── __init__.py
│   └── display.py            # Pretty-print SDK messages
├── output/                   # Generated files land here
│   ├── raw_data.json         # Ingestion Agent output
│   ├── analysis.json         # Analysis Agent output
│   └── report.md             # Final report
├── .env.example              # API key template
├── requirements.txt
└── README.md
```

---

## Quick Start

### 1. Prerequisites

- Python 3.10 or higher
- An [Anthropic API key](https://platform.claude.com/)

### 2. Install dependencies

```bash
cd web-data-pipeline
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure your API key

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=your-key-here
```

Or export it directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Run the pipeline

```bash
# Research a topic
python main.py "AI chip market trends 2025"

# Process a specific URL
python main.py "https://example.com/article"

# Verbose mode (shows tool calls)
python main.py "renewable energy" --verbose
```

---

## Output Files

After a successful run, three files appear in the `output/` folder:

**`raw_data.json`** — Raw fetched content
```json
[
  {
    "source": "https://...",
    "title": "Page title",
    "fetched_at": "2025-03-16T09:00:00Z",
    "content": "Cleaned page text..."
  }
]
```

**`analysis.json`** — Structured insights
```json
{
  "topic": "AI chip market trends 2025",
  "source_count": 4,
  "key_themes": ["NVIDIA dominance", "edge AI", "..."],
  "key_entities": [{"name": "NVIDIA", "type": "org", "mentions": 12}],
  "statistics": [{"value": "$500B", "context": "projected market size"}],
  "sentiment": {"label": "positive", "reason": "..."},
  "actionable_insights": ["Insight 1", "Insight 2", "Insight 3"],
  "analysed_at": "2025-03-16T09:02:00Z"
}
```

**`report.md`** — Polished Markdown report with executive summary, tables, and source links.

---

## Extending the Pipeline

### Add a new specialist agent

1. Define it in `pipeline/agents.py`:

```python
MY_AGENT = AgentDefinition(
    description="What this agent does (shown to orchestrator)",
    prompt="Your agent's detailed instructions...",
    tools=["Read", "Write", "Bash"],
)
```

2. Register it in `pipeline/orchestrator.py`:

```python
agents={
    "ingestion-agent": INGESTION_AGENT,
    "analysis-agent":  ANALYSIS_AGENT,
    "reporter-agent":  REPORTER_AGENT,
    "my-agent":        MY_AGENT,          # ← add here
},
```

3. Update `ORCHESTRATOR_SYSTEM_PROMPT` to tell the orchestrator when and how to call it.

### Use a different model

Add `model` to `ClaudeAgentOptions` in `orchestrator.py`:

```python
options=ClaudeAgentOptions(
    model="claude-opus-4-6",   # default is claude-sonnet-4-6
    ...
)
```

### Connect an MCP server (e.g. a database)

```python
options=ClaudeAgentOptions(
    mcp_servers={
        "postgres": {
            "command": "npx",
            "args": ["@modelcontextprotocol/server-postgres", "postgresql://localhost/mydb"]
        }
    },
    ...
)
```

### Use Amazon Bedrock or Google Vertex AI

```bash
# Bedrock
export CLAUDE_CODE_USE_BEDROCK=1

# Vertex AI
export CLAUDE_CODE_USE_VERTEX=1
```

---

## Key SDK Concepts Used

| Concept | Where used | Docs |
|---|---|---|
| `query()` | `orchestrator.py` | [Python reference](https://platform.claude.com/docs/en/agent-sdk/python) |
| `ClaudeAgentOptions` | `orchestrator.py` | [Options](https://platform.claude.com/docs/en/agent-sdk/python#claude-agent-options) |
| `AgentDefinition` | `agents.py` | [Subagents](https://platform.claude.com/docs/en/agent-sdk/subagents) |
| `permission_mode` | `orchestrator.py` | [Permissions](https://platform.claude.com/docs/en/agent-sdk/permissions) |
| Message types | `display.py` | [Messages](https://platform.claude.com/docs/en/agent-sdk/python#messages) |

---

## Troubleshooting

**`API key not found`** — Make sure `ANTHROPIC_API_KEY` is set in `.env` or your shell.

**`claude_agent_sdk not found`** — Run `pip install -r requirements.txt` inside your virtual environment.

**Pipeline stops after ingestion** — The Analysis Agent checks for `output/raw_data.json`. If ingestion failed silently, re-run with `--verbose` to see tool-call details.

**Rate limit errors** — The SDK handles retries automatically, but for large topics you may hit rate limits. Try a more specific topic to reduce the number of web fetches.

---

## Resources

- [Claude Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Subagents Guide](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [SDK Demo Agents (GitHub)](https://github.com/anthropics/claude-agent-sdk-demos)
- [Python SDK Reference](https://platform.claude.com/docs/en/agent-sdk/python)
