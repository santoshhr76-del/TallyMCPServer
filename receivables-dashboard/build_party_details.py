"""
Parse the full company masters XML and extract address/contact details
for every party in receivables.json -> write party_details.json.
"""
import json
import re
import os
import xml.etree.ElementTree as ET

BASE    = r"C:\Users\Dell\Documents\TallyMCPServer\receivables-dashboard"
MASTERS = os.path.join(BASE, "masters_all.xml")
OUT_DIR = os.path.join(BASE, "output")
RECV    = os.path.join(OUT_DIR, "receivables.json")
OUT     = os.path.join(OUT_DIR, "party_details.json")

# ── helpers ────────────────────────────────────────────────────────────────────

# Strip literal control chars AND illegal numeric character references (&#4; etc.)
_LITERAL_CTRL  = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHAR_REF      = re.compile(r"&#(?:x([0-9A-Fa-f]+)|([0-9]+));")

def _is_valid_xml_cp(cp: int) -> bool:
    return (cp in (0x9, 0xA, 0xD)
            or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD
            or 0x10000 <= cp <= 0x10FFFF)

def _sanitize(text: str) -> str:
    def _repl(m: re.Match) -> str:
        h, d = m.group(1), m.group(2)
        cp = int(h, 16) if h else int(d)
        return m.group(0) if _is_valid_xml_cp(cp) else ""
    text = _CHAR_REF.sub(_repl, text)
    text = _LITERAL_CTRL.sub("", text)
    return text

def _find_text(el, tag: str) -> str:
    if el is None:
        return ""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""

# ── load party list ────────────────────────────────────────────────────────────

with open(RECV, encoding="utf-8") as f:
    recv = json.load(f)

party_outstanding = {p["party_name"]: p["outstanding"] for p in recv["parties"]}
party_names = list(party_outstanding.keys())
print(f"Need details for {len(party_names)} parties.")

# ── extract each ledger block from the masters XML using regex ─────────────────

print("Loading masters XML ...", end=" ", flush=True)
with open(MASTERS, encoding="utf-8", errors="replace") as f:
    masters_xml = f.read()
print("done.")


def _xml_attr_name(name: str) -> str:
    """Convert a party name to its XML-attribute-escaped form (&  -> &amp;)."""
    return name.replace("&", "&amp;")


def extract_ledger_block(xml_text: str, ledger_name: str) -> str | None:
    """Return the raw XML text of a single <LEDGER NAME="...">...</LEDGER> block."""
    attr_name = _xml_attr_name(ledger_name)
    safe      = re.escape(attr_name)
    pattern   = rf'(<LEDGER NAME="{safe}"[^>]*>.*?</LEDGER>)'
    m = re.search(pattern, xml_text, re.DOTALL)
    return m.group(1) if m else None


def parse_ledger_block(block_xml: str) -> dict:
    """Parse address/contact fields from a sanitized LEDGER XML block."""
    clean = _sanitize(block_xml)
    try:
        ledger = ET.fromstring(clean)
    except ET.ParseError as exc:
        return {"error": f"XML parse error: {exc}"}

    mailing = ledger.find("LEDMAILINGDETAILS.LIST")

    # Address lines
    addresses: list[str] = []
    if mailing is not None:
        addr_list = mailing.find("ADDRESS.LIST")
        if addr_list is not None:
            addresses = [a.text.strip() for a in addr_list.findall("ADDRESS") if a.text]
        if not addresses:
            addresses = [a.text.strip() for a in mailing.findall("ADDRESS") if a.text]

    # Fallback: regex over raw block
    if not addresses:
        raw_mail = re.search(
            r"<LEDMAILINGDETAILS\.LIST>(.*?)</LEDMAILINGDETAILS\.LIST>",
            block_xml, re.DOTALL | re.IGNORECASE
        )
        if raw_mail:
            addresses = re.findall(
                r"<ADDRESS[^>]*>([^<]+)</ADDRESS>", raw_mail.group(1), re.IGNORECASE
            )
            addresses = [a.strip() for a in addresses if a.strip()]

    state   = _find_text(mailing, "STATE")   or ""
    pincode = _find_text(mailing, "PINCODE") or ""

    # Phone: check LEDCONTACTDETAILS.LIST first, then top-level LEDGERPHONE
    phone = ""
    contact = ledger.find("LEDCONTACTDETAILS.LIST")
    if contact is not None:
        phone = _find_text(contact, "PHONENUMBER")
    if not phone:
        phone = _find_text(ledger, "LEDGERPHONE") or ""

    email = _find_text(ledger, "EMAIL") or ""
    gstin = (_find_text(ledger, "PARTYGSTIN")
             or _find_text(ledger, "LEDGSTIN")
             or "")

    return {
        "gstin":     gstin,
        "state":     state,
        "pincode":   pincode,
        "addresses": addresses,
        "phone":     phone,
        "email":     email,
    }


# ── build party_details array ──────────────────────────────────────────────────

party_details = []
with_pin = 0
missing_pin = 0

for name in party_names:
    outstanding = party_outstanding[name]
    block = extract_ledger_block(masters_xml, name)

    if block is None:
        print(f"  NOT FOUND : {name}")
        party_details.append({
            "party_name":  name,
            "gstin":       "",
            "state":       "",
            "pincode":     "",
            "addresses":   [],
            "phone":       "",
            "email":       "",
            "outstanding": outstanding,
            "error":       "Ledger not found in company masters",
        })
        missing_pin += 1
        continue

    info = parse_ledger_block(block)

    if "error" in info:
        print(f"  PARSE ERR : {name}: {info['error']}")
        party_details.append({
            "party_name":  name,
            "gstin":       "",
            "state":       "",
            "pincode":     "",
            "addresses":   [],
            "phone":       "",
            "email":       "",
            "outstanding": outstanding,
            "error":       info["error"],
        })
        missing_pin += 1
    else:
        pin = info.get("pincode", "")
        if pin:
            with_pin += 1
        else:
            missing_pin += 1
        print(f"  {'OK+pin' if pin else 'OK    '} : {name[:45]:<45}  pin={pin or '(none)':>6}  state={info.get('state', '')}")
        party_details.append({
            "party_name":  name,
            "gstin":       info.get("gstin", ""),
            "state":       info.get("state", ""),
            "pincode":     pin,
            "addresses":   info.get("addresses", []),
            "phone":       info.get("phone", ""),
            "email":       info.get("email", ""),
            "outstanding": outstanding,
        })

os.makedirs(OUT_DIR, exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(party_details, f, indent=2, ensure_ascii=False)

total = len(party_details)
print(f"\nParty details fetched -- {total} parties, {with_pin} with pin codes, {missing_pin} missing pin codes")
print(f"Saved to: {OUT}")
