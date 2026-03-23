#!/usr/bin/env python3
"""
send_whatsapp.py — Outstanding Payment Reminder via WhatsApp (Twilio)
=====================================================================
Sends a formatted WhatsApp message per party showing outstanding bills.

Setup (one-time):
  1. Create a free Twilio account at twilio.com
  2. In Twilio Console → Messaging → Try it out → Send a WhatsApp message
     → note your sandbox number (e.g. +14155238886)
  3. Each recipient must join the sandbox first by sending:
       "join <your-sandbox-keyword>"  to  +14155238886
     (Only required for sandbox. Production numbers skip this step.)
  4. Add to .env:
       TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
       TWILIO_AUTH_TOKEN=your_auth_token
       TWILIO_WHATSAPP_FROM=+14155238886   # sandbox number, or your registered number

  Phone numbers in party_phones.json must include country code, digits only:
       "MANOJ KIRANA SEC 14": "919876543210"   ← India: 91 + 10-digit number

Run standalone:
  python send_whatsapp.py

Run with --dry-run to preview messages without sending:
  python send_whatsapp.py --dry-run
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 output on Windows (avoids charmap errors with ₹, —, → etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
OUTPUT_DIR      = BASE_DIR / "output"
RECEIVABLES_F   = OUTPUT_DIR / "receivables.json"
PARTY_DETAILS_F = OUTPUT_DIR / "party_details.json"
PARTY_PHONES_F  = BASE_DIR  / "party_phones.json"

COMPANY_NAME    = "Surabhi Enterprises, Udaipur"
COMPANY_PHONE   = os.environ.get("COMPANY_PHONE", "+91-XXXXX-XXXXX")

TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID",  "")
TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN",    "")
TWILIO_FROM     = os.environ.get("TWILIO_WHATSAPP_FROM", "+14155238886")
UPI_VPA         = os.environ.get("UPI_VPA", "surabhi@upi")

# WhatsApp message hard limit is 4096 chars; stay well below it
WA_MSG_LIMIT    = 3800


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_inr(amount: float) -> str:
    """Format amount as Indian Rupees (compact, no decimals for WhatsApp)."""
    amount = round(amount)
    s = str(int(amount))
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    groups = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return f"₹{','.join(groups)},{last3}"


def overdue_emoji(days: int) -> str:
    if days == 0:   return "🟢"
    if days <= 30:  return "🟡"
    if days <= 60:  return "🟠"
    if days <= 90:  return "🔴"
    return "🚨"


def _upi_qr_url(amount: float) -> str:
    """Return a publicly accessible QR code image URL for UPI payment."""
    upi = (
        f"upi://pay?pa={UPI_VPA}"
        f"&pn=Surabhi+Enterprises+Udaipur"
        f"&am={amount:.2f}&cu=INR"
        f"&tn=Outstanding+Payment"
    )
    return (
        "https://api.qrserver.com/v1/create-qr-code/"
        f"?size=256x256&data={urllib.parse.quote(upi, safe='')}"
    )


def normalise_phone(raw: str) -> str:
    """Strip spaces/dashes/+ to digits only.
    Auto-prepends 91 (India country code) if exactly 10 digits are provided."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return digits


# ── WhatsApp Message Renderer ─────────────────────────────────────────────────
def render_wa_message(party_name: str, bills: list,
                      total_outstanding: float, today_str: str) -> str:
    """
    Render a WhatsApp-formatted message.
    Uses *bold*, _italic_, and plain-text tables (monospace not reliable on all clients).
    Keeps total length under WA_MSG_LIMIT.
    """
    max_days   = max((b["days_overdue"] for b in bills), default=0)
    oldest_due = max(bills, key=lambda b: b["days_overdue"])["due_date"]
    sorted_bills = sorted(bills, key=lambda b: b["days_overdue"], reverse=True)

    urgency = overdue_emoji(max_days)

    lines = [
        f"*{urgency} Payment Reminder — {COMPANY_NAME}*",
        f"_Date: {today_str}_",
        "",
        f"Dear *{party_name}*,",
        "",
        "This is a reminder for outstanding dues on your account with us.",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 *Total Outstanding: {fmt_inr(total_outstanding)}*",
        f"📆 Oldest Due: {oldest_due}",
        f"{urgency} Max Overdue: {max_days} days  |  Bills: {len(bills)}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "*Outstanding Bills:*",
    ]

    # Bill rows — compact format
    for b in sorted_bills:
        em   = overdue_emoji(b["days_overdue"])
        days = f"{b['days_overdue']}d" if b["days_overdue"] > 0 else "current"
        lines.append(
            f"{em} *{b['bill_ref']}*  |  Due: {b['due_date']}  |  "
            f"{days}  |  *{fmt_inr(b['outstanding'])}*"
        )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"*TOTAL: {fmt_inr(total_outstanding)}*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "Kindly arrange payment at the earliest. If payment has already "
        "been made, please ignore this message.",
        "",
        f"_Thank you,_",
        f"_{COMPANY_NAME}_",
        f"_{COMPANY_PHONE}_",
    ]

    msg = "\n".join(lines)

    # Safety truncation (should never trigger for normal bill counts)
    if len(msg) > WA_MSG_LIMIT:
        msg = msg[:WA_MSG_LIMIT - 60] + "\n\n_... (message truncated)_"

    return msg


