#!/usr/bin/env python3
"""
send_reminders.py — Outstanding Payment Reminder Emailer
=========================================================
Reads receivables.json + party_details.json + party_emails.json,
renders a personalised HTML email per party, and sends via Gmail SMTP.

Setup (one-time):
  1. Enable 2-Factor Authentication on your Google account
  2. Generate an App Password:
       myaccount.google.com → Security → App Passwords → Mail → Windows Computer
  3. Add to .env:
       GMAIL_SENDER=santoshhr76@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Run standalone:
  python send_reminders.py

Run with --dry-run to preview HTML without sending:
  python send_reminders.py --dry-run
"""

import argparse
import base64
import datetime
import json
import os
import smtplib
import sys
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
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
PARTY_EMAILS_F  = BASE_DIR  / "party_emails.json"

COMPANY_NAME    = "Surabhi Enterprises, Udaipur"
COMPANY_EMAIL   = os.environ.get("GMAIL_SENDER", "santoshhr76@gmail.com")
COMPANY_PHONE   = os.environ.get("COMPANY_PHONE", "+91-XXXXX-XXXXX")

SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 587


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_inr(amount: float) -> str:
    """Format amount as Indian Rupees with comma grouping."""
    amount = round(amount, 2)
    s = f"{amount:,.2f}"
    # Indian grouping: last group is 3 digits, rest are 2
    parts = s.split(".")
    integer = parts[0].replace(",", "")
    decimal = parts[1]
    if len(integer) <= 3:
        return f"₹{integer}.{decimal}"
    last3 = integer[-3:]
    rest = integer[:-3]
    groups = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return f"₹{','.join(groups)},{last3}.{decimal}"


def overdue_badge(days: int) -> str:
    if days == 0:
        color, label = "#0e9f6e", "Current"
    elif days <= 30:
        color, label = "#f59e0b", f"{days}d overdue"
    elif days <= 60:
        color, label = "#f97316", f"{days}d overdue"
    elif days <= 90:
        color, label = "#ef4444", f"{days}d overdue"
    else:
        color, label = "#7f1d1d", f"{days}d overdue"
    return (
        f'<span style="background:{color};color:#fff;'
        f'padding:2px 8px;border-radius:10px;font-size:11px;'
        f'font-weight:700;white-space:nowrap;">{label}</span>'
    )


# ── QR Code Generator ────────────────────────────────────────────────────────
UPI_VPA = os.environ.get("UPI_VPA", "surabhi@upi")   # set real VPA in .env

def _upi_url(amount: float) -> str:
    return (
        f"upi://pay?pa={UPI_VPA}"
        f"&pn=Surabhi+Enterprises+Udaipur"
        f"&am={amount:.2f}&cu=INR"
        f"&tn=Outstanding+Payment"
    )

def make_qr_bytes(amount: float) -> bytes | None:
    """Return PNG bytes for a UPI QR code, or None if qrcode is unavailable."""
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=2, box_size=6, border=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        qr.add_data(_upi_url(amount))
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0d1b2a", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return None


# ── HTML Email Renderer ───────────────────────────────────────────────────────
def render_email_html(party_name: str, bills: list, total_outstanding: float,
                      today_str: str, qr_src: str = "") -> str:
    """Render HTML email. qr_src can be a CID ('cid:qr_image'), a data URI, or ''."""
    max_days   = max((b["days_overdue"] for b in bills), default=0)
    oldest_due = max(bills, key=lambda b: b["days_overdue"])["due_date"]

    # Build bill rows
    bill_rows = ""
    for i, b in enumerate(sorted(bills, key=lambda x: x["days_overdue"], reverse=True)):
        row_bg = "#fff" if i % 2 == 0 else "#f8fafc"
        amt_color = "#ef4444" if b["days_overdue"] > 30 else "#1e293b"
        bill_rows += f"""
        <tr style="background:{row_bg};">
          <td style="padding:10px 14px;font-size:13px;color:#1e293b;border-bottom:1px solid #e2e8f0;">{b['bill_ref']}</td>
          <td style="padding:10px 14px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">{b['bill_date']}</td>
          <td style="padding:10px 14px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">{b['due_date']}</td>
          <td style="padding:10px 14px;font-size:13px;border-bottom:1px solid #e2e8f0;">{overdue_badge(b['days_overdue'])}</td>
          <td style="padding:10px 14px;font-size:13px;font-weight:700;color:{amt_color};text-align:right;border-bottom:1px solid #e2e8f0;">{fmt_inr(b['outstanding'])}</td>
        </tr>"""

    # Urgency color for header bar
    if max_days > 90:
        accent = "#7f1d1d"; accent_light = "#fef2f2"
    elif max_days > 60:
        accent = "#ef4444"; accent_light = "#fef2f2"
    elif max_days > 30:
        accent = "#f97316"; accent_light = "#fff7ed"
    else:
        accent = "#1a56db"; accent_light = "#eff6ff"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Payment Reminder — {party_name}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">

