"""
fetch_place_names.py
====================
Fetches a human-readable place name for every pin code in map_data.json
by querying the Nominatim OpenStreetMap API (with India Post as fallback).

Adds:
  • "place_name" field to each party entry in map_data.json
  • "pincode_labels" dict at the top level of map_data.json
  • Permanent place-name labels on the Leaflet map in dashboard.html

Usage (run from receivables-dashboard/):
    python fetch_place_names.py
"""

import json, time, re, pathlib, urllib.request, urllib.parse
from collections import defaultdict

OUTPUT     = pathlib.Path("output")
MAP_JSON   = OUTPUT / "map_data.json"
DASH_HTML  = OUTPUT / "dashboard.html"

HEADERS    = {"User-Agent": "receivables-dashboard/1.0 (contact: admin@example.com)"}
SLEEP_SEC  = 1.1          # Nominatim rate-limit: max 1 req/sec


# ── helpers ──────────────────────────────────────────────────────────────────

def fetch_json(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    ⚠ HTTP error for {url}: {e}")
        return None


def nominatim_place_name(pincode: str) -> str | None:
    """Return best locality name from Nominatim for an Indian pincode."""
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?postalcode={pincode}&country=IN&format=json&addressdetails=1&limit=1"
    )
    data = fetch_json(url)
    if not data:
        return None
    first = data[0] if isinstance(data, list) and data else None
    if not first:
        return None
    addr = first.get("address", {})
    # Priority: most specific → least specific
    for key in ("village", "town", "suburb", "city_district",
                "city", "county", "state_district"):
        if addr.get(key):
            return addr[key]
    # Fallback: first comma-segment of display_name
    display = first.get("display_name", "")
    return display.split(",")[0].strip() if display else None


def indiapost_place_name(pincode: str) -> str | None:
    """Fallback: India Post API — returns PostOffice[0].Name."""
    url = f"https://api.postalpincode.in/pincode/{pincode}"
    data = fetch_json(url)
    if not data or not isinstance(data, list):
        return None
    block = data[0]
    if block.get("Status") != "Success":
        return None
    offices = block.get("PostOffice") or []
    if not offices:
        return None
    # Prefer district name as the broadest useful label
    return offices[0].get("District") or offices[0].get("Name")


# ── 1. Load map_data.json ────────────────────────────────────────────────────

raw = json.loads(MAP_JSON.read_text(encoding="utf-8"))
parties      = raw["parties"] if isinstance(raw, dict) else raw
pincode_labels = raw.get("pincode_labels", {}) if isinstance(raw, dict) else {}

# Collect unique pincodes that don't already have a resolved label
unique_pins = sorted({
    str(p.get("pincode", "")).strip()
    for p in parties
    if p.get("pincode") and str(p.get("pincode")).strip()
})

print(f"  Pin codes to resolve: {len(unique_pins)}")

# ── 2. Fetch place names ─────────────────────────────────────────────────────

for pin in unique_pins:
    if pin in pincode_labels:
        print(f"  ✓ {pin} → {pincode_labels[pin]}  (cached)")
        continue

    print(f"  Querying Nominatim for {pin} …", end=" ", flush=True)
    name = nominatim_place_name(pin)
    time.sleep(SLEEP_SEC)

    if not name:
        print("Nominatim failed — trying India Post …", end=" ", flush=True)
        name = indiapost_place_name(pin)

    if name:
        pincode_labels[pin] = name
        print(f"→ {name}")
    else:
        print("→ not found")

# ── 3. Enrich each party with its place_name ─────────────────────────────────

for p in parties:
    pin = str(p.get("pincode", "")).strip()
    p["place_name"] = pincode_labels.get(pin, "")

# ── 4. Save updated map_data.json ─────────────────────────────────────────────

if isinstance(raw, dict):
    raw["pincode_labels"] = pincode_labels
    raw["parties"]        = parties
else:
    raw = {
        "generated_at":   "",
        "party_count":    len(parties),
        "note":           "Geocoded via Nominatim + India Post",
        "pincode_labels": pincode_labels,
        "parties":        parties,
    }

MAP_JSON.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  ✅ map_data.json updated — {len(pincode_labels)} place labels resolved")

# ── 5. Patch dashboard.html — add/replace permanent place-name labels ─────────

html = DASH_HTML.read_text(encoding="utf-8")

# Compute representative lat/lng per pincode (average of all parties)
pin_coords: dict[str, list] = defaultdict(list)
for p in parties:
    pin = str(p.get("pincode", "")).strip()
    if pin and p.get("lat") and p.get("lng"):
        pin_coords[pin].append((float(p["lat"]), float(p["lng"])))

label_lines = []
for pin, place in pincode_labels.items():
    if not place:
        continue
    coords = pin_coords.get(pin)
    if not coords:
        continue
    avg_lat = sum(c[0] for c in coords) / len(coords)
    avg_lng = sum(c[1] for c in coords) / len(coords)
    safe    = place.replace("'", "\\'").replace('"', '\\"')
    label_lines.append(
        f"L.marker([{avg_lat:.4f},{avg_lng:.4f}], {{"
        f"icon:L.divIcon({{"
        f"className:'',"
        f"html:'<div style=\"background:rgba(255,255,255,0.88);padding:2px 7px;"
        f"border-radius:4px;font-size:10px;font-weight:700;color:#1e293b;"
        f"white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,0.22);"
        f"pointer-events:none;\">{safe}</div>',"
        f"iconAnchor:[0,0]}}),interactive:false}}).addTo(map);"
    )

label_block = (
    "\n  // ── Place name labels (auto-generated) ─────────────────────\n  "
    + "\n  ".join(label_lines)
    + "\n"
)

# Remove any previous label block
html = re.sub(
    r"// ── Place name labels.*?// ──",
    "// ──",
    html,
    flags=re.DOTALL,
)

# Inject before the legend control (works for both inline map variants)
for anchor in ("  var leg=L.control(", "  var legend = L.control("):
    if anchor in html:
        html = html.replace(anchor, label_block + anchor, 1)
        break

DASH_HTML.write_text(html, encoding="utf-8")
print(f"  ✅ dashboard.html updated — {len(label_lines)} permanent place labels added")
print("  👉 Run:  start output\\dashboard.html")
