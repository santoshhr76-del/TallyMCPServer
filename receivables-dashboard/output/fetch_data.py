"""
One-shot script: fetch outstanding receivables + ledger details from TallyPrime
and write output/receivables.json and output/party_details.json.
"""

import sys
import json
import os

# Make sure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import tallyprime_mcp.tally_client as tc

TALLY_URL          = "http://tally.tallymcpclient.com"
OUTPUT_DIR         = os.path.dirname(os.path.abspath(__file__))
RECEIVABLES_PATH   = os.path.join(OUTPUT_DIR, "receivables.json")
PARTY_DETAILS_PATH = os.path.join(OUTPUT_DIR, "party_details.json")

# ── Step 1: fetch outstanding receivables ────────────────────────────────────
print("Fetching outstanding receivables ...")
rec = tc.fetch_outstanding_receivables(
    as_of_date="21-03-2026",
    ledger_group="Sundry Debtors",
    tally_url=TALLY_URL,
)

# Save verbatim — do NOT modify or summarise
with open(RECEIVABLES_PATH, "w", encoding="utf-8") as f:
    json.dump(rec, f, ensure_ascii=False, indent=2)

# Check for top-level error
if "error" in rec and not rec.get("party_summary") and not rec.get("parties"):
    print(f"ERROR from TallyPrime: {rec['error']}")
    print(f"Saved error response to {RECEIVABLES_PATH}")
    sys.exit(1)

# Support both key variants the client may use
parties_data  = rec.get("party_summary") or rec.get("parties", [])
grand_total   = rec.get("total_outstanding", rec.get("grand_total", 0))
party_count   = rec.get("party_count", len(parties_data))

print(f"Receivables fetched — {party_count} parties, grand total Rs {grand_total}")

# Build outstanding lookup from party_summary (most direct source)
outstanding_map: dict[str, float] = {}
for p in parties_data:
    pname = p.get("party") or p.get("name", "")
    outstanding_map[pname] = float(p.get("outstanding", p.get("closing_balance", 0)))

# Collect unique party names preserving order
unique_names = list(dict.fromkeys(
    p.get("party") or p.get("name", "") for p in parties_data
))

# ── Step 2: fetch ledger details for each party ───────────────────────────────
print(f"Fetching ledger details for {len(unique_names)} parties ...")
party_details = []

for name in unique_names:
    print(f"  get_ledger: {name}")
    try:
        led = tc.fetch_ledger(name=name, tally_url=TALLY_URL)
    except Exception as exc:
        led = {"error": str(exc)}

    if "error" in led:
        party_details.append({
            "party_name":  name,
            "gstin":       "",
            "state":       "",
            "pincode":     "",
            "addresses":   [],
            "phone":       "",
            "email":       "",
            "outstanding": outstanding_map.get(name, 0),
            "error":       led["error"],
        })
    else:
        party_details.append({
            "party_name":  name,
            "gstin":       led.get("gstin", ""),
            "state":       led.get("state", ""),
            "pincode":     led.get("pincode", ""),
            "addresses":   led.get("addresses", []),
            "phone":       led.get("phone", ""),
            "email":       led.get("email", ""),
            "outstanding": outstanding_map.get(name, 0),
        })

# ── Step 3: save party_details.json ──────────────────────────────────────────
with open(PARTY_DETAILS_PATH, "w", encoding="utf-8") as f:
    json.dump(party_details, f, ensure_ascii=False, indent=2)

with_pin  = sum(1 for p in party_details if str(p.get("pincode", "")).strip())
missing   = len(party_details) - with_pin

print(f"Party details fetched — {len(party_details)} parties, {with_pin} with pin codes, {missing} missing pin codes")
print(f"Files written:")
print(f"  {RECEIVABLES_PATH}")
print(f"  {PARTY_DETAILS_PATH}")
