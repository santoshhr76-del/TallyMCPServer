"""
fix_map.py — Embeds the Leaflet map directly into dashboard.html.

Replaces the <div id="map-placeholder"> left by the Dashboard Agent with a
fully-featured inline Leaflet map built from map_data.json:
  - sqrt-scaled circle markers (proportional to outstanding amount)
  - Hover tooltips (party name, amount, state, overdue days)
  - Permanent place-name labels for each unique pincode
  - Overdue colour coding: red >90d | orange 31-90d | green ≤30d
  - Jitter offset for parties sharing same pincode (all bubbles visible)
  - Legend at bottom-right

Run automatically by main.py after the pipeline completes.
"""

import json, re, math, pathlib

output = pathlib.Path("output")

# ── Hardcoded pincode → place-name lookup (Udaipur region) ────────────────
# These override any names returned by geocoding APIs.
PINCODE_NAMES: dict = {
    "313001": "Pratapnagar",
    "313002": "H Magri",
    "313003": "Girwa",
    "313004": "Udaipur City",
    "313005": "Udaipur City",
    "313011": "Badgaon",
    "313024": "Debari",
    "313025": "Bari",
    "313027": "Salumber",
    "313031": "Sisarma",
    "313705": "Gogunda",
    "313903": "Kalyanpur",
    "313803": "Kherwara",
}

# ── Load map data ──────────────────────────────────────────────────────────
raw         = json.loads((output / "map_data.json").read_text(encoding="utf-8"))
all_parties = raw["parties"] if isinstance(raw, dict) else raw
parties     = [p for p in all_parties if p.get("lat") and p.get("lng")]
print(f"  Parties with coordinates: {len(parties)} / {len(all_parties)}")

# ── Load overdue days from receivables.json (map_data.json has zeroes) ────
max_overdue_by_party: dict = {}
recv_path = output / "receivables.json"
if recv_path.exists():
    recv = json.loads(recv_path.read_text(encoding="utf-8"))
    for entry in recv.get("bills_by_party", []):
        party_name = entry.get("party", "")
        bills = entry.get("bills", [])
        if bills:
            max_overdue_by_party[party_name] = max(
                int(b.get("days_overdue", 0)) for b in bills
            )
    print(f"  Loaded overdue data for {len(max_overdue_by_party)} parties from receivables.json")

# ── Compute sqrt-scaled radii (min 6, max 28) ─────────────────────────────
amounts = [float(p.get("outstanding", 0)) for p in parties]
max_amt = max(amounts) if amounts else 1
def scaled_radius(amt: float) -> int:
    return max(6, min(28, int(6 + 22 * math.sqrt(amt / max_amt))))

# ── Build place-name labels (hardcoded names take priority) ───────────────
# Start with whatever the pipeline geocoder stored in map_data.json
pincode_labels: dict = raw.get("pincode_labels", {}) if isinstance(raw, dict) else {}
# Supplement with per-party place_name if pincode_labels is sparse
for p in all_parties:
    pc = str(p.get("pincode", ""))
    pn = p.get("place_name", "")
    if pc and pn and pc not in pincode_labels:
        pincode_labels[pc] = pn
# Override (or add) with the authoritative hardcoded Udaipur-region names
pincode_labels.update(PINCODE_NAMES)

# Build one label per unique pincode (placed at pincode centroid)
label_markers_js = ""
seen_pins: set = set()
for p in parties:
    pc = str(p.get("pincode", ""))
    if pc in seen_pins or pc not in pincode_labels:
        continue
    seen_pins.add(pc)
    label = str(pincode_labels[pc]).replace("'", "\\'")
    lat   = p.get("lat", 0)
    lng   = p.get("lng", 0)
    label_markers_js += (
        f"L.marker([{lat},{lng}], {{\n"
        f"  icon: L.divIcon({{\n"
        f"    className: 'place-label-icon',\n"
        f"    html: '<div class=\"place-label\">{label}</div>',\n"
        f"    iconSize: [0, 0],\n"
        f"    iconAnchor: [0, 8]\n"
        f"  }})\n"
        f"}}).addTo(map);\n"
    )

# ── Compute jitter offsets for parties sharing the same pincode ───────────
# Groups parties by pincode and spreads them in a small circle so all
# bubbles are individually visible instead of stacking on the same pixel.
from collections import defaultdict

# Group party indices by pincode
pincode_groups: dict = defaultdict(list)
for i, p in enumerate(parties):
    pc = str(p.get("pincode", ""))
    pincode_groups[pc].append(i)

# Jitter radius in degrees (~0.0008° ≈ 90m at this latitude)
JITTER_DEG = 0.0008

party_offsets: dict = {}  # index → (dlat, dlng)
for pc, indices in pincode_groups.items():
    n = len(indices)
    if n == 1:
        party_offsets[indices[0]] = (0.0, 0.0)
    else:
        # Arrange in a circle; first party stays near centre
        for k, idx in enumerate(indices):
            angle = (2 * math.pi * k) / n
            dlat  = JITTER_DEG * math.sin(angle)
            dlng  = JITTER_DEG * math.cos(angle)
            party_offsets[idx] = (dlat, dlng)

