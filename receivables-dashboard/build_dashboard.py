#!/usr/bin/env python3
"""
build_dashboard.py — Fast dashboard data refresh (no LLM required)
===================================================================

Replaces the DATA CONSTANTS block in output/dashboard.html with fresh
values computed from output/receivables.json + output/party_details.json.

Runtime: < 3 seconds  (vs 15-20 min for DASHBOARD_AGENT)

Run automatically by main.py after the data agents complete.
"""

import json
import re
import datetime
import pathlib

output = pathlib.Path("output")

# ── Tally state-code → readable name ──────────────────────────────────────
STATE_CODES: dict = {
    "0":   "Rajasthan",
    "1":   "Jammu & Kashmir",
    "2":   "Himachal Pradesh",
    "3":   "Punjab",
    "4":   "Chandigarh",
    "5":   "Uttarakhand",
    "6":   "Haryana",
    "7":   "Delhi",
    "8":   "Rajasthan",
    "9":   "Uttar Pradesh",
    "10":  "Bihar",
    "11":  "Sikkim",
    "12":  "Arunachal Pradesh",
    "13":  "Nagaland",
    "14":  "Manipur",
    "15":  "Mizoram",
    "16":  "Tripura",
    "17":  "Meghalaya",
    "18":  "Assam",
    "19":  "West Bengal",
    "20":  "Jharkhand",
    "21":  "Odisha",
    "22":  "Chhattisgarh",
    "23":  "Madhya Pradesh",
    "24":  "Gujarat",
    "26":  "Dadra & Nagar Haveli",
    "27":  "Maharashtra",
    "29":  "Karnataka",
    "30":  "Goa",
    "31":  "Lakshadweep",
    "32":  "Kerala",
    "33":  "Tamil Nadu",
    "34":  "Puducherry",
    "35":  "Andaman & Nicobar",
    "36":  "Telangana",
    "37":  "Andhra Pradesh",
}


def resolve_state(raw: str) -> str:
    """Map TallyPrime state code → human-readable name."""
    s = str(raw).strip()
    if s in STATE_CODES:
        return STATE_CODES[s]
    if s == "" or s == "0":
        return "Rajasthan"   # default for this region
    # If it's already a readable name (not a digit code), return as-is
    if not s.isdigit():
        return s if s not in ("—", "-", "None") else "—"
    return s


def js_escape(s: str) -> str:
    """Escape a string for safe embedding in a JS single-quoted string."""
    return (str(s)
            .replace("\\", "\\\\")
            .replace("'",  "\\'")
            .replace("\n", " ")
            .replace("\r", ""))


def fmt_inr(n: float) -> str:
    """Indian ₹ notation for display in print output."""
    n = round(abs(n), 2)
    s = f"{n:,.2f}"
    # Simple re-format (full Indian grouping handled by JS fINR)
    return f"Rs {s}"


# ── Load source files ──────────────────────────────────────────────────────
print("[BUILD] Reading receivables.json ...")
recv = json.loads((output / "receivables.json").read_text(encoding="utf-8"))

print("[BUILD] Reading party_details.json ...")
party_details_raw = json.loads((output / "party_details.json").read_text(encoding="utf-8"))

# Build pincode/state lookup keyed by party name
detail_map: dict = {p["party_name"]: p for p in party_details_raw}

# ── Extract top-level metrics ──────────────────────────────────────────────
GRAND_TOTAL: float = float(recv.get("total_outstanding", 0))
PARTY_COUNT: int   = int(recv.get("party_count", 0))
BILL_COUNT:  int   = int(recv.get("bill_count",  0))

aging_raw = recv.get("aging_summary", {})
AGING = {
    "overdue_1_30":     float(aging_raw.get("overdue_1_30",     0)),
    "overdue_31_60":    float(aging_raw.get("overdue_31_60",    0)),
    "overdue_61_90":    float(aging_raw.get("overdue_61_90",    0)),
    "overdue_above_90": float(aging_raw.get("overdue_above_90", 0)),
}

# ── Build PARTIES array ────────────────────────────────────────────────────
bills_by_party: dict = {
    entry["party"]: entry.get("bills", [])
    for entry in recv.get("bills_by_party", [])
}

