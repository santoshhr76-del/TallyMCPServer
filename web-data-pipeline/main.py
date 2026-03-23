#!/usr/bin/env python3
"""
Web Data Processing Pipeline — Entry Point
==========================================

A 4-agent system built with the Claude Agent SDK that:
  1. Orchestrator  — coordinates the full pipeline
  2. Ingestion Agent  — fetches & validates web / API data
  3. Analysis Agent   — extracts themes, entities, and insights
  4. Reporter Agent   — produces a polished Markdown report

Usage:
    python main.py "your topic or URL here"
    python main.py "latest developments in quantum computing"
    python main.py "https://example.com/some-article" --verbose
"""

import argparse
import asyncio
import sys
from pathlib import Path

# ── Ensure the package root is on sys.path when run directly ───────────────
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

# Load ANTHROPIC_API_KEY from .env if present
load_dotenv()

from pipeline.orchestrator import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="web-data-pipeline",
        description="Multi-agent web data processing pipeline (Claude Agent SDK)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py "AI chip market 2025"
  python main.py "https://example.com/article" --verbose
  python main.py "renewable energy trends" -v
        """,
    )
    parser.add_argument(
        "topic",
        type=str,
        help="Topic to research OR a URL to process",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Show detailed tool-call output for debugging",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    await run_pipeline(topic=args.topic, verbose=args.verbose)


if __name__ == "__main__":
    asyncio.run(async_main())
