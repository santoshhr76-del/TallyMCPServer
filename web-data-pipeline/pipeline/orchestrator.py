"""
Orchestrator for the Web Data Processing Pipeline.

The Orchestrator is the top-level agent that:
  1. Receives the user's topic / URL.
  2. Delegates to the Ingestion Agent  → fetches and saves raw_data.json
  3. Delegates to the Analysis Agent   → reads raw data, saves analysis.json
  4. Delegates to the Reporter Agent   → reads analysis, writes report.md
  5. Returns a summary of what was produced.

The orchestrator itself runs as the main `query()` call; the three specialist
agents are registered as subagents and are invoked via the built-in "Agent" tool.
"""

import asyncio
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)

from .agents import INGESTION_AGENT, ANALYSIS_AGENT, REPORTER_AGENT
from ..utils.display import (
    print_banner,
    print_pipeline_step,
    print_message,
    print_error,
    print_success,
)


# ── Orchestrator system prompt ──────────────────────────────────────────────

ORCHESTRATOR_SYSTEM_PROMPT = """You are the Orchestrator of a 4-stage Web Data Processing Pipeline.

Your role is to coordinate three specialist subagents in strict sequence to
research a topic, analyse the findings, and produce a report. You do NOT do
the actual work yourself — you delegate every step to the appropriate subagent.

Pipeline stages:
  Stage 1 – Ingestion  : call the `ingestion-agent`  with the user's topic/URL
  Stage 2 – Analysis   : call the `analysis-agent`   (after ingestion is done)
  Stage 3 – Reporting  : call the `reporter-agent`   (after analysis is done)

Rules:
- Always run stages in order: Ingestion → Analysis → Reporter.
- Do NOT proceed to Analysis until Ingestion has written output/raw_data.json.
- Do NOT proceed to Reporting until Analysis has written output/analysis.json.
- After all three stages complete, print a final summary:
    ✅ Pipeline complete!
    📄 raw_data.json — <X> sources fetched
    📊 analysis.json — <Y> key themes, <Z> insights
    📝 report.md     — saved to output/report.md
- If any stage fails, log the error and stop the pipeline with a clear message.
"""


async def run_pipeline(topic: str, verbose: bool = False) -> None:
    """
    Run the full 4-agent data processing pipeline for the given topic or URL.

    Args:
        topic:    The research topic or URL to process.
        verbose:  If True, print tool-call details for debugging.
    """
    # Ensure the output directory exists
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    print_banner("Web Data Processing Pipeline")
    print_pipeline_step(
        "Initialising orchestrator",
        f'Topic: "{topic}"',
    )

    orchestrator_prompt = f"""
Please run the full 3-stage pipeline for the following topic:

  TOPIC: {topic}

Stage 1 — call the ingestion-agent to fetch data about this topic and save
           the results to output/raw_data.json.

Stage 2 — once ingestion is complete, call the analysis-agent to analyse
           output/raw_data.json and save the structured findings to
           output/analysis.json.

Stage 3 — once analysis is complete, call the reporter-agent to read both
           output files and produce a polished Markdown report at
           output/report.md.

After all stages succeed, print the final summary as described in your
instructions.
"""

    try:
        async for message in query(
            prompt=orchestrator_prompt,
            options=ClaudeAgentOptions(
                # Tools the orchestrator itself can use
                allowed_tools=[
                    "Read",    # read output files to verify stages completed
                    "Bash",    # check file existence, timestamps
                    "Agent",   # spawn subagents — REQUIRED for multi-agent
                ],
                # Register the three specialist subagents
                agents={
                    "ingestion-agent": INGESTION_AGENT,
                    "analysis-agent":  ANALYSIS_AGENT,
                    "reporter-agent":  REPORTER_AGENT,
                },
                # Auto-approve file writes so pipeline runs unattended
                permission_mode="acceptEdits",
                # Orchestrator identity
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            ),
        ):
            print_message(message, verbose=verbose)

    except KeyboardInterrupt:
        print_error("Pipeline interrupted by user.")
    except Exception as exc:  # noqa: BLE001
        print_error(f"Pipeline failed: {exc}")
        raise


# ── Convenience wrapper for direct module execution ────────────────────────

async def _main_async(topic: str, verbose: bool = False) -> None:
    await run_pipeline(topic, verbose=verbose)


def main(topic: str, verbose: bool = False) -> None:
    """Synchronous entry point (wraps the async pipeline)."""
    asyncio.run(_main_async(topic, verbose=verbose))