parties_list = []
for ps in recv.get("party_summary", []):
    name        = ps["party"]
    outstanding = float(ps.get("outstanding", 0))
    detail      = detail_map.get(name, {})
    state       = resolve_state(detail.get("state", "Rajasthan"))
    pincode     = str(detail.get("pincode", "—") or "—")

    raw_bills   = bills_by_party.get(name, [])
    bill_count  = len(raw_bills)
    max_days    = 0
    bills_out   = []

    for b in raw_bills:
        overdue = int(b.get("days_overdue", 0))
        max_days = max(max_days, overdue)
        bills_out.append({
            "ref":    b.get("bill_ref", ""),
            "date":   b.get("bill_date", ""),
            "due":    b.get("due_date",  ""),
            "amt":    float(b.get("outstanding", b.get("amount", 0))),
            "overdue": overdue,
        })

    parties_list.append({
        "name":        name,
        "state":       state,
        "pincode":     pincode,
        "outstanding": outstanding,
        "bill_count":  bill_count,
        "max_days":    max_days,
        "bills":       bills_out,
    })

# ── Build TOP5 ─────────────────────────────────────────────────────────────
top5 = sorted(parties_list, key=lambda p: p["outstanding"], reverse=True)[:5]

# ── Render JS constants block ──────────────────────────────────────────────
def render_parties_js(parties: list) -> str:
    lines = ["["]
    for p in parties:
        bills_items = []
        for b in p["bills"]:
            bills_items.append(
                f"      {{ref:'{js_escape(b['ref'])}', date:'{js_escape(b['date'])}', "
                f"due:'{js_escape(b['due'])}', amt:{b['amt']}, overdue:{b['overdue']}}}"
            )
        bills_js = "[\n" + ",\n".join(bills_items) + "\n    ]" if bills_items else "[]"
        lines.append(
            f"  {{ name:'{js_escape(p['name'])}', state:'{js_escape(p['state'])}', "
            f"pincode:'{js_escape(p['pincode'])}', outstanding:{p['outstanding']}, "
            f"bill_count:{p['bill_count']}, max_days:{p['max_days']},\n"
            f"    bills:{bills_js}}}, "
        )
    lines.append("]")
    return "\n".join(lines)


def render_top5_js(top5: list) -> str:
    items = [
        f"  {{ name:'{js_escape(p['name'])}', outstanding:{p['outstanding']}, bills:{p['bill_count']} }}"
        for p in top5
    ]
    return "[\n" + ",\n".join(items) + "\n]"


try:
    today_str = datetime.date.today().strftime("%#d-%b-%Y")  # Windows
except ValueError:
    today_str = datetime.date.today().strftime("%-d-%b-%Y")  # Linux/Mac

new_constants = f"""/* =====================================================
   DATA CONSTANTS
===================================================== */
const GRAND_TOTAL = {GRAND_TOTAL};
const PARTY_COUNT = {PARTY_COUNT};
const BILL_COUNT  = {BILL_COUNT};
const AGING = {{
  overdue_1_30:     {AGING['overdue_1_30']},
  overdue_31_60:    {AGING['overdue_31_60']},
  overdue_61_90:    {AGING['overdue_61_90']},
  overdue_above_90: {AGING['overdue_above_90']}
}};

const PARTIES = {render_parties_js(parties_list)};

const TOP5 = {render_top5_js(top5)};"""

# ── Patch dashboard.html ───────────────────────────────────────────────────
dashboard_path = output / "dashboard.html"
if not dashboard_path.exists():
    print("[ERROR] output/dashboard.html not found.")
    print("        Run the full pipeline once first to generate the template.")
    raise SystemExit(1)

html = dashboard_path.read_text(encoding="utf-8")

# Replace DATA CONSTANTS block (everything between the two sentinel comments)
pattern = re.compile(
    r"/\*\s*={3,}\s*\n\s*DATA CONSTANTS\s*\n\s*={3,}\s*\*/.*?"
    r"(?=/\*\s*={3,}\s*\n\s*HELPERS)",
    re.DOTALL,
)
if not pattern.search(html):
    print("[ERROR] Could not locate DATA CONSTANTS sentinel in dashboard.html.")
    print("        The template may be from an incompatible pipeline version.")
    raise SystemExit(1)

html = pattern.sub(new_constants + "\n\n", html)

# Update navbar "As of" date
html = re.sub(
    r'As of \d{1,2}-[A-Za-z]{3}-\d{4}',
    f'As of {today_str}',
    html,
)

dashboard_path.write_text(html, encoding="utf-8")

print(f"[OK] dashboard.html updated:")
print(f"     Parties: {PARTY_COUNT}  |  Bills: {BILL_COUNT}  |  Total: {fmt_inr(GRAND_TOTAL)}")
print(f"     Aging  : 1-30d={AGING['overdue_1_30']:.0f} | "
      f"31-60d={AGING['overdue_31_60']:.0f} | "
      f"61-90d={AGING['overdue_61_90']:.0f} | "
      f">90d={AGING['overdue_above_90']:.0f}")
print(f"     Top 5  : {', '.join(p['name'].split()[0] for p in top5)}")
