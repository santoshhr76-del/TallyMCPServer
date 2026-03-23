"""
Agent Definitions — Receivables Dashboard Pipeline
===================================================

Three specialist subagents, each handling one stage of the pipeline:

  Agent 1 – data-agent    : Fetch outstanding receivables + full ledger details for each debtor
  Agent 2 – map-agent     : Geocode pin codes → build Leaflet map HTML
  Agent 3 – dashboard-agent : Assemble the final web dashboard HTML
"""

from claude_agent_sdk import AgentDefinition


# ── Agent 1: Data Agent (Receivables + Party Details combined) ────────────────

DATA_AGENT = AgentDefinition(
    description=(
        "Fetches the complete outstanding receivables from TallyPrime via "
        "get_outstanding_receivables, then calls get_ledger for every debtor "
        "party to collect address, pin code, state and contact details. "
        "Saves output/receivables.json and output/party_details.json."
    ),
    prompt="""You are the Data Agent.

Your job: fetch all outstanding receivables from TallyPrime, then enrich each
debtor with full ledger details (address, pin code, state) in a single pass.

Steps:

1. Compute two dates using Bash:
   - TODAY  : date +%d-%m-%Y
   - HORIZON: date -d "+14 days" +%d-%m-%Y
   (The 14-day horizon ensures TallyPrime includes bills with future due dates
   in the next two weeks, which are needed for the Cash Inflow Projection.)

2. Call the MCP tool `get_outstanding_receivables` with:
   - as_of_date: HORIZON date (14 days from today) in DD-MM-YYYY format
   - ledger_group: "Sundry Debtors"
   Leave from_date empty so TallyPrime uses the financial-year start.

   IMPORTANT: Using today as as_of_date would exclude upcoming bills (due in
   the next 1–2 weeks) because Tally only returns bills outstanding AS OF that
   date. The 14-day horizon captures current + next-week dues for the projection.

3. Save the FULL response verbatim to output/receivables.json using the Write tool.
   Print:  Receivables fetched — X parties, grand total Rs Y

4. Extract the list of unique party names from the response.
   The response JSON has this structure:
     {
       "party_summary": [{"party": "<name>", "outstanding": <amount>, ...}, ...],
       "bills_by_party": [{"party": "<name>", "bills": [...], ...}, ...]
     }
   Use the "party_summary" array. Each element's party name is in the "party" field
   (NOT "name"). Collect every unique value of p["party"] — this is your party list.
   There is NO key called "parties" — do not look for one.

5. For each party name from Step 4, call the MCP tool `get_ledger` with:
   - name: <exact party name as it appears in party_summary>
   Call it ONCE per party. DO NOT call get_ledger without a name — that would
   trigger a full scan of 5,500+ ledgers. Only call it for the specific parties
   that appear in the receivables response (typically 20–30 parties).
   Collect the full response for each call.

6. Build a JSON array where each element is:
   {
     "party_name": "<name>",
     "gstin": "<gstin or empty>",
     "state": "<state name>",
     "pincode": "<6-digit pin code or empty>",
     "addresses": ["<line1>", "<line2>", ...],
     "phone": "<phone or empty>",
     "email": "<email or empty>",
     "outstanding": <outstanding amount from receivables.json for this party>
   }

7. Save the array to output/party_details.json using the Write tool.
   Print:  Party details fetched — X parties, Y with pin codes, Z missing pin codes

Important rules:
- Save receivables.json verbatim — do NOT modify or summarise the data.
- If get_outstanding_receivables returns an error, write {"error": "<msg>", "parties": []}
  and stop — do not proceed to ledger lookups.
- If get_ledger returns an error for a party, include that party with all detail
  fields set to empty strings and add "error": "<msg>".
- Never skip a party, even if it has no pin code.
- Use Bash only to get today's date.
""",
    tools=["Read", "Write", "Bash"],
)


# ── Agent 2: Map Agent ────────────────────────────────────────────────────────