# ── Build circle markers ───────────────────────────────────────────────────
markers_js = ""
all_latlngs: list = []   # collected for fitBounds
for i, p in enumerate(parties):
    name    = str(p.get("name") or p.get("party_name") or "").replace("'", "\\'")
    base_lat = p.get("lat", 0)
    base_lng = p.get("lng", 0)
    dlat, dlng = party_offsets.get(i, (0.0, 0.0))
    lat  = round(base_lat + dlat, 6)
    lng  = round(base_lng + dlng, 6)
    amt     = float(p.get("outstanding", 0))
    state_raw = str(p.get("state", "")).replace("'", "\\'")
    pin     = str(p.get("pincode", ""))
    radius  = scaled_radius(amt)

    # Use overdue days from receivables.json (map_data.json values are unreliable)
    overdue = max_overdue_by_party.get(name, 0)
    # Fallback to map_data.json field if not found by name
    if overdue == 0:
        overdue = int(p.get("max_days_overdue") or p.get("max_overdue_days") or 0)

    if overdue > 90:
        color = "#e02424"   # red
    elif overdue > 30:
        color = "#ff5a1f"   # orange
    else:
        color = "#0e9f6e"   # green

    # Format amount in Indian notation
    amt_int = int(amt)
    if amt_int >= 100000:
        amt_fmt = f"₹{amt_int // 100000},{(amt_int % 100000) // 1000:02d},{amt_int % 1000:03d}"
    elif amt_int >= 1000:
        amt_fmt = f"₹{amt_int // 1000},{amt_int % 1000:03d}"
    else:
        amt_fmt = f"₹{amt_int}"

    tooltip = f"{name}<br>{amt_fmt}<br>{state_raw} – {pin}<br>Overdue: {overdue}d"

    all_latlngs.append((lat, lng))
    markers_js += (
        f"L.circleMarker([{lat},{lng}], "
        f"{{radius:{radius},color:'{color}',fillColor:'{color}',"
        f"fillOpacity:0.75,weight:2}})"
        f".bindTooltip('{tooltip}', {{sticky:true, direction:'top', offset:[0,-4]}})"
        f".addTo(map);\n"
    )

# ── Build fitBounds JS from all marker positions ───────────────────────────
bounds_array_js = "[" + ",".join(f"[{lat},{lng}]" for lat, lng in all_latlngs) + "]"

# ── Assemble the full inline map snippet ──────────────────────────────────
leaflet_css = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
leaflet_js  = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"

inline_map = f"""<link rel="stylesheet" href="{leaflet_css}" />
<script src="{leaflet_js}"></script>
<style>
  /* Override Leaflet's default divIcon box — must target leaflet-div-icon */
  .leaflet-div-icon.place-label-icon {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
  }}
  .place-label {{
    background: rgba(255,255,255,0.90);
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    color: #1e293b;
    white-space: nowrap;
    box-shadow: 0 1px 4px rgba(0,0,0,0.18);
    pointer-events: none;
    display: inline-block;
  }}
</style>
<div id="leaflet-map" style="width:100%;height:500px;border-radius:12px;"></div>
<script>
  // Initialise with a fallback view; fitBounds below overrides it
  var map = L.map('leaflet-map').setView([24.5937, 73.6855], 13);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '© OpenStreetMap contributors',
    maxZoom: 18
  }}).addTo(map);

  // Legend
  var legend = L.control({{position: 'bottomright'}});
  legend.onAdd = function() {{
    var d = L.DomUtil.create('div', '');
    d.style.cssText = 'background:white;padding:8px 12px;border-radius:6px;font-size:12px;line-height:1.8;box-shadow:0 1px 5px rgba(0,0,0,0.2)';
    d.innerHTML = '<b>Overdue</b><br>'
      + '<span style="color:#e02424">●</span> &gt;90 days<br>'
      + '<span style="color:#ff5a1f">●</span> 31–90 days<br>'
      + '<span style="color:#0e9f6e">●</span> Current / &lt;30 days';
    return d;
  }};
  legend.addTo(map);

  // Circle markers (hover tooltips) — jittered so all are visible
  {markers_js}

  // Permanent place-name labels
  {label_markers_js}

  // Auto-fit zoom to show all markers with padding
  var allPoints = {bounds_array_js};
  if (allPoints.length > 0) {{
    map.fitBounds(L.latLngBounds(allPoints), {{ padding: [30, 30] }});
  }}
</script>"""

# ── Replace map-placeholder in dashboard.html ─────────────────────────────
html = (output / "dashboard.html").read_text(encoding="utf-8")

# Strategy: try each known pattern in priority order.
# Pattern 1: existing inline Leaflet block from a previous fix_map.py run
#   Starts with <link...leaflet.../> OR <style>...(place-label)...</style>
#   Ends with the closing </script> of the Leaflet map init block.
# We anchor on <div id="leaflet-map" which is always present after fix_map runs.
leaflet_rerun_pattern = re.compile(
    r'(?:<link[^>]*leaflet[^>]*/>|<style>\s*/\*[^*]*(?:Leaflet|leaflet|place-label)[^*]*\*/).*?'
    r'<div id="leaflet-map"[^>]*>.*?</div>\s*<script>.*?</script>',
    re.DOTALL | re.IGNORECASE,
)
if leaflet_rerun_pattern.search(html):
    html = leaflet_rerun_pattern.sub(inline_map, html, count=1)
    print("  [OK] Existing Leaflet map block replaced (re-run).")
else:
    # Pattern 2: map-placeholder div left by dashboard agent
    placeholder_pattern = re.compile(
        r'<div\s+id=["\']map-placeholder["\'][^>]*>.*?</div>',
        re.DOTALL | re.IGNORECASE,
    )
    if placeholder_pattern.search(html):
        html = placeholder_pattern.sub(inline_map, html, count=1)
        print("  [OK] map-placeholder replaced with inline Leaflet map.")
    else:
        # Pattern 3: iframe map from very old pipeline runs
        html = re.sub(
            r'<iframe[^>]*id=["\']mapFrame["\'][^>]*>.*?</iframe>',
            inline_map, html, flags=re.DOTALL,
        )
        print("  [OK] Inline Leaflet map injected (iframe fallback).")

(output / "dashboard.html").write_text(html, encoding="utf-8")
print("  --> Open output/dashboard.html in your browser.")
