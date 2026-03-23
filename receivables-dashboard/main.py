#!/usr/bin/env python3
"""
Receivables Dashboard Pipeline — Entry Point
============================================

A 4-agent system built with the Claude Agent SDK that turns live TallyPrime
accounting data into an interactive web dashboard:

  Agent 1 – Fetch outstanding receivables      → output/receivables.json
  Agent 2 – Fetch debtor ledger details          → output/party_details.json
  Agent 3 – Geocode pin codes + build map        → output/map.html
  Agent 4 – Assemble web dashboard               → output/dashboard.html

Prerequisites:
  1. TallyPrime is running with the Gateway server enabled (port 9000 by default).
  2. The tallyprime_mcp package is installed in this Python environment.
     (pip install -e /path/to/TallyMCPServer  OR  pip install tallyprime-mcp)
  3. ANTHROPIC_API_KEY is set in .env or as an environment variable.

Usage:
  python main.py
  python main.py --tally-url http://localhost:9000
  python main.py --tally-url https://xyz.trycloudflare.com --verbose
  python main.py --ledger-group "Trade Receivables"
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

# Load .env BEFORE reading os.environ defaults below
load_dotenv()

from pipeline.orchestrator import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="receivables-dashboard",
        description="Multi-agent Receivables Dashboard (Claude Agent SDK + TallyPrime MCP)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local TallyPrime (default)
  python main.py

  # Remote TallyPrime via Cloudflare Tunnel
  python main.py --tally-url https://xyz.trycloudflare.com

  # Custom debtor group + verbose logging
  python main.py --ledger-group "Trade Receivables" --verbose
        """,
    )
    parser.add_argument(
        "--tally-url",
        default=os.environ.get("TALLY_URL", "http://localhost:9000"),
        metavar="URL",
        help="TallyPrime Gateway URL (default: TALLY_URL from .env, or http://localhost:9000)",
    )
    parser.add_argument(
        "--ledger-group",
        default=os.environ.get("LEDGER_GROUP", "Sundry Debtors"),
        metavar="GROUP",
        help="Tally ledger group containing debtors (default: LEDGER_GROUP from .env, or 'Sundry Debtors')",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Show tool-call details for debugging",
    )
    return parser.parse_args()


def run_build_dashboard() -> None:
    """
    Stage 3 (fast Python): regenerate the DATA CONSTANTS block in
    dashboard.html from receivables.json + party_details.json.
    Replaces the slow DASHBOARD_AGENT (~15-20 min) with a < 3 sec script.
    """
    import subprocess
    script_path = Path(__file__).parent / "build_dashboard.py"
    if not script_path.exists():
        print("[WARN] build_dashboard.py not found -- skipping dashboard build.")
        return
    print("\n[BUILD] Running build_dashboard.py -- injecting fresh data ...")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        encoding="utf-8",
        cwd=str(Path(__file__).parent),
    )
    if result.returncode == 0:
        print("[OK] Dashboard data refreshed.")
        if result.stdout.strip():
            print(result.stdout.strip())
    else:
        print("[ERROR] build_dashboard.py failed:")
        if result.stderr.strip():
            print(result.stderr.strip())