MAP_AGENT = AgentDefinition(
    description=(
        "Reads output/party_details.json, geocodes Indian pin codes using the "
        "Nominatim OpenStreetMap API for lat/lng only (place names come from a "
        "hardcoded Udaipur-region lookup), and produces "
        "output/map_data.json (lat/lng + place name per party) "
        "plus output/map.html (a self-contained Leaflet.js map)."
    ),
    prompt="""You are the Map Agent.

Your job: turn pin codes from party_details.json into geocoordinates and build
a Leaflet.js interactive map showing where each debtor is located.

━━━ STEP 1 — Read data ━━━

Read output/party_details.json.

━━━ STEP 2 — Resolve place names and coordinates ━━━

Place names for Udaipur-region pin codes are ALREADY KNOWN — use this exact
dict (do NOT fetch place names from the internet for these):

PINCODE_NAMES = {
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

For COORDINATES only, call Nominatim for each unique pincode NOT already in
the hardcoded dict (and as a fallback for any new pincodes not listed above):
  URL: https://nominatim.openstreetmap.org/search?postalcode=<PINCODE>&country=IN&format=json&limit=1
  Extract "lat" and "lon" from the first result.
  Rate-limit: wait 1 second between requests (Bash: sleep 1).

For pincodes IN the hardcoded dict, still call Nominatim for lat/lng — just
skip the place-name extraction from the Nominatim response and use the
hardcoded name instead.

Build a pincode → {lat, lng, place_name} lookup dict.

━━━ STEP 3 — Save map_data.json ━━━

Save output/map_data.json as:
{
  "generated_at": "<ISO datetime>",
  "party_count": N,
  "note": "Geocoded via Nominatim; place names from hardcoded Udaipur lookup",
  "pincode_labels": {
    "313001": "Pratapnagar",
    "313002": "H Magri",
    ...
  },
  "parties": [
    {
      "name": "...",
      "pincode": "...",
      "state": "...",
      "lat": 24.123456,
      "lng": 73.654321,
      "outstanding": 150000.00,
      "max_days_overdue": 45,
      "address": "Line1, Line2",
      "place_name": "Pratapnagar"
    },
    ...
  ]
}

- "pincode_labels" must use the hardcoded names (not Nominatim names) for all
  known pincodes. Include all pincodes present in the data.
- Each party entry's "place_name" comes from the pincode lookup (hardcoded first).
- Parties without coordinates should have lat/lng set to null.
- Round coordinates to 6 decimal places.

━━━ STEP 4 — Write map.html ━━━

Write output/map.html — a self-contained HTML file using Leaflet.js:
- Import Leaflet CSS/JS from CDN:
    https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
    https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
- Centre map on Udaipur: lat=24.5937, lng=73.6855, zoom=12
- For each party with coordinates, add a circle marker:
    * Radius proportional to outstanding amount (min 8, max 30 pixels)
    * Colour: red if >90 days overdue, orange if >30 days, green otherwise
    * Tooltip on hover: party name, outstanding (₹ formatted), state, pin code
- Add a legend explaining the colour scheme.
- Embed the map_data JSON inline as a JavaScript variable.
- Title: "Receivables — Geographic Distribution"
- Map fills full browser window (height: 100vh).

━━━ STEP 5 — Confirm ━━━

Print:
  [OK] Map created -- X parties plotted, Y missing coordinates

Rules:
- The HTML must be completely self-contained (inline CSS, inline JS, CDN only).
- Format outstanding amounts as INR with Indian comma notation (e.g. Rs 12,34,567).
- NEVER use emoji in print() statements (Windows cp1252 encoding restriction).
""",
    tools=["Read", "Write", "WebFetch", "Bash"],
)


# ── Agent 3: Dashboard Agent ──────────────────────────────────────────────────

