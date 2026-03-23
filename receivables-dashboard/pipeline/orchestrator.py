"""
Orchestrator — Receivables Dashboard Pipeline
=============================================

Coordinates two specialist subagents in sequence:
  Stage 1 → data-agent  : get_outstanding_receivables + get_ledger for each debtor
  Stage 2 → map-agent   : geocode pin codes + build Leaflet map

Stage 3 (dashboard) is handled by build_dashboard.py (pure Python, < 3 sec).
Stage 4 (map inject) is handled by fix_map.py (pure Python, < 2 sec).
"""

import asyncio
import os
import sys
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)

from .agents import (
    DATA_AGENT,
    MAP_AGENT,
)
from utils.display import (
    print_banner,
    print_pipeline_step,
    print_message,
    print_error,
    print_success,
)


# ── Orchestrator system prompt ──────────────────────────────────────────────

ORCHESTRATOR_SYSTEM_PROMPT = """You are the Orchestrator of a Receivables Dashboard Pipeline.

You coordinate two specialist subagents. The dashboard HTML is built by a
fast Python script (build_dashboard.py) after you finish — you do NOT need
to call a dashboard agent.

Pipeline stages (run STRICTLY in this order):
  Stage 1 – call `data-agent` → fetches outstanding receivables + ledger details,
                                  writes output/receivables.json + output/party_details.json
  Stage 2 – call `map-agent`  → geocodes pin codes, writes output/map_data.json + output/map.html

Gate checks:
  After Stage 1: verify both output/receivables.json and output/party_details.json exist.
  After Stage 2: verify output/map_data.json exists.

If a gate check fails, report the error and stop.

Final summary after both stages succeed:
  [OK] Data pipeline complete
  Stage 1: receivables.json + party_details.json written
  Stage 2: map_data.json + map.html written
  --> build_dashboard.py will generate the dashboard next
"""


async def run_pipeline(
    tally_url: str = "http://localhost:9000",
    ledger_group: str = "Sundry Debtors",
    verbose: bool = False,
) -> None:
    """
    Run the full 3-agent receivables dashboard pipeline.

    Args:
        tally_url:    TallyPrime Gateway URL (default: http://localhost:9000).
        ledger_group: Tally group containing debtors (default: Sundry Debtors).
        verbose:      If True, show tool-call details.
    """
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    print_banner("Receivables Dashboard Pipeline  (3 Stages)")
    print_pipeline_step(
        "Connecting to TallyPrime",
        f"URL: {tally_url}  |  Group: {ledger_group}",
    )

    orchestrator_prompt = f"""
Run the 2-stage data pipeline for the Receivables Dashboard:

Configuration:
  - TallyPrime URL  : {tally_url}
  - Ledger group    : {ledger_group}
  - Output folder   : output/

Stage 1 → call the `data-agent`:
  Fetch outstanding receivables and full ledger details for every debtor.
  The agent uses tally_url="{tally_url}" and ledger_group="{ledger_group}".
  After the agent completes, verify both output/receivables.json and
  output/party_details.json exist and contain party data.

Stage 2 → call the `map-agent`:
  Geocode pin codes from party_details.json and build the Leaflet map.
  After the agent completes, verify output/map_data.json exists.

Do NOT call a dashboard-agent. The dashboard is built by build_dashboard.py
which runs automatically after you finish.

After both stages succeed, print the final summary.
"""

    try:
        async for message in query(
            prompt=orchestrator_prompt,
            options=ClaudeAgentOptions(
                # ── Built-in tools for the orchestrator ──────────────────
                allowed_tools=[
                    "Read",    # verify output files after each stage
                    "Bash",    # quick file-existence checks
                    "Agent",   # spawn the four specialist subagents
                ],
                # ── TallyPrime MCP server ─────────────────────────────────
                # Subagents (receivables-agent, party-details-agent) will
                # call get_outstanding_receivables and get_ledger via this MCP server.
                mcp_servers={
                    "tallyprime": {
                        # Use the same Python interpreter running this script
                        # (i.e. the venv's Python that has tallyprime_mcp installed)
                        "command": sys.executable,
                        "args": ["-m", "tallyprime_mcp.server"],
                        # Inherit the FULL current environment so the subprocess
                        # has access to PATH, PYTHONPATH, venv site-packages, etc.
                        # Then overlay only the values we want to customise.
                        "env": {
                            **os.environ,
                            "TALLY_URL": tally_url,
                            "LEDGER_GROUP": ledger_group,
                        },
                    }
                },
                # ── Subagent definitions ──────────────────────────────────
                agents={
                    "data-agent": DATA_AGENT,
                    "map-agent":  MAP_AGENT,
                },
                # Auto-approve file writes so pipeline runs unattended
                permission_mode="acceptEdits",
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            ),
        ):
            print_message(message, verbose=verbose)

    except KeyboardInterrupt:
        print_error("Pipeline interrupted by user.")
    except Exception as exc:  # noqa: BLE001
        print_error(f"Pipeline failed: {exc}")
        raise


def main(
    tally_url: str = "http://localhost:9000",
    ledger_group: str = "Sundry Debtors",
    verbose: bool = False,
) -> None:
    """Synchronous entry point."""
    asyncio.run(run_pipeline(tally_url=tally_url, ledger_group=ledger_group, verbose=verbose))