<!-- Wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

  <!-- Header Bar -->
  <tr>
    <td style="background:{accent};border-radius:12px 12px 0 0;padding:24px 32px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <div style="font-size:11px;font-weight:700;color:rgba(255,255,255,0.7);
                        letter-spacing:0.12em;text-transform:uppercase;margin-bottom:4px;">
              Payment Reminder
            </div>
            <div style="font-size:22px;font-weight:800;color:#ffffff;line-height:1.2;">
              {COMPANY_NAME}
            </div>
          </td>
          <td align="right" style="vertical-align:top;">
            <div style="background:rgba(255,255,255,0.15);border-radius:8px;
                        padding:8px 14px;display:inline-block;text-align:right;">
              <div style="font-size:10px;color:rgba(255,255,255,0.7);
                          text-transform:uppercase;letter-spacing:0.1em;">Date</div>
              <div style="font-size:14px;font-weight:700;color:#fff;">{today_str}</div>
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="background:#ffffff;padding:32px 32px 0;">

      <p style="font-size:15px;color:#1e293b;margin:0 0 8px;">Dear <strong>{party_name}</strong>,</p>

      <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 24px;">
        We hope this message finds you well. This is a gentle reminder regarding
        the outstanding dues on your account with us.
        Kindly review the details below and arrange payment at your earliest convenience.
      </p>

      <!-- Summary Cards -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
        <tr>
          <td width="33%" style="padding-right:8px;">
            <div style="background:{accent_light};border-radius:10px;padding:14px 16px;text-align:center;">
              <div style="font-size:10px;font-weight:700;color:#64748b;
                          text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
                Total Outstanding
              </div>
              <div style="font-size:20px;font-weight:800;color:{accent};">
                {fmt_inr(total_outstanding)}
              </div>
            </div>
          </td>
          <td width="33%" style="padding:0 4px;">
            <div style="background:#f8fafc;border-radius:10px;padding:14px 16px;text-align:center;">
              <div style="font-size:10px;font-weight:700;color:#64748b;
                          text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
                Oldest Due Date
              </div>
              <div style="font-size:16px;font-weight:800;color:#1e293b;">
                {oldest_due}
              </div>
            </div>
          </td>
          <td width="33%" style="padding-left:8px;">
            <div style="background:#f8fafc;border-radius:10px;padding:14px 16px;text-align:center;">
              <div style="font-size:10px;font-weight:700;color:#64748b;
                          text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
                No. of Bills
              </div>
              <div style="font-size:20px;font-weight:800;color:#1e293b;">
                {len(bills)}
              </div>
            </div>
          </td>
        </tr>
      </table>

      <!-- Bills Table -->
      <div style="font-size:11px;font-weight:700;color:#64748b;letter-spacing:0.1em;
                  text-transform:uppercase;margin-bottom:8px;">Outstanding Bills Breakdown</div>

      <table width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;
                    border-collapse:collapse;font-family:'Segoe UI',Arial,sans-serif;">
        <thead>
          <tr style="background:#f1f5f9;">
            <th style="padding:10px 14px;font-size:11px;font-weight:700;color:#64748b;
                       text-align:left;text-transform:uppercase;letter-spacing:0.08em;
                       border-bottom:2px solid #e2e8f0;">Bill Ref</th>
            <th style="padding:10px 14px;font-size:11px;font-weight:700;color:#64748b;
                       text-align:left;text-transform:uppercase;letter-spacing:0.08em;
                       border-bottom:2px solid #e2e8f0;">Bill Date</th>
            <th style="padding:10px 14px;font-size:11px;font-weight:700;color:#64748b;
                       text-align:left;text-transform:uppercase;letter-spacing:0.08em;
                       border-bottom:2px solid #e2e8f0;">Due Date</th>
            <th style="padding:10px 14px;font-size:11px;font-weight:700;color:#64748b;
                       text-align:left;text-transform:uppercase;letter-spacing:0.08em;
                       border-bottom:2px solid #e2e8f0;">Status</th>
            <th style="padding:10px 14px;font-size:11px;font-weight:700;color:#64748b;
                       text-align:right;text-transform:uppercase;letter-spacing:0.08em;
                       border-bottom:2px solid #e2e8f0;">Amount</th>
          </tr>
        </thead>
        <tbody>
          {bill_rows}
          <!-- Total Row -->
          <tr style="background:#f8fafc;">
            <td colspan="4" style="padding:12px 14px;font-size:13px;font-weight:700;
                                   color:#1e293b;border-top:2px solid #e2e8f0;">
              Total Outstanding
            </td>
            <td style="padding:12px 14px;font-size:15px;font-weight:800;
                       color:{accent};text-align:right;border-top:2px solid #e2e8f0;">
              {fmt_inr(total_outstanding)}
            </td>
          </tr>
        </tbody>
      </table>

    </td>
  </tr>

  <!-- CTA + Closing -->
  <tr>
    <td style="background:#ffffff;padding:28px 32px 32px;">

      <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 16px;">
        We request you to kindly clear the above outstanding amount at the earliest.
        If the payment has already been made, please disregard this reminder.
        In case of any discrepancy, please reach out to us immediately.
      </p>

      <!-- CTA Box + QR Code -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
        <tr>
          <td style="vertical-align:middle;">
            <div style="background:{accent_light};border-left:4px solid {accent};
                        border-radius:0 8px 8px 0;padding:14px 18px;">
              <div style="font-size:13px;font-weight:700;color:{accent};">
                Please arrange payment of {fmt_inr(total_outstanding)}
              </div>
            </div>
          </td>
          <td width="100" style="padding-left:16px;vertical-align:middle;text-align:center;">
            <img src="{{QR_DATA_URI}}"
                 width="88" height="88"
                 alt="Scan to Pay"
                 style="display:block;border:2px solid #e2e8f0;border-radius:8px;"/>
            <div style="font-size:10px;color:#94a3b8;margin-top:4px;font-weight:600;
                        text-transform:uppercase;letter-spacing:0.08em;">Scan to Pay</div>
          </td>
        </tr>
      </table>

      <p style="font-size:14px;color:#475569;margin:0 0 4px;">
        Thank you for your continued business with us.
      </p>
      <p style="font-size:14px;color:#1e293b;font-weight:700;margin:0 0 2px;">
        {COMPANY_NAME}
      </p>
      <p style="font-size:13px;color:#64748b;margin:0;">
        {COMPANY_EMAIL} &nbsp;·&nbsp; {COMPANY_PHONE}
      </p>

    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#0d1b2a;border-radius:0 0 12px 12px;padding:16px 32px;">
      <p style="font-size:11px;color:#64748b;margin:0;text-align:center;">
        This is an automated payment reminder from {COMPANY_NAME}.
        Please do not reply to this email directly — contact us at {COMPANY_EMAIL}.
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""
    html = html.replace("{QR_DATA_URI}", qr_src)
    return html