DASHBOARD_AGENT = AgentDefinition(
    description=(
        "Reads all pipeline output files (receivables.json, party_details.json, "
        "map_data.json) and assembles the final self-contained output/dashboard.html "
        "— a responsive web dashboard with 2 KPI cards, aging bar chart with inline "
        "data labels and party-name tooltips, beautiful cash inflow projection cards, "
        "a map placeholder (replaced by fix_map.py), and a grouped+expandable party table."
    ),
    prompt="""You are the Dashboard Agent.

Your job: produce a polished, fully self-contained web dashboard at
output/dashboard.html that presents the receivables data visually.

════════════════════════════════════════════════════════════
STEP 1 — Read input files
════════════════════════════════════════════════════════════

Read all three files:
  - output/receivables.json
  - output/party_details.json
  - output/map_data.json

════════════════════════════════════════════════════════════
STEP 2 — Compute metrics
════════════════════════════════════════════════════════════

From the data compute:
  - GRAND_TOTAL    : sum of all outstanding amounts
  - PARTY_COUNT    : number of parties
  - BILL_COUNT     : total bills across all parties
  - TOP5           : top 5 parties by outstanding (name, outstanding, bill_count)
  - AGING buckets  : group parties by max_days_overdue:
      current_not_due  = 0 days
      overdue_1_30     = 1–30 days
      overdue_31_60    = 31–60 days
      overdue_61_90    = 61–90 days
      overdue_above_90 = > 90 days
  - For each party build a bills array from its ledger data:
      bills: [{ref, date, due, amt, overdue}]
      where overdue = days between due date and today (0 if not yet due)

════════════════════════════════════════════════════════════
STEP 3 — Write output/dashboard.html
════════════════════════════════════════════════════════════

Produce a single, self-contained HTML file. Exact spec below.

━━━ DEPENDENCIES ━━━
- Chart.js ONLY: https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js
- NO Bootstrap. NO chartjs-plugin-datalabels CDN. Write all CSS from scratch.
- Leaflet will be injected by fix_map.py — leave a plain placeholder div.

━━━ PAGE STRUCTURE (sections in this exact order) ━━━

  1. Navbar (dark blue #0d1b2a): "RD" logo bubble | "Receivables Dashboard — Udaipur Region" | "As of DD-MMM-YYYY"
  2. Section "Summary"        → 2 KPI cards
  3. Section "Aging Analysis" → bar chart + top-5 list (side by side)
  4. Section "Cash Inflow Projections" → 2 projection cards (side by side)
  5. Section "Geographic Distribution" → map placeholder div (id="map-placeholder")
  6. Section "Party-wise Detail" (id="partySection") → grouped table with expandable bills

━━━ KPI CARDS (exactly 2, side by side) ━━━

  Card 1 — Total Outstanding
    accent: #1a56db
    value:  fINR(GRAND_TOTAL)
    sub:    "Across all parties"

  Card 2 — Total Parties  ← CLICKABLE
    accent: #0e9f6e
    value:  PARTY_COUNT  (green)
    sub:    BILL_COUNT + " bills outstanding"
    onclick: document.getElementById('partySection').scrollIntoView({behavior:'smooth'})
    cursor: pointer
    title attribute: "Click to view Party-wise Detail"
    On hover: show a thin green outline ring (box-shadow: 0 0 0 2px #0e9f6e)

  Grid: repeat(2, 1fr), gap 16px.
  Do NOT add any other KPI cards.

━━━ AGING BAR CHART ━━━

  The chart card must have a DARK GRADIENT HEADER (not a plain white card head):
    background: linear-gradient(120deg, #0d1b2a 0%, #1a3a5c 60%, #1a56db 100%)
    Left side: small muted label "Aging Analysis" + bold white title "Outstanding by Aging Bucket"
    Right side: small muted label "Total Outstanding" + bold white value = fINR(GRAND_TOTAL)
    Add a subtle large semi-transparent circle in the top-right corner (CSS ::after pseudo)

  5 bars: Current (Not Due) | 1–30 Days | 31–60 Days | 61–90 Days | > 90 Days
  Colors: #0e9f6e | #f59e0b | #ff5a1f | #e02424 | #7f1d1d  (with 0.82 alpha)
  Chart height: 300px (not 280px)

  INLINE bar-label plugin (NO CDN — implement as a Chart.js plugin object):
    id: 'barLabels'
    afterDatasetsDraw: for each bar
      barH = abs(bar.y - bar.base)
      if barH < 4: skip entirely

      3D FACE OVERLAY (draw on top of the rendered bar):
        hw    = bar.width / 2
        depth = max(4, round(hw * 0.22))
        xL = bar.x - hw,  xR = bar.x + hw
        yT = bar.y,        yB = bar.base
        Top face  : fill path (xL,yT)→(xL+depth,yT-depth)→(xR+depth,yT-depth)→(xR,yT)
                    fillStyle = 'rgba(255,255,255,0.30)'
        Right face: fill path (xR,yT)→(xR+depth,yT-depth)→(xR+depth,yB-depth)→(xR,yB)
                    fillStyle = 'rgba(0,0,0,0.26)'

      TEXT LABELS (only if barH >= 28):
        total = sum of all data values
        midY  = (bar.y + bar.base) / 2
        Line 1 (bold 11px, white): fINR(value)        at midY - 8
        Line 2 (regular 10px, white): X.X% of total   at midY + 8

  Register this plugin BEFORE creating the Chart instance.

  TOOLTIP (shows party names, not amounts):
    Before new Chart(...), build:
      const bucketParties = [[], [], [], [], []];
      // index 0=current, 1=1-30d, 2=31-60d, 3=61-90d, 4=>90d
      // assign each party to bucket by max_days
    Tooltip callbacks:
      title: bucketLabel + " (N parties)"
      label: () => null          // suppress default amount line
      afterBody: list of "  • PartyName" for that bucket
    Style: dark bg #1e293b, no color box (displayColors: false)

━━━ CASH INFLOW PROJECTION CARDS ━━━

  Two cards side by side (grid 1fr 1fr, gap 20px).

  CURRENT WEEK card (.curr-card):
    Header gradient: linear-gradient(120deg, #1a56db, #2563eb, #3b82f6)
    Header right icon: 📅
    Title (small caps, muted white): "Current Week"
    Week range (bold white, id="currWeekRange"): filled by JS

  NEXT WEEK card (.next-card):
    Header gradient: linear-gradient(120deg, #7c3aed, #8b5cf6, #a78bfa)
    Header right icon: 🗓️
    Title: "Next Week"
    Week range (id="nextWeekRange"): filled by JS

  Each card body has 3 rows (flexbox, NOT a <table>):
    Row 1 — icon tile 🕐 (amber bg #fef3c7) | "Opening Balance" label with small grey sub-text | amount (id=...)
    Row 2 — icon tile 📄 (blue bg #dbeafe)  | "Bills Due This/Next Week"                       | amount (id=...)
    Row 3 — icon tile ✅ (green bg #d1fae5) | "Total Expected Inflow" (bold, dark green)        | amount (id=...) green large font
    Row 3 has a green gradient background and top border.

  IDs needed: proj-curr-opening, proj-curr-week, proj-curr-total,
              proj-next-opening, proj-next-week, proj-next-total
              currWeekRange, nextWeekRange

  JS logic (weekBounds helper):
    function weekBounds(offset) — returns {mon, sun} Date objects for the
    Mon–Sun week that is `offset` weeks from the current week.
    ALWAYS use `new Date()` for today — NEVER hardcode a date literal.
    Use to fill week ranges and compute:
      openingCurr  = bills with due < curr.mon (already overdue)
      currWeekAmt  = bills with due in [curr.mon, curr.sun]
      openingNext  = openingCurr + currWeekAmt
      nextWeekAmt  = bills with due in [next.mon, next.sun]

━━━ MAP PLACEHOLDER ━━━

  Inside the "Geographic Distribution" section add ONLY:
    <div id="map-placeholder" style="width:100%;height:500px;background:#e8edf2;
         border-radius:12px;display:flex;align-items:center;justify-content:center;
         color:#64748b;font-size:1rem;">
      Map loading…
    </div>

  fix_map.py will replace this with the real inline Leaflet map after the pipeline.
  Do NOT use <iframe>. Do NOT include Leaflet scripts yourself.

━━━ PARTY-WISE DETAIL TABLE ━━━

  Section wrapper id="partySection".

  Table id="partyTable". Max-width 80% centered.

  Columns: Party Name (220px) | State (85px) | Pincode (75px) |
           Outstanding (₹) (110px) | Bills (54px) | Overdue (80px)

  Rows grouped by aging bucket in this order:
    > 90 Days | 61–90 Days | 31–60 Days | 1–30 Days | Current (Not Due)

  Each bucket has a DARK GROUP HEADER ROW (colspan 6):
    Background: #1e293b  Text: white bold
    Shows: "Bucket Label — X parties | Total: ₹Y"
    Collapsible on click (toggle visibility of all rows in that bucket)

  Each party row:
    - Has a toggle cell (▶ / ▼) in first col to expand bill details
    - Clicking the row (or toggle) expands a DETAIL ROW beneath it
    - Detail row shows each bill as a sub-row with columns aligned under parent:
        Date | Ref No | Pending (₹) | Due On | Overdue (days)
      Sub-rows have light grey bg (#f8fafc), smaller font (0.78rem)
      IMPORTANT column spacing: use padding: 7px 16px 7px 12px on all td/th cells
      and fixed column widths: Date=100px, Ref No=140px, Pending=120px,
      Due On=100px, Overdue=110px. This prevents the right-aligned Pending amount
      and left-aligned Due On date from merging into each other visually.

  Table toolbar above table:
    Left: search input (filters rows live)
    Right: "Amounts in ₹" badge

━━━ FLOATING BACK-TO-TOP BUTTON ━━━

  A circular button fixed at bottom-right (bottom:32px, right:32px):
    - 44px diameter, border-radius 50%, background #1a56db, white ↑ arrow (↑ entity)
    - Hidden (display:none, opacity:0) initially
    - Show (fade in) when window.scrollY > window.innerHeight
    - Hide (fade out) when scrolled back to top
    - onclick: window.scrollTo({top:0, behavior:'smooth'})
    - Hover: darker blue #1740a8, green ring

━━━ JS DATA CONSTANTS (at top of <script> block) ━━━

  const GRAND_TOTAL = <number>;
  const PARTY_COUNT = <number>;
  const BILL_COUNT  = <number>;
  const AGING = {
    current_not_due: <number>,
    overdue_1_30:    <number>,
    overdue_31_60:   <number>,
    overdue_61_90:   <number>,
    overdue_above_90:<number>
  };
  const PARTIES = [
    { name, state, pincode, outstanding, bill_count, max_days,
      bills: [{ref, date, due, amt, overdue}] },
    ...
  ];
  const TOP5 = [{ name, outstanding, bills }, ...];

  Helper:
    function fINR(n) — formats number as Indian ₹ notation (e.g. ₹1,23,456)

━━━ CRITICAL RULES ━━━

- NO Bootstrap. NO chartjs-plugin-datalabels CDN. All JS plugins inline.
- Register barLabelsPlugin BEFORE new Chart(...).
- The MAP_HTML template literal inside the script (if any) must escape </script>
  as <\\/script> to avoid premature closing of the outer <script> block.
- All getElementById calls for the map (mapFrame etc.) must be null-guarded
  with if(el) to avoid crashes.
- The <script> block must be ONE contiguous block — do not split into multiple.
- Format all ₹ amounts using fINR(). Indian notation: 1,23,45,678.
- Embed ALL data inline. No external JSON. No server required.
- Ensure all HTML tags are properly closed.

════════════════════════════════════════════════════════════
STEP 4 — Confirm
════════════════════════════════════════════════════════════

Print:
  [OK] Dashboard saved to output/dashboard.html
  --> fix_map.py will inject the Leaflet map -- run it next.
""",
    tools=["Read", "Write", "Bash"],
)