def sync_party_phones() -> None:
    """
    After Agent 1 writes party_details.json, sync LEDGERMOBILE numbers into
    party_phones.json.  Rules:
      - Party already has a non-empty entry in party_phones.json → keep it (manual override).
      - Party has empty entry in party_phones.json AND Tally has a mobile → fill from Tally.
      - Party not yet in party_phones.json AND Tally has a mobile → add it.
      - Party not in party_phones.json AND no Tally mobile → add with empty string.
    """
    import json
    from pathlib import Path

    details_path = Path(__file__).parent / "output" / "party_details.json"
    phones_path  = Path(__file__).parent / "party_phones.json"

    if not details_path.exists():
        return

    details = json.loads(details_path.read_text(encoding="utf-8"))
    phones  = json.loads(phones_path.read_text(encoding="utf-8")) if phones_path.exists() else {}

    updated = 0
    for d in details:
        name = d.get("party_name", "")
        raw  = str(d.get("phone", "")).strip()
        # Normalise: strip non-digits, prepend 91 for 10-digit numbers
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == 10:
            digits = "91" + digits

        existing = str(phones.get(name, "")).strip()
        if existing:
            continue  # non-empty manual entry — never overwrite
        if digits and len(digits) >= 10:
            phones[name] = digits
            updated += 1
        elif name not in phones:
            phones[name] = ""

    phones_path.write_text(
        json.dumps(phones, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if updated:
        print(f"[PHONES] {updated} mobile number(s) synced from Tally into party_phones.json")


def run_send_reminders() -> None:
    """
    Stage 6: Send outstanding payment reminder emails via Gmail SMTP.
    Runs send_reminders.py. Skipped if GMAIL_APP_PASSWORD is not set in .env.
    """
    import subprocess
    script_path = Path(__file__).parent / "send_reminders.py"
    if not script_path.exists():
        print("[WARN] send_reminders.py not found -- skipping reminders.")
        return

    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_password:
        print("[SKIP] GMAIL_APP_PASSWORD not set -- skipping reminder emails.")
        print("       Add it to .env to enable automated reminders.")
        return

    print("\n[EMAIL] Sending outstanding payment reminders...")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        encoding="utf-8",
        cwd=str(Path(__file__).parent),
    )
    if result.returncode == 0:
        if result.stdout.strip():
            print(result.stdout.strip())
    else:
        print("[ERROR] send_reminders.py failed:")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())


def run_fix_map() -> None:
    """
    Post-processing step: inject the inline Leaflet map into dashboard.html.
    Runs fix_map.py from the same directory as main.py.
    """
    import subprocess
    fix_map_path = Path(__file__).parent / "fix_map.py"
    if not fix_map_path.exists():
        print("[WARN] fix_map.py not found -- skipping map injection.")
        return
    dashboard_path = Path(__file__).parent / "output" / "dashboard.html"
    if not dashboard_path.exists():
        print("[WARN] output/dashboard.html not found -- skipping map injection.")
        return
    print("\n[MAP] Running fix_map.py -- injecting Leaflet map into dashboard...")
    result = subprocess.run(
        [sys.executable, str(fix_map_path)],
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        print("[OK] Map injected successfully.")
        if result.stdout.strip():
            print(result.stdout.strip())
    else:
        print("[ERROR] fix_map.py exited with errors:")
        if result.stderr.strip():
            print(result.stderr.strip())


def run_send_whatsapp() -> None:
    """
    Stage 7: Send outstanding payment reminders via WhatsApp (Twilio).
    Runs send_whatsapp.py. Skipped if TWILIO_ACCOUNT_SID is not set in .env.
    """
    import subprocess
    script_path = Path(__file__).parent / "send_whatsapp.py"
    if not script_path.exists():
        print("[WARN] send_whatsapp.py not found -- skipping WhatsApp reminders.")
        return

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    if not twilio_sid:
        print("[SKIP] TWILIO_ACCOUNT_SID not set -- skipping WhatsApp reminders.")
        print("       Add Twilio credentials to .env to enable WhatsApp reminders.")
        return

    print("\n[WHATSAPP] Sending outstanding payment reminders via WhatsApp...")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        encoding="utf-8",
        cwd=str(Path(__file__).parent),
    )
    if result.returncode == 0:
        if result.stdout.strip():
            print(result.stdout.strip())
    else:
        print("[ERROR] send_whatsapp.py failed:")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())


async def async_main() -> None:
    args = parse_args()
    await run_pipeline(
        tally_url=args.tally_url,
        ledger_group=args.ledger_group,
        verbose=args.verbose,
    )
    # Stage 3: fast Python dashboard builder (replaces DASHBOARD_AGENT)
    run_build_dashboard()
    # Stage 3b: sync Tally LEDGERMOBILE numbers into party_phones.json
    sync_party_phones()
    # Stage 4: inject Leaflet map into dashboard.html
    run_fix_map()

    # Stage 5: sync dashboard.html -> index.html so browser auto-refresh works
    import shutil
    output_dir = Path(__file__).parent / "output"
    src  = output_dir / "dashboard.html"
    dest = output_dir / "index.html"
    if src.exists():
        shutil.copy2(src, dest)
        print("[SYNC] index.html updated from dashboard.html")

    # Stage 6: send outstanding payment reminder emails
    run_send_reminders()

    # Stage 7: send WhatsApp payment reminders
    run_send_whatsapp()

    print("\n--> Open output/dashboard.html in your browser to view the dashboard.")


if __name__ == "__main__":
    asyncio.run(async_main())
