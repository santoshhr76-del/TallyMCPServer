"""
Fetch full ledger details for every party in receivables.json
and write party_details.json.
"""
import json
import re
import os
import urllib.request
import xml.etree.ElementTree as ET

TALLY_URL = "http://tally.tallymcpclient.com/"
OUT_DIR   = r"C:\Users\Dell\Documents\TallyMCPServer\receivables-dashboard\output"
RECV_PATH = os.path.join(OUT_DIR, "receivables.json")
OUT_PATH  = os.path.join(OUT_DIR, "party_details.json")

HEADERS = {"Content-Type": "text/xml; charset=utf-8"}

# ── helpers ────────────────────────────────────────────────────────────────────

def _xe(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _rx(text: str, tag: str) -> str:
    """Regex fallback: first occurrence of <TAG>value</TAG>."""
    m = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""

def _find_text(el, tag: str) -> str:
    if el is None:
        return ""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""

ILLEGAL_XML_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|"
    r"&#(?:x([0-9A-Fa-f]+)|([0-9]+));",
)

def _sanitize(text: str) -> str:
    def repl(m):
        hex_val, dec_val = m.group(1), m.group(2)
        if hex_val or dec_val:
            cp = int(hex_val, 16) if hex_val else int(dec_val)
            if cp in (0x9, 0xA, 0xD) or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD):
                return m.group(0)
            return ""
        return ""
    return ILLEGAL_XML_RE.sub(repl, text)

def _post_xml(xml_body: str) -> str:
    data = xml_body.encode("utf-8")
    req  = urllib.request.Request(TALLY_URL, data=data,
                                  headers={"Content-Type": "text/xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_bytes = resp.read()
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw_bytes.decode("utf-16")
    return raw_bytes.decode("utf-8", errors="replace")

def _parse_xml(text: str) -> ET.Element:
    clean = _sanitize(text)
    return ET.fromstring(clean)


# ── ledger fetcher ──────────────────────────────────────────────────────────────

def fetch_ledger(name: str) -> dict:
    safe_name = name.replace("&", "&amp;").replace('"', "&quot;")
    xml_body = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPLedgerDetail</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPLedgerDetail" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>Name,Parent,ClosingBalance,OpeningBalance,CurrencyName,
                   GSTRegistrationType,PartyGSTIN,IncomeTaxNumber,
                   LedgerPhone,Email,CreditLimit,BillCreditPeriod,IsBillWiseOn,
                   LedMailingDetails</FETCH>
            <FILTER>MCPLedgerByName</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="MCPLedgerByName">$Name = "{safe_name}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    try:
        raw  = _post_xml(xml_body)
        root = _parse_xml(raw)
    except Exception as exc:
        return {"_error": str(exc), "_raw": ""}

    ledger = root.find(".//LEDGER")
    if ledger is None:
        # Check for error message in response
        err_text = _rx(raw, "LINEERROR") or _rx(raw, "RESPONSE") or "Ledger not found"
        return {"_error": err_text, "_raw": raw[:500]}

    # Address lines
    mailing  = ledger.find("LEDMAILINGDETAILS.LIST")
    addresses: list[str] = []
    if mailing is not None:
        addresses = [a.text.strip() for a in mailing.findall("ADDRESS.LIST/ADDRESS") if a.text]
    if not addresses:
        raw_addr_block = re.search(
            r"<LEDMAILINGDETAILS\.LIST>(.*?)</LEDMAILINGDETAILS\.LIST>", raw, re.DOTALL | re.IGNORECASE
        )
        if raw_addr_block:
            addresses = re.findall(r"<ADDRESS[^>]*>([^<]+)</ADDRESS>", raw_addr_block.group(1), re.IGNORECASE)
            addresses = [a.strip() for a in addresses if a.strip()]

    state   = (_find_text(mailing, "STATE")   if mailing is not None else "") or _rx(raw, "STATE")
    country = (_find_text(mailing, "COUNTRY") if mailing is not None else "") or _rx(raw, "COUNTRY")
    pincode = (_find_text(mailing, "PINCODE") if mailing is not None else "") or _rx(raw, "PINCODE")
    gstin   = _find_text(ledger, "PARTYGSTIN")  or _rx(raw, "PARTYGSTIN")
    phone   = _find_text(ledger, "LEDGERPHONE") or _rx(raw, "LEDGERPHONE")
    email   = _find_text(ledger, "EMAIL")        or _rx(raw, "EMAIL")

    return {
        "gstin":     gstin,
        "state":     state,
        "pincode":   pincode,
        "addresses": addresses,
        "phone":     phone,
        "email":     email,
    }


# ── main ────────────────────────────────────────────────────────────────────────

with open(RECV_PATH, encoding="utf-8") as f:
    recv = json.load(f)

party_outstanding = {p["party_name"]: p["outstanding"] for p in recv["parties"]}

party_details = []
with_pin = 0
missing_pin = 0

for party_name, outstanding in party_outstanding.items():
    print(f"  Fetching: {party_name} ...", end=" ", flush=True)
    info = fetch_ledger(party_name)

    if "_error" in info:
        print(f"ERROR: {info['_error']}")
        party_details.append({
            "party_name":  party_name,
            "gstin":       "",
            "state":       "",
            "pincode":     "",
            "addresses":   [],
            "phone":       "",
            "email":       "",
            "outstanding": outstanding,
            "error":       info["_error"],
        })
        missing_pin += 1
    else:
        pin = info.get("pincode", "")
        if pin:
            with_pin += 1
        else:
            missing_pin += 1
        print(f"OK  pin={pin or '(none)'}")
        party_details.append({
            "party_name":  party_name,
            "gstin":       info.get("gstin", ""),
            "state":       info.get("state", ""),
            "pincode":     pin,
            "addresses":   info.get("addresses", []),
            "phone":       info.get("phone", ""),
            "email":       info.get("email", ""),
            "outstanding": outstanding,
        })

with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(party_details, f, indent=2, ensure_ascii=False)

total = len(party_details)
print(f"\nParty details fetched -- {total} parties, {with_pin} with pin codes, {missing_pin} missing pin codes")
print(f"Saved to: {OUT_PATH}")
