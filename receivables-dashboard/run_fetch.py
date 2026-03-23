"""
One-shot fetch: outstanding receivables + per-party ledger details from TallyPrime.
Saves:
  output/receivables.json   — verbatim response from fetch_outstanding_receivables
  output/party_details.json — enriched per-party array with ledger details
"""
import sys
import os
import json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR   = os.path.join(REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import tallyprime_mcp.tally_client as tc

TALLY_URL    = "http://tally.tallymcpclient.com"
AS_OF_DATE   = "05-04-2026"   # today (22-03-2026) + 14 days
LEDGER_GROUP = "Sundry Debtors"
OUTPUT_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Step 1 : fetch outstanding receivables ────────────────────────────────────
print(f"Fetching outstanding receivables as of {AS_OF_DATE} …")
recv = tc.fetch_outstanding_receivables(
    as_of_date   = AS_OF_DATE,
    ledger_group = LEDGER_GROUP,
    tally_url    = TALLY_URL,
)

# ── Step 2 : save receivables.json verbatim ───────────────────────────────────
recv_path = os.path.join(OUTPUT_DIR, "receivables.json")
with open(recv_path, "w", encoding="utf-8") as f:
    json.dump(recv, f, ensure_ascii=False, indent=2)

if "error" in recv:
    print(f"ERROR from TallyPrime: {recv['error']}")
    print("Aborting — no ledger lookups will be performed.")
    sys.exit(1)

party_summary = recv.get("party_summary", [])
grand_total   = recv.get("total_outstanding", 0)
print(f"Receivables fetched — {len(party_summary)} parties, grand total Rs {grand_total:,.2f}")

# ── Step 3 : collect unique party names ───────────────────────────────────────
party_names = sorted({p["party"] for p in party_summary if p.get("party")})
print(f"Unique parties to look up: {len(party_names)}")

outstanding_map = {p["party"]: p.get("outstanding", 0.0) for p in party_summary}

# ── Step 4 : fetch ledger details for each party ──────────────────────────────
party_details = []
for i, name in enumerate(party_names, 1):
    print(f"  [{i}/{len(party_names)}] get_ledger: {name}")
    try:
        ledger = tc.fetch_ledger(name=name, tally_url=TALLY_URL)
    except Exception as exc:
        ledger = {"error": str(exc)}

    if "error" in ledger:
        party_details.append({
            "party_name":  name,
            "gstin":       "",
            "state":       "",
            "pincode":     "",
            "addresses":   [],
            "phone":       "",
            "email":       "",
            "outstanding": outstanding_map.get(name, 0.0),
            "error":       ledger["error"],
        })
    else:
        party_details.append({
            "party_name":  name,
            "gstin":       ledger.get("gstin", ""),
            "state":       ledger.get("state", ""),
            "pincode":     ledger.get("pincode", ""),
            "addresses":   ledger.get("addresses", []),
            "phone":       ledger.get("phone", ""),
            "email":       ledger.get("email", ""),
            "outstanding": outstanding_map.get(name, 0.0),
        })

# ── Step 5 : save party_details.json ─────────────────────────────────────────
details_path = os.path.join(OUTPUT_DIR, "party_details.json")
with open(details_path, "w", encoding="utf-8") as f:
    json.dump(party_details, f, ensure_ascii=False, indent=2)

with_pin    = sum(1 for p in party_details if p.get("pincode"))
without_pin = len(party_details) - with_pin
print(f"Party details fetched — {len(party_details)} parties, {with_pin} with pin codes, {without_pin} missing pin codes")
print(f"\nOutput files:")
print(f"  {recv_path}")
print(f"  {details_path}")
