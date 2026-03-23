"""
patch_table.py  —  Upgrades dashboard.html with:
  1. Parties grouped by aging bucket (1-30 / 31-60 / 61-90 / >90) using max_days
  2. Clickable rows that expand to show individual bill details
"""

import json, re, pathlib

OUTPUT   = pathlib.Path("output")
REC_JSON = OUTPUT / "receivables.json"
HTML_IN  = OUTPUT / "dashboard.html"

# ── 1. Load receivables data ─────────────────────────────────────────────
rec  = json.loads(REC_JSON.read_text(encoding="utf-8"))
rec_parties = {p["name"]: p["bills"] for p in rec["parties"]}   # name → [bills]

# ── 2. Read dashboard ─────────────────────────────────────────────────────
html = HTML_IN.read_text(encoding="utf-8")

# ── 3. Inject bills into PARTIES JS constant ─────────────────────────────
# Build new PARTIES array as JS with bills embedded
def js_bills(bills):
    rows = []
    for b in bills:
        rows.append(
            f'{{ref:{json.dumps(b["bill_ref"])},date:{json.dumps(b["bill_date"])},'
            f'due:{json.dumps(b["due_date"])},amt:{b["outstanding"]},overdue:{int(b["days_overdue"])}}}'
        )
    return "[" + ",".join(rows) + "]"

new_parties_lines = ["const PARTIES = ["]
for m in re.finditer(r'\{ name: (".*?"),\s+state: (".*?"),\s+pincode: (".*?"),\s+outstanding: ([\d.]+),\s+bill_count: (\d+),\s+max_days: (\d+) \}', html):
    name_js, state_js, pin_js, outstanding, bill_count, max_days = m.groups()
    name_py = json.loads(name_js)
    bills   = rec_parties.get(name_py, [])
    bills_js = js_bills(bills)
    new_parties_lines.append(
        f'  {{ name:{name_js}, state:{state_js}, pincode:{pin_js}, '
        f'outstanding:{outstanding}, bill_count:{bill_count}, max_days:{max_days}, bills:{bills_js} }},'
    )
new_parties_lines.append("];")
new_parties_block = "\n".join(new_parties_lines)

html = re.sub(
    r'const PARTIES = \[.*?\];',
    new_parties_block,
    html,
    flags=re.DOTALL
)

# ── 4. Add CSS for group headers + bill detail rows ───────────────────────
extra_css = """
    /* ── Aging group header rows ─────────────────────────────────────── */
    .group-header td {
      background: linear-gradient(90deg, #1e293b 0%, #0f172a 100%) !important;
      color: #f8fafc;
      font-size: .72rem;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
      padding: 8px 14px;
      border-bottom: 2px solid rgba(255,255,255,.08) !important;
      cursor: default;
    }
    .group-header:hover td { background: linear-gradient(90deg, #1e293b 0%, #0f172a 100%) !important; }
    .group-badge {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 10px;
      font-size: .7rem;
      font-weight: 700;
      margin-right: 8px;
    }
    .group-summary { color: #94a3b8; font-weight: 400; font-size: .7rem; margin-left: 10px; }

    /* ── Expand toggle ───────────────────────────────────────────────── */
    .expand-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border-radius: 4px;
      background: rgba(99,102,241,.15);
      border: 1px solid rgba(99,102,241,.3);
      color: #818cf8;
      font-size: .65rem;
      cursor: pointer;
      transition: all .15s;
      user-select: none;
      flex-shrink: 0;
    }
    .expand-btn:hover { background: rgba(99,102,241,.3); }
    .expand-btn.open  { background: rgba(99,102,241,.35); transform: rotate(90deg); }

    /* ── Bill detail rows ────────────────────────────────────────────── */
    .bill-row { display: none; }
    .bill-row.visible { display: table-row; }
    .bill-row td {
      background: #f1f5ff !important;
      border-bottom: 1px solid #dbeafe !important;
      font-size: .77rem;
      padding: 7px 12px;
    }
    .bill-row:hover td { background: #e0e7ff !important; }
    .bill-detail-inner {
      display: flex;
      align-items: center;
      gap: 0;
    }
    .bill-table-wrap {
      width: 100%;
      margin-left: 28px;
    }
    .bill-table {
      width: 100%;
      border-collapse: collapse;
      font-size: .76rem;
    }
    .bill-table th {
      color: #6366f1;
      font-size: .68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .05em;
      padding: 4px 10px;
      text-align: left;
      border-bottom: 1px solid #c7d2fe;
      background: transparent;
    }
    .bill-table th.num { text-align: right; }
    .bill-table td { padding: 5px 10px; color: #334155; border-bottom: 1px solid #e0e7ff; }
    .bill-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .bill-table tr:last-child td { border-bottom: none; }
    .overdue-chip {
      display: inline-block;
      padding: 1px 8px;
      border-radius: 8px;
      font-size: .7rem;
      font-weight: 600;
    }
"""