# ── Plain-text fallback ───────────────────────────────────────────────────────
def render_email_text(party_name: str, bills: list, total_outstanding: float,
                      today_str: str) -> str:
    max_days   = max((b["days_overdue"] for b in bills), default=0)
    oldest_due = max(bills, key=lambda b: b["days_overdue"])["due_date"]
    lines = [
        f"PAYMENT REMINDER — {COMPANY_NAME}",
        f"Date: {today_str}",
        "=" * 60,
        f"",
        f"Dear {party_name},",
        f"",
        f"This is a reminder regarding outstanding dues on your account.",
        f"",
        f"OUTSTANDING SUMMARY",
        f"  Total Outstanding : {fmt_inr(total_outstanding)}",
        f"  Oldest Due Date   : {oldest_due}",
        f"  Max Overdue       : {max_days} days",
        f"  Number of Bills   : {len(bills)}",
        f"",
        f"BILL DETAILS",
        f"{'Bill Ref':<15} {'Bill Date':<14} {'Due Date':<14} {'Days':>6}  {'Amount':>14}",
        f"{'-'*65}",
    ]
    for b in sorted(bills, key=lambda x: x["days_overdue"], reverse=True):
        lines.append(
            f"{b['bill_ref']:<15} {b['bill_date']:<14} {b['due_date']:<14} "
            f"{b['days_overdue']:>6}d  {fmt_inr(b['outstanding']):>14}"
        )
    lines += [
        f"{'-'*65}",
        f"{'TOTAL':<45} {fmt_inr(total_outstanding):>14}",
        f"",
        f"Kindly arrange payment at the earliest.",
        f"",
        f"Thank you,",
        f"{COMPANY_NAME}",
        f"{COMPANY_EMAIL}  |  {COMPANY_PHONE}",
    ]
    return "\n".join(lines)