# ── Sender ────────────────────────────────────────────────────────────────────
def send_whatsapp(to_number: str, body: str, media_url: str = "") -> None:
    """Send a WhatsApp message via Twilio. to_number is digits only (e.g. 919876543210).
    If media_url is provided, the QR code image is sent as a media attachment."""
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    kwargs = dict(
        from_=f"whatsapp:+{TWILIO_FROM.lstrip('+')}",
        to=f"whatsapp:+{to_number}",
        body=body,
    )
    if media_url:
        kwargs["media_url"] = [media_url]
    client.messages.create(**kwargs)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Send WhatsApp payment reminders via Twilio")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print messages to console without sending")
    args = parser.parse_args()

    if not RECEIVABLES_F.exists():
        print("[ERROR] output/receivables.json not found. Run the pipeline first.")
        sys.exit(1)

    recv    = json.loads(RECEIVABLES_F.read_text(encoding="utf-8"))
    details = json.loads(PARTY_DETAILS_F.read_text(encoding="utf-8")) if PARTY_DETAILS_F.exists() else []

    # Step 1: load phone numbers from party_details.json (Tally "Primary mobile" field)
    # Same pattern as send_reminders.py for emails — Tally is the base source.
    phone_map: dict[str, str] = {}
    for d in details:
        raw = str(d.get("phone", "")).strip()
        if raw:
            digits = normalise_phone(raw)
            if len(digits) >= 10:
                phone_map[d["party_name"]] = digits

    # Step 2: party_phones.json overrides — corrections and additions only.
    # Non-empty entry → override/add. Empty string → no action, Tally data flows through.
    if PARTY_PHONES_F.exists():
        for party_name, raw_phone in json.loads(PARTY_PHONES_F.read_text(encoding="utf-8")).items():
            raw = str(raw_phone).strip()
            if raw:
                digits = normalise_phone(raw)
                if len(digits) >= 10:
                    phone_map[party_name] = digits

    today = datetime.date.today()
    try:
        today_str = today.strftime("%#d %B %Y")  # Windows
    except ValueError:
        today_str = today.strftime("%-d %B %Y")  # Linux/Mac

    bills_by_party = {p["party"]: p for p in recv.get("bills_by_party", [])}

    parties_to_notify = [
        (name, phone_map[name])
        for name in phone_map
        if name in bills_by_party
    ]

    if not parties_to_notify:
        return  # No phones configured — skip silently

    # ── Dry run ────────────────────────────────────────────────────────────
    if args.dry_run:
        for party_name, phone in parties_to_notify:
            pd  = bills_by_party[party_name]
            msg = render_wa_message(party_name, pd["bills"], pd["outstanding"], today_str)
            qr  = _upi_qr_url(pd["outstanding"])
            print(f"\n{'='*60}")
            print(f"TO: whatsapp:+{phone}  ({party_name})")
            print(f"QR: {qr}")
            print(f"CHARS: {len(msg)}")
            print(f"{'─'*60}")
            print(msg)
        print(f"\n[DRY-RUN] {len(parties_to_notify)} message(s) rendered, none sent.")
        return

    # ── Real send ──────────────────────────────────────────────────────────
    if not TWILIO_SID or not TWILIO_TOKEN:
        print("[ERROR] TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in .env")
        sys.exit(1)

    try:
        from twilio.rest import Client  # noqa: F401 — validate import early
    except ImportError:
        print("[ERROR] twilio package not installed.")
        print("        Run:  pip install twilio")
        sys.exit(1)

    sent = 0
    failed = 0

    print(f"\n[WHATSAPP] Sending {len(parties_to_notify)} reminder(s) via Twilio...")
    for party_name, phone in parties_to_notify:
        pd       = bills_by_party[party_name]
        max_days = max((b["days_overdue"] for b in pd["bills"]), default=0)
        msg      = render_wa_message(party_name, pd["bills"], pd["outstanding"], today_str)
        qr_url   = _upi_qr_url(pd["outstanding"])
        try:
            send_whatsapp(phone, msg, media_url=qr_url)
            print(f"  [SENT]  {party_name} -> +{phone}  "
                  f"(Rs {pd['outstanding']:,.0f}, max {max_days}d overdue)")
            sent += 1
        except Exception as e:
            print(f"  [FAIL]  {party_name} -> +{phone}  ERROR: {e}")
            failed += 1

    print(f"\n[DONE] Sent: {sent}  |  Failed: {failed}")


if __name__ == "__main__":
    main()