html = html.replace("    /* ── Responsive nudges", extra_css + "\n    /* ── Responsive nudges")

# ── 5. Update table <thead> – add expand column ───────────────────────────
old_thead = """        <thead>
          <tr>
            <th onclick="sortTable(0)" title="Party Name">#&nbsp;&nbsp;Party Name</th>
            <th onclick="sortTable(1)">State</th>
            <th onclick="sortTable(2)">Pincode</th>
            <th onclick="sortTable(3)" class="num">Outstanding (₹)</th>
            <th onclick="sortTable(4)" class="num">Bills</th>
            <th onclick="sortTable(5)" class="num">Max Days</th>
            <th onclick="sortTable(6)">Aging Bucket</th>
          </tr>
        </thead>"""

new_thead = """        <thead>
          <tr>
            <th style="width:32px;"></th>
            <th onclick="sortTable(0)" title="Party Name">#&nbsp;&nbsp;Party Name</th>
            <th onclick="sortTable(1)">State</th>
            <th onclick="sortTable(2)">Pincode</th>
            <th onclick="sortTable(3)" class="num">Outstanding (₹)</th>
            <th onclick="sortTable(4)" class="num">Bills</th>
            <th onclick="sortTable(5)" class="num">Max Days</th>
            <th onclick="sortTable(6)">Aging Bucket</th>
          </tr>
        </thead>"""

html = html.replace(old_thead, new_thead)

# ── 6. Replace renderTable() with grouped + expandable version ────────────
old_render_start = "function renderTable() {"
old_render_end   = "\nfunction sortTable(colIdx) {"

old_render_block = html[html.index(old_render_start) : html.index(old_render_end)]