# ── Email Sender ──────────────────────────────────────────────────────────────
def send_email(smtp_conn, to_addr: str, subject: str,
               html_body: str, text_body: str) -> None:
    """
    Send a clean multipart/alternative email (plain-text + HTML).
    QR code is referenced as an external URL in the HTML body — works in
    Gmail and all other clients without CID embedding issues.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{COMPANY_NAME} <{COMPANY_EMAIL}>"
    msg["To"]      = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))
    smtp_conn.send_message(msg)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Send outstanding payment reminders")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render emails and save as HTML files; do NOT send")
    args = parser.parse_args()

    # ── Load data ──────────────────────────────────────────────────────────
    if not RECEIVABLES_F.exists():
        print("[ERROR] output/receivables.json not found. Run the pipeline first.")
        sys.exit(1)

    recv     = json.loads(RECEIVABLES_F.read_text(encoding="utf-8"))
    details  = json.loads(PARTY_DETAILS_F.read_text(encoding="utf-8")) if PARTY_DETAILS_F.exists() else []

    # Build email lookup: party_details.json email field + party_emails.json overrides
    email_map: dict[str, str] = {}
    for d in details:
        if d.get("email", "").strip():
            email_map[d["party_name"]] = d["email"].strip()

    if PARTY_EMAILS_F.exists():
        overrides = json.loads(PARTY_EMAILS_F.read_text(encoding="utf-8"))
        for party_name, email_addr in overrides.items():
            if email_addr.strip():
                email_map[party_name] = email_addr.strip()

    today = datetime.date.today()
    try:
        today_str = today.strftime("%#d %B %Y")  # Windows
    except ValueError:
        today_str = today.strftime("%-d %B %Y")  # Linux/Mac

    bills_by_party = {p["party"]: p for p in recv.get("bills_by_party", [])}

    # ── Filter parties that have emails ────────────────────────────────────
    parties_to_notify = [
        (name, email_map[name])
        for name in email_map
        if name in bills_by_party
    ]

    if not parties_to_notify:
        return  # No emails configured — skip silently

    # ── Dry run: save HTML previews (base64 data URI works in browser) ────────
    if args.dry_run:
        preview_dir = OUTPUT_DIR / "email_previews"
        preview_dir.mkdir(exist_ok=True)
        for party_name, email_addr in parties_to_notify:
            pd       = bills_by_party[party_name]
            qr_bytes = make_qr_bytes(pd["outstanding"])
            if qr_bytes:
                qr_src = "data:image/png;base64," + base64.b64encode(qr_bytes).decode()
            else:
                # Fallback to external URL for browser preview if qrcode unavailable
                qr_src = (
                    "https://api.qrserver.com/v1/create-qr-code/"
                    f"?size=88x88&data={urllib.parse.quote(_upi_url(pd['outstanding']), safe='')}"
                )
            html  = render_email_html(party_name, pd["bills"], pd["outstanding"],
                                      today_str, qr_src=qr_src)
            safe  = party_name.replace(" ", "_").replace("/", "-")[:40]
            fpath = preview_dir / f"{safe}.html"
            fpath.write_text(html, encoding="utf-8")
            print(f"  [PREVIEW] {party_name} → {fpath.name}")
        print(f"\n[OK] HTML previews saved to output/email_previews/")
        return

    # ── Real send ──────────────────────────────────────────────────────────
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_password:
        return  # GMAIL_APP_PASSWORD not set — skip silently

    sent = 0
    failed = 0

    print(f"\n[SEND] Connecting to Gmail SMTP...")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(COMPANY_EMAIL, app_password)
            print(f"[OK] Authenticated as {COMPANY_EMAIL}")

            for party_name, to_addr in parties_to_notify:
                pd      = bills_by_party[party_name]
                max_days = max((b["days_overdue"] for b in pd["bills"]), default=0)
                subject = (
                    f"Payment Reminder: Outstanding \u20b9{pd['outstanding']:,.0f} "
                    f"\u2014 {party_name} [{today_str}]"
                )
                # External QR URL — renders reliably in Gmail and all clients
                qr_src = (
                    "https://api.qrserver.com/v1/create-qr-code/"
                    f"?size=88x88&data={urllib.parse.quote(_upi_url(pd['outstanding']), safe='')}"
                )
                html_body = render_email_html(
                    party_name, pd["bills"], pd["outstanding"], today_str,
                    qr_src=qr_src)
                text_body = render_email_text(
                    party_name, pd["bills"], pd["outstanding"], today_str)
                try:
                    send_email(smtp, to_addr, subject, html_body, text_body)
                    print(f"  [SENT]   {party_name} -> {to_addr}  "
                          f"(Rs {pd['outstanding']:,.0f}, max {max_days}d overdue)")
                    sent += 1
                except Exception as e:
                    print(f"  [FAIL]   {party_name} -> {to_addr}  ERROR: {e}")
                    failed += 1

    except smtplib.SMTPAuthenticationError:
        print("[ERROR] Gmail authentication failed.")
        print("        Check GMAIL_SENDER and GMAIL_APP_PASSWORD in .env")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] SMTP connection failed: {e}")
        sys.exit(1)

    print(f"\n[DONE] Sent: {sent}  |  Failed: {failed}")


if __name__ == "__main__":
    main()