new_render_block = r"""function renderTable() {
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = '';

  const filtered = tableData.filter(function(p) {
    if (!searchTerm) return true;
    const q = searchTerm.toLowerCase();
    return (
      p.name.toLowerCase().includes(q) ||
      (p.state || '').toLowerCase().includes(q) ||
      (p.pincode || '').toLowerCase().includes(q)
    );
  });

  document.getElementById('tableInfo').textContent =
    'Showing ' + filtered.length + ' of ' + PARTY_COUNT + ' parties';

  // Bucket definitions
  const BUCKETS = [
    { key: '>90',   label: '> 90 Days',   min: 91,  max: Infinity, badgeCls: 'badge-red',    rowCls: 'row-red',    badgeBg: '#fde8e8', badgeColor: '#991b1b' },
    { key: '61-90', label: '61–90 Days',  min: 61,  max: 90,       badgeCls: 'badge-orange',  rowCls: 'row-orange', badgeBg: '#fff3e0', badgeColor: '#9a3412' },
    { key: '31-60', label: '31–60 Days',  min: 31,  max: 60,       badgeCls: 'badge-yellow',  rowCls: 'row-orange', badgeBg: '#fef9c3', badgeColor: '#854d0e' },
    { key: '1-30',  label: '1–30 Days',   min: 1,   max: 30,       badgeCls: 'badge-green',   rowCls: '',           badgeBg: '#dcfce7', badgeColor: '#065f46' },
  ];

  let globalIdx = 0;

  BUCKETS.forEach(function(bucket) {
    const group = filtered.filter(function(p) {
      return p.max_days >= bucket.min && p.max_days <= bucket.max;
    });
    if (group.length === 0) return;

    // Group total
    const groupTotal = group.reduce(function(s, p) { return s + p.outstanding; }, 0);

    // Group header row
    const ghTr = document.createElement('tr');
    ghTr.className = 'group-header';
    ghTr.innerHTML = `<td colspan="8">
      <span class="group-badge" style="background:${bucket.badgeBg};color:${bucket.badgeColor};">${bucket.label}</span>
      ${group.length} ${group.length === 1 ? 'party' : 'parties'}
      <span class="group-summary">· Total: ${fINR(groupTotal)}</span>
    </td>`;
    tbody.appendChild(ghTr);

    group.forEach(function(p) {
      globalIdx++;
      const rowId   = 'party-' + globalIdx;
      const billsId = 'bills-' + globalIdx;
      const bucket2 = agingBucket(p.max_days);
      const hasBills = p.bills && p.bills.length > 0;

      // Party row
      const tr = document.createElement('tr');
      tr.className = bucket2.rowCls;
      tr.style.cursor = hasBills ? 'pointer' : 'default';
      tr.id = rowId;
      tr.setAttribute('data-bills-id', billsId);
      tr.onclick = hasBills ? function() { toggleBills(rowId, billsId); } : null;

      tr.innerHTML = `
        <td style="padding-left:12px;">
          ${hasBills
            ? `<span class="expand-btn" id="btn-${rowId}" title="Show bills">▶</span>`
            : `<span style="color:#e2e8f0;font-size:.6rem;">—</span>`}
        </td>
        <td>
          <div style="display:flex;align-items:center;gap:8px;">
            <span style="color:#94a3b8;font-size:.72rem;font-weight:600;min-width:20px;">${globalIdx}</span>
            <span class="party-name" title="${p.name}">${p.name}</span>
          </div>
        </td>
        <td>${p.state || '<span style="color:#cbd5e1;">—</span>'}</td>
        <td>${p.pincode || '<span style="color:#cbd5e1;">—</span>'}</td>
        <td class="num amount">${fINR(p.outstanding)}</td>
        <td class="num">${p.bill_count}</td>
        <td class="num">
          <span class="days-pill" style="${p.max_days > 90 ? 'background:#fee2e2;color:#991b1b;' : p.max_days > 60 ? 'background:#fee2e2;color:#991b1b;' : p.max_days > 30 ? 'background:#ffedd5;color:#9a3412;' : 'background:#d1fae5;color:#065f46;'}">${p.max_days}d</span>
        </td>
        <td><span class="aging-badge ${bucket2.cls}">${bucket2.label}</span></td>
      `;
      tbody.appendChild(tr);

      // Bill detail row (hidden by default)
      if (hasBills) {
        const billTr = document.createElement('tr');
        billTr.className = 'bill-row';
        billTr.id = billsId;

        const billRows = p.bills.map(function(b) {
          const chipStyle = b.overdue > 90
            ? 'background:#fee2e2;color:#991b1b;'
            : b.overdue > 60
            ? 'background:#fee2e2;color:#991b1b;'
            : b.overdue > 30
            ? 'background:#ffedd5;color:#9a3412;'
            : 'background:#d1fae5;color:#065f46;';
          return `<tr>
            <td>${b.date}</td>
            <td>${b.ref}</td>
            <td class="num">${fINR(b.amt)}</td>
            <td>${b.due}</td>
            <td class="num"><span class="overdue-chip" style="${chipStyle}">${b.overdue}d</span></td>
          </tr>`;
        }).join('');

        billTr.innerHTML = `<td colspan="8">
          <div class="bill-table-wrap">
            <table class="bill-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Ref. No</th>
                  <th class="num">Pending Amount</th>
                  <th>Due On</th>
                  <th class="num">Overdue by Days</th>
                </tr>
              </thead>
              <tbody>${billRows}</tbody>
            </table>
          </div>
        </td>`;
        tbody.appendChild(billTr);
      }
    });
  });
}

function toggleBills(rowId, billsId) {
  const billRow = document.getElementById(billsId);
  const btn     = document.getElementById('btn-' + rowId);
  if (!billRow) return;
  const isOpen = billRow.classList.contains('visible');
  billRow.classList.toggle('visible', !isOpen);
  if (btn) btn.classList.toggle('open', !isOpen);
}

"""

html = html.replace(old_render_block, new_render_block)

# ── 7. Write updated HTML ─────────────────────────────────────────────────
HTML_IN.write_text(html, encoding="utf-8")
print("  ✅ dashboard.html patched — grouped aging + expandable bill rows.")
print("  👉 Run:  start output\\dashboard.html")
