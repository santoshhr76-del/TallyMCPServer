"""
TallyPrime XML HTTP Client
Communicates with TallyPrime Gateway Server (default port 9000)
via XML requests following the TDL (Tally Definition Language) protocol.

Every public function accepts an optional `tally_url` parameter so a single
MCP server deployment can target different TallyPrime instances at runtime.
"""

import httpx
import xml.etree.ElementTree as ET
import re
from typing import Any
import logging
import os
from datetime import date
logger = logging.getLogger(__name__)

# Default URL — override per-call via tally_url argument or at startup via env var
DEFAULT_TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("TALLY_TIMEOUT", "30"))

HEADERS = {"Content-Type": "text/xml; charset=utf-8"}


def _xe(s: str) -> str:
    """XML-escape a string value for safe insertion into element text content."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _parse_date(date_str: str) -> str:
    """Parse a user-supplied date string and return Tally's YYYYMMDD format.

    Accepted input formats:
      DD-MM-YYYY  (e.g. "01-04-2025")  — Indian / Tally UI convention
      DD/MM/YYYY  (e.g. "01/04/2025")
      YYYY-MM-DD  (e.g. "2025-04-01")  — ISO 8601
      YYYYMMDD    (e.g. "20250401")     — Tally native, passed through unchanged

    Raises ValueError for unrecognised formats.
    """
    s = date_str.strip()
    if re.fullmatch(r"\d{8}", s):                      # already YYYYMMDD
        return s
    if re.fullmatch(r"\d{2}[/-]\d{2}[/-]\d{4}", s):   # DD-MM-YYYY or DD/MM/YYYY
        parts = re.split(r"[/-]", s)
        return f"{parts[2]}{parts[1]}{parts[0]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):          # YYYY-MM-DD
        return s.replace("-", "")
    raise ValueError(
        f"Unrecognised date format: '{date_str}'. "
        "Use DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, or YYYYMMDD."
    )


def _resolve_url(tally_url: str | None) -> str:
    """Return caller-supplied URL if given, otherwise fall back to the default."""
    if tally_url:
        return tally_url.rstrip("/")
    return DEFAULT_TALLY_URL


def _post_xml(xml_body: str, tally_url: str | None = None, timeout: float | None = None) -> str:
    """Send an XML request to TallyPrime and return the raw XML response as a str.

    TallyPrime sometimes returns UTF-16 LE (with BOM) when exporting reports that
    include certain columns (e.g. Opening Amount / BILLOP).  httpx guesses the
    encoding from the Content-Type header, which may not declare charset=utf-16,
    causing response.text to be garbled.  We detect the BOM explicitly and decode
    accordingly so the parser always receives a clean Unicode string.
    """
    url = _resolve_url(tally_url)
    t = timeout or DEFAULT_TIMEOUT
    with httpx.Client(timeout=t) as client:
        response = client.post(url, content=xml_body.encode("utf-8"), headers=HEADERS)
        response.raise_for_status()
        raw_bytes = response.content
        # UTF-16 LE BOM: FF FE — UTF-16 BE BOM: FE FF
        if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw_bytes.decode("utf-16")
        return response.text


def _sanitize_xml(xml_text: str) -> str:
    """
    Remove characters and character references that are illegal in XML 1.0.

    TallyPrime sends two kinds of illegal content:
      1. Literal control characters (e.g. raw 0x1F byte in a ledger name)
      2. Numeric character references (e.g. &#x1F; or &#31;) whose codepoint
         is illegal — the XML parser resolves these and then crashes.

    Valid XML 1.0 codepoints:
      #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    """
    # ── Step 1: strip numeric character references to illegal codepoints ──────
    # Must be done BEFORE stripping literals, because the references themselves
    # are made of legal ASCII characters that the literal-strip won't touch.
    def _filter_char_ref(m: re.Match) -> str:
        ref = m.group(1)
        try:
            cp = int(ref[1:], 16) if ref[0] in "xX" else int(ref)
        except ValueError:
            return m.group(0)          # leave malformed references alone
        # Codepoints illegal in XML 1.0:
        #   0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F, 0xFFFE, 0xFFFF, surrogates
        if (
            cp < 0x09
            or cp in (0x0B, 0x0C)
            or (0x0E <= cp <= 0x1F)
            or (0xD800 <= cp <= 0xDFFF)
            or cp in (0xFFFE, 0xFFFF)
        ):
            return " "
        return m.group(0)

    xml_text = re.sub(r"&#([xX][0-9a-fA-F]+|\d+);", _filter_char_ref, xml_text)

    # ── Step 2: strip literal illegal characters ──────────────────────────────
    illegal_chars = re.compile(
        r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
    )
    return illegal_chars.sub(" ", xml_text)


def _parse_xml(xml_text: str) -> ET.Element:
    return ET.fromstring(_sanitize_xml(xml_text))


def _find_text(element: ET.Element, path: str, default: str = "") -> str:
    node = element.find(path)
    return (node.text or "").strip() if node is not None else default


def _rx(xml_str: str, tag: str, default: str = "") -> str:
    """
    Extract text content of the FIRST occurrence of <TAG ...>value</TAG>
    from a raw XML string, ignoring any attributes on the tag.
    Also handles XML attribute extraction: _rx(xml, 'COMPANY@NAME').
    Case-insensitive.
    """
    if "@" in tag:
        elem, attr = tag.split("@", 1)
        m = re.search(rf'<{elem}\b[^>]*\b{attr}="([^"]*)"', xml_str, re.IGNORECASE)
    else:
        m = re.search(rf'<{tag}\b[^>]*>([^<]*)</{tag}>', xml_str, re.IGNORECASE)
    return m.group(1).strip() if m else default


# ─────────────────────────────────────────────
# COMPANY / CONNECTION
# ─────────────────────────────────────────────

def get_active_company(tally_url: str | None = None) -> dict[str, Any]:
    """
    Return the currently open company in TallyPrime.

    Uses a Collection request (the only type guaranteed to work on all
    TallyPrime versions via the Gateway XML API).

    Also returns _raw_xml so you can inspect TallyPrime's exact response
    if any fields come back empty — pass that XML to debug_raw_xml to diagnose.
    """
    xml = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPCompanyList</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPCompanyList" ISMODIFY="No">
            <TYPE>Company</TYPE>
            <FETCH>Name,StartingFrom,EndingAt,CurrencyName,
                   GSTRegistrationNumber,StateName,CountryName,
                   PhoneNumber,Email,Address,BooksFrom,IsSimpleGSTEnabled</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)

    # Use regex directly on the raw XML string — more reliable than ElementTree
    # when TallyPrime wraps values with TYPE attributes e.g. <NAME TYPE="String">
    if "<COMPANY" not in raw:
        return {
            "error": "No COMPANY element found in TallyPrime response",
            "tally_url": _resolve_url(tally_url),
        }

    return {
        # NAME appears both as attribute <COMPANY NAME="..."> and child <NAME>...</NAME>
        "name":          _rx(raw, "COMPANY@NAME") or _rx(raw, "NAME"),
        "starting_from": _rx(raw, "STARTINGFROM"),
        "ending_at":     _rx(raw, "ENDINGAT"),
        "currency":      _rx(raw, "CURRENCYNAME"),
        "books_from":    _rx(raw, "BOOKSFROM"),
        "gstin":         _rx(raw, "GSTREGISTRATIONNUMBER"),
        "state":         _rx(raw, "STATENAME"),
        "country":       _rx(raw, "COUNTRYNAME"),
        "phone":         _rx(raw, "PHONENUMBER"),
        "email":         _rx(raw, "EMAIL"),
        "address":       _rx(raw, "ADDRESS"),
        "tally_url":     _resolve_url(tally_url),
    }


def debug_raw_xml(request_xml: str, tally_url: str | None = None) -> dict[str, Any]:
    """
    Send any raw XML to TallyPrime and return the raw response text.
    Use this tool to inspect exactly what TallyPrime returns for any request —
    helpful for diagnosing empty fields or unexpected structures.
    """
    raw = _post_xml(request_xml, tally_url)
    return {
        "raw_response": raw,
        "length": len(raw),
        "tally_url": _resolve_url(tally_url),
    }


# ─────────────────────────────────────────────
# LEDGERS & GROUPS
# ─────────────────────────────────────────────

def fetch_all_ledgers(tally_url: str | None = None) -> list[dict[str, Any]]:
    """Fetch all ledgers from TallyPrime.
    Uses a 120-second timeout — the full ledger list can be several MB of XML."""
    xml = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Ledgers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="List of Ledgers" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>Name,Parent,ClosingBalance,OpeningBalance,CurrencyName,
                   MasterId,IsRevenue,IsDeemedPositive,IsBillWiseOn</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(xml, tally_url, timeout=120.0))
    return [
        {
            "name": ledger.get("NAME") or _find_text(ledger, "NAME"),
            "parent": _find_text(ledger, "PARENT"),
            "opening_balance": _find_text(ledger, "OPENINGBALANCE"),
            "closing_balance": _find_text(ledger, "CLOSINGBALANCE"),
            "currency": _find_text(ledger, "CURRENCYNAME"),
            "master_id": ledger.get("MASTERID", ""),
            "is_revenue": _find_text(ledger, "ISREVENUE"),
        }
        for ledger in root.findall(".//LEDGER")
    ]


def fetch_ledger(name: str, tally_url: str | None = None) -> dict[str, Any]:
    """Fetch details of a specific ledger by name.

    Uses a Collection request with a TDL name-filter — the only request type
    that works reliably on all TallyPrime versions via the XML Gateway.
    (<TYPE>Object</TYPE> is not supported by the Gateway.)
    """
    # Escape any double-quotes in the ledger name so the TDL formula is valid XML
    safe_name = name.replace("&", "&amp;").replace('"', "&quot;")
    xml = f"""<ENVELOPE>
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
                   LedgerMobile,Email,CreditLimit,BillCreditPeriod,IsBillWiseOn,
                   LedMailingDetails</FETCH>
            <FILTER>MCPLedgerByName</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="MCPLedgerByName">$Name = "{safe_name}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)
    ledger = root.find(".//LEDGER")
    if ledger is None:
        return {"error": f"Ledger '{name}' not found", "tally_url": _resolve_url(tally_url)}

    # ── Mailing details: address, state, country, pincode live inside
    #    <LEDMAILINGDETAILS.LIST> as confirmed from the Tally Masters XML export.
    mailing = ledger.find("LEDMAILINGDETAILS.LIST")

    # Address lines from the mailing sub-element
    addresses: list[str] = []
    if mailing is not None:
        addresses = [a.text.strip() for a in mailing.findall("ADDRESS.LIST/ADDRESS") if a.text]
    if not addresses:
        # Fallback: regex over raw XML to handle TYPE="String" attribute on ADDRESS.LIST
        raw_addr_block = re.search(
            r"<LEDMAILINGDETAILS\.LIST>(.*?)</LEDMAILINGDETAILS\.LIST>", raw, re.DOTALL | re.IGNORECASE
        )
        if raw_addr_block:
            addresses = re.findall(
                r"<ADDRESS[^>]*>([^<]+)</ADDRESS>", raw_addr_block.group(1), re.IGNORECASE
            )
            addresses = [a.strip() for a in addresses if a.strip()]

    # State, Country, Pincode — from LEDMAILINGDETAILS.LIST (actual tag names per Tally XML)
    state   = (_find_text(mailing, "STATE")   if mailing is not None else "") or _rx(raw, "STATE")
    country = (_find_text(mailing, "COUNTRY") if mailing is not None else "") or _rx(raw, "COUNTRY")
    pincode = (_find_text(mailing, "PINCODE") if mailing is not None else "") or _rx(raw, "PINCODE")

    # Opening/closing balance: negative value = credit balance in Tally export format
    def _fmt_balance(val: str) -> str:
        """Convert Tally's numeric balance to a human-readable string with Dr/Cr suffix."""
        v = val.strip()
        if not v or v == "0" or v == "0.00":
            return "0.00"
        try:
            n = float(v)
            if n < 0:
                return f"{abs(n):.2f} Cr"
            return f"{n:.2f} Dr"
        except ValueError:
            return v  # already has Dr/Cr or is non-numeric — return as-is

    opening_balance = _fmt_balance(
        _find_text(ledger, "OPENINGBALANCE") or _rx(raw, "OPENINGBALANCE")
    )
    closing_balance = _fmt_balance(
        _find_text(ledger, "CLOSINGBALANCE") or _rx(raw, "CLOSINGBALANCE")
    )

    return {
        "name":                 ledger.get("NAME") or _find_text(ledger, "NAME") or _rx(raw, "NAME"),
        "parent":               _find_text(ledger, "PARENT")                or _rx(raw, "PARENT"),
        "opening_balance":      opening_balance,
        "closing_balance":      closing_balance,
        "currency":             _find_text(ledger, "CURRENCYNAME")          or _rx(raw, "CURRENCYNAME"),
        "gst_registration_type":_find_text(ledger, "GSTREGISTRATIONTYPE")  or _rx(raw, "GSTREGISTRATIONTYPE"),
        "gstin":                _find_text(ledger, "PARTYGSTIN")            or _rx(raw, "PARTYGSTIN"),
        "pan":                  _find_text(ledger, "INCOMETAXNUMBER")       or _rx(raw, "INCOMETAXNUMBER"),
        "phone":                (_find_text(mailing, "PHONENUMBER") if mailing is not None else "")
                                or _find_text(ledger, "LEDGERMOBILE")
                                or _rx(raw, "PHONENUMBER")
                                or _rx(raw, "LEDGERMOBILE"),
        "email":                _find_text(ledger, "EMAIL")                 or _rx(raw, "EMAIL"),
        "addresses":            addresses,
        "state":                state,
        "country":              country,
        "pincode":              pincode,
        "credit_limit":         _find_text(ledger, "CREDITLIMIT")           or _rx(raw, "CREDITLIMIT"),
        "credit_period":        _find_text(ledger, "BILLCREDITPERIOD")      or _rx(raw, "BILLCREDITPERIOD"),
        "is_bill_wise":         _find_text(ledger, "ISBILLWISEON")          or _rx(raw, "ISBILLWISEON"),
        "tally_url":            _resolve_url(tally_url),
    }


def create_party_ledger(
    name: str,
    parent: str,
    opening_balance: float = 0.0,
    gstin: str = "",
    gst_registration_type: str = "Regular",
    address: str = "",
    state: str = "",
    country: str = "India",
    pincode: str = "",
    phone: str = "",
    email: str = "",
    credit_period: str = "",
    credit_limit: float = 0.0,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a new ledger in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>All Masters</ID> — the correct format for
    TallyPrime Import requests via the XML Gateway.

    Address, state, country and pincode are written inside <LEDMAILINGDETAILS.LIST>
    as confirmed by the Tally Masters XML structure.
    GSTIN is written both at root level (<PARTYGSTIN>) and inside <LEDGSTREGDETAILS.LIST>.
    """

    # Phone / email / credit fields
    phone_xml     = f"<LEDGERMOBILE>{_xe(phone)}</LEDGERMOBILE>"                  if phone         else ""
    email_xml     = f"<EMAIL>{_xe(email)}</EMAIL>"                                if email         else ""
    cr_period_xml = f"<BILLCREDITPERIOD>{_xe(credit_period)}</BILLCREDITPERIOD>"  if credit_period else ""
    cr_limit_xml  = f"<CREDITLIMIT>{credit_limit}</CREDITLIMIT>"                  if credit_limit  else ""

    # GSTIN at root level (used by Tally for party identification)
    gstin_xml = f"<PARTYGSTIN>{_xe(gstin)}</PARTYGSTIN>" if gstin else ""

    # Financial year start date — required by Tally in APPLICABLEFROM fields
    today = date.today()
    fy_start = f"{today.year if today.month >= 4 else today.year - 1}0401"

    # Build <LEDGSTREGDETAILS.LIST> — exact field order per Tally Masters XML:
    # APPLICABLEFROM → GSTREGISTRATIONTYPE → STATE → PLACEOFSUPPLY → GSTIN
    if gstin or gst_registration_type:
        gst_reg_xml = f"""          <LEDGSTREGDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>
            <STATE>{_xe(state)}</STATE>
            <PLACEOFSUPPLY>{_xe(state)}</PLACEOFSUPPLY>
            <GSTIN>{_xe(gstin)}</GSTIN>
          </LEDGSTREGDETAILS.LIST>"""
    else:
        gst_reg_xml = ""

    # Build <LEDMAILINGDETAILS.LIST> — exact field order per Tally Masters XML:
    # ADDRESS.LIST → APPLICABLEFROM → PINCODE → MAILINGNAME → STATE → COUNTRY
    if address or state or country or pincode:
        addr_lines = "\n".join(
            f"              <ADDRESS>{_xe(line.strip())}</ADDRESS>"
            for line in address.split("\n") if line.strip()
        )
        addr_list_xml = (
            f"            <ADDRESS.LIST TYPE='String'>\n{addr_lines}\n            </ADDRESS.LIST>"
            if addr_lines else ""
        )
        mailing_xml = f"""          <LEDMAILINGDETAILS.LIST>
{addr_list_xml}
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <PINCODE>{_xe(pincode)}</PINCODE>
            <MAILINGNAME>{_xe(name)}</MAILINGNAME>
            <STATE>{_xe(state)}</STATE>
            <COUNTRY>{_xe(country)}</COUNTRY>
          </LEDMAILINGDETAILS.LIST>"""
    else:
        mailing_xml = ""

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <OPENINGBALANCE>{opening_balance}</OPENINGBALANCE>
          <GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>
          {gstin_xml}
          {phone_xml}
          {email_xml}
          {cr_period_xml}
          {cr_limit_xml}
          <ISBILLWISEON>No</ISBILLWISEON>
{gst_reg_xml}
{mailing_xml}
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)

    # TallyPrime wraps import result fields with TYPE attributes — use _rx() to read them
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    # Collect any line-level errors from the response
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)

    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":      "success" if ok else ("error" if errors else "no_change"),
        "created":     created,
        "altered":     altered,
        "errors":      errors,
        "raw_status":  _rx(raw, "STATUS") or "",
        "ledger_name": name,
        "tally_url":   _resolve_url(tally_url),
    }


def create_sales_ledger(
    name: str,
    effective_date: str,
    parent: str = "Sales Accounts",
    gst_type_of_supply: str = "Goods",
    taxability: str = "Taxable",
    gst_nature_of_transaction: str = "",
    hsn_sac_code: str = "",
    hsn_description: str = "",
    gst_rate: float = 0.0,
    is_reverse_charge: bool = False,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Sales / Income ledger in TallyPrime.

    Based on the actual GST Sales and Local Sales 18% ledger structures
    from Tally Masters XML analysis.

    Key fields written:
      PARENT                    → sales group (default: Sales Accounts)
      GSTTYPEOFSUPPLY           → Goods | Services
      AFFECTSSTOCK              → Yes (required for sales ledgers)
      GSTDETAILS.LIST           → taxability, nature of transaction,
                                  CGST/SGST/IGST rates (derived from gst_rate),
                                  reverse charge flag
                                  APPLICABLEFROM = effective_date
                                  STATEWISEDETAILS.LIST with STATENAME="" (Any state)
      HSNDETAILS.LIST           → HSN/SAC code + optional description (if provided)

    GST rate logic:
      igst_rate = gst_rate          (e.g. 18 for 18% GST)
      cgst_rate = gst_rate / 2      (e.g. 9)
      sgst_rate = gst_rate / 2      (e.g. 9)

    Args:
        name                      : Ledger name (e.g. "Local Sales 18%")
        effective_date            : Date from which GST details are effective.
                                    Formats: DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD,
                                    or YYYYMMDD (e.g. "01-04-2025")
        parent                    : Parent group (default: "Sales Accounts")
        gst_type_of_supply        : "Goods" or "Services"
        taxability                : "Taxable", "Exempt", "Nil Rated", or "Non-GST"
        gst_nature_of_transaction : GST nature of transaction
                                    e.g. "Local Sales - Taxable",
                                         "Interstate Sales - Taxable",
                                         "Exports - Taxable"
                                    Leave blank to omit (as in the GST Sales ledger)
        hsn_sac_code              : HSN code for goods / SAC code for services (optional)
        hsn_description           : Description of the HSN/SAC code (e.g. "Steel")
        gst_rate                  : Total GST rate % (e.g. 18 for 18% GST).
                                    IGST = gst_rate, CGST = SGST = gst_rate / 2
        is_reverse_charge         : Set True to mark ledger as Reverse Charge Applicable
    """
    fy_start = _parse_date(effective_date)

    # Derive component rates from single GST rate
    igst_rate = gst_rate
    cgst_rate = gst_rate / 2
    sgst_rate = gst_rate / 2

    rev_charge = "Yes" if is_reverse_charge else "No"
    cess_valuation = "Not Applicable"

    nature_xml = (
        f"            <GSTNATUREOFTRANSACTION>{_xe(gst_nature_of_transaction)}</GSTNATUREOFTRANSACTION>\n"
        if gst_nature_of_transaction else ""
    )

    gst_details_xml = f"""          <GSTDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <TAXABILITY>{_xe(taxability)}</TAXABILITY>
{nature_xml}            <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
            <GSTCALCSLABONMRP>No</GSTCALCSLABONMRP>
            <ISREVERSECHARGEAPPLICABLE>{rev_charge}</ISREVERSECHARGEAPPLICABLE>
            <ISNONGSTGOODS>No</ISNONGSTGOODS>
            <GSTINELIGIBLEITC>Yes</GSTINELIGIBLEITC>
            <INCLUDEEXPFORSLABCALC>No</INCLUDEEXPFORSLABCALC>
            <STATEWISEDETAILS.LIST>
              <STATENAME></STATENAME>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{cgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{sgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{igst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>{_xe(cess_valuation)}</GSTRATEVALUATIONTYPE>
              </RATEDETAILS.LIST>
            </STATEWISEDETAILS.LIST>
          </GSTDETAILS.LIST>"""

    # HSN/SAC details — only written when a code is provided
    if hsn_sac_code:
        hsn_desc_xml = (
            f"            <HSN>{_xe(hsn_description)}</HSN>\n"
            if hsn_description else ""
        )
        hsn_xml = f"""          <HSNDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <HSNCODE>{_xe(hsn_sac_code)}</HSNCODE>
{hsn_desc_xml}            <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
          </HSNDETAILS.LIST>"""
    else:
        hsn_xml = "          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
          <GSTTYPEOFSUPPLY>{_xe(gst_type_of_supply)}</GSTTYPEOFSUPPLY>
          <AFFECTSSTOCK>Yes</AFFECTSSTOCK>
          <ISCOSTCENTRESON>Yes</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
{gst_details_xml}
{hsn_xml}
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":      "success" if ok else ("error" if errors else "no_change"),
        "created":     created,
        "altered":     altered,
        "errors":      errors,
        "raw_status":  _rx(raw, "STATUS") or "",
        "ledger_name": name,
        "tally_url":   _resolve_url(tally_url),
    }


def create_purchase_ledger(
    name: str,
    effective_date: str,
    parent: str = "Purchase Accounts",
    gst_type_of_supply: str = "Goods",
    taxability: str = "Taxable",
    gst_nature_of_transaction: str = "Interstate Purchase - Taxable",
    hsn_sac_code: str = "",
    hsn_description: str = "",
    gst_rate: float = 0.0,
    is_reverse_charge: bool = False,
    is_ineligible_itc: bool = False,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Purchase / Expense ledger in TallyPrime.

    Based on the actual "Interstate Purchase 18%" ledger structure
    from Tally Masters XML analysis (Master-08956c1b.xml).

    Key fields written:
      PARENT                    → purchase group (default: Purchase Accounts)
      GSTTYPEOFSUPPLY           → Goods | Services
      AFFECTSSTOCK              → Yes (required for purchase ledgers)
      GSTDETAILS.LIST           → taxability, nature of transaction,
                                  CGST/SGST/IGST rates (derived from gst_rate),
                                  reverse charge flag, ITC eligibility flag
                                  APPLICABLEFROM = effective_date
                                  STATEWISEDETAILS.LIST with STATENAME="" (Any state)
      HSNDETAILS.LIST           → HSN/SAC code + optional description (if provided)

    GST rate logic:
      igst_rate = gst_rate          (e.g. 18 for 18% GST)
      cgst_rate = gst_rate / 2      (e.g. 9)
      sgst_rate = gst_rate / 2      (e.g. 9)

    Key differences from create_sales_ledger:
      - PARENT defaults to "Purchase Accounts"
      - GSTINELIGIBLEITC defaults to No (ITC eligible for purchases)
      - gst_nature_of_transaction defaults to "Interstate Purchase - Taxable"
      - is_ineligible_itc parameter controls ITC eligibility

    Args:
        name                      : Ledger name (e.g. "Interstate Purchase 18%")
        effective_date            : Date from which GST details are effective.
                                    Formats: DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD,
                                    or YYYYMMDD (e.g. "01-04-2025")
        parent                    : Parent group (default: "Purchase Accounts")
        gst_type_of_supply        : "Goods" or "Services"
        taxability                : "Taxable", "Exempt", "Nil Rated", or "Non-GST"
        gst_nature_of_transaction : GST nature of transaction
                                    e.g. "Interstate Purchase - Taxable",
                                         "Intrastate Purchase - Taxable",
                                         "Interstate Purchase - Exempt"
        hsn_sac_code              : HSN code for goods / SAC code for services (optional)
        hsn_description           : Description of the HSN/SAC code (optional)
        gst_rate                  : Total GST rate % (e.g. 18 for 18% GST).
                                    IGST = gst_rate, CGST = SGST = gst_rate / 2
        is_reverse_charge         : Set True to mark ledger as Reverse Charge Applicable
        is_ineligible_itc         : Set True if ITC is ineligible for this purchase
                                    (e.g. for blocked credits under Section 17(5))
    """
    fy_start = _parse_date(effective_date)

    # Derive component rates from single GST rate
    igst_rate = gst_rate
    cgst_rate = gst_rate / 2
    sgst_rate = gst_rate / 2

    rev_charge = "Yes" if is_reverse_charge else "No"
    ineligible_itc = "Yes" if is_ineligible_itc else "No"
    cess_valuation = "Not Applicable"

    nature_xml = (
        f"            <GSTNATUREOFTRANSACTION>{_xe(gst_nature_of_transaction)}</GSTNATUREOFTRANSACTION>\n"
        if gst_nature_of_transaction else ""
    )

    gst_details_xml = f"""          <GSTDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <TAXABILITY>{_xe(taxability)}</TAXABILITY>
{nature_xml}            <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
            <GSTCALCSLABONMRP>No</GSTCALCSLABONMRP>
            <ISREVERSECHARGEAPPLICABLE>{rev_charge}</ISREVERSECHARGEAPPLICABLE>
            <ISNONGSTGOODS>No</ISNONGSTGOODS>
            <GSTINELIGIBLEITC>{ineligible_itc}</GSTINELIGIBLEITC>
            <INCLUDEEXPFORSLABCALC>No</INCLUDEEXPFORSLABCALC>
            <STATEWISEDETAILS.LIST>
              <STATENAME></STATENAME>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{cgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{sgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{igst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>{_xe(cess_valuation)}</GSTRATEVALUATIONTYPE>
              </RATEDETAILS.LIST>
            </STATEWISEDETAILS.LIST>
          </GSTDETAILS.LIST>"""

    # HSN/SAC details — only written when a code is provided
    if hsn_sac_code:
        hsn_desc_xml = (
            f"            <HSN>{_xe(hsn_description)}</HSN>\n"
            if hsn_description else ""
        )
        hsn_xml = f"""          <HSNDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <HSNCODE>{_xe(hsn_sac_code)}</HSNCODE>
{hsn_desc_xml}            <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
          </HSNDETAILS.LIST>"""
    else:
        hsn_xml = "          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
          <GSTTYPEOFSUPPLY>{_xe(gst_type_of_supply)}</GSTTYPEOFSUPPLY>
          <AFFECTSSTOCK>Yes</AFFECTSSTOCK>
          <ISCOSTCENTRESON>Yes</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
{gst_details_xml}
{hsn_xml}
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":      "success" if ok else ("error" if errors else "no_change"),
        "created":     created,
        "altered":     altered,
        "errors":      errors,
        "raw_status":  _rx(raw, "STATUS") or "",
        "ledger_name": name,
        "tally_url":   _resolve_url(tally_url),
    }


def create_duty_ledger(
    name: str,
    duty_head: str,
    parent: str = "Duties & Taxes",
    rate_of_tax: float = 0.0,
    cess_valuation_method: str = "Based on Value",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a GST Duty ledger (CGST / SGST / IGST / Cess) in TallyPrime.

    Based on the actual CGST, SGST, IGST, INPUT CGST, INPUT SGST, INPUT IGST,
    and GST CESS ledger structures from Tally Masters XML analysis
    (Master-08956c1b.xml).

    These ledgers are the tax collection/payment accounts used in vouchers.
    The key fields are TAXTYPE=GST, GSTDUTYHEAD (which tax head), and
    optionally RATEOFTAXCALCULATION (percentage of calculation).

    For CGST/SGST/IGST: rate_of_tax is usually left at 0 because the rate
    is resolved dynamically from the sales/purchase ledger's GST details.
    For Cess: rate_of_tax should be set explicitly (e.g. 12 for 12% cess).

    Typical usage:
        Output tax → name="CGST",       duty_head="CGST"
        Output tax → name="SGST",       duty_head="SGST/UTGST"
        Output tax → name="IGST",       duty_head="IGST"
        Input tax  → name="Input CGST", duty_head="CGST"
        Input tax  → name="Input SGST", duty_head="SGST/UTGST"
        Input tax  → name="Input IGST", duty_head="IGST"
        Cess       → name="GST Cess",   duty_head="Cess", rate_of_tax=12

    Args:
        name                  : Ledger name (e.g. "CGST", "Input IGST", "GST Cess")
        duty_head             : GST duty head — must be one of:
                                  "CGST"       → Central GST
                                  "SGST/UTGST" → State / Union Territory GST
                                  "IGST"       → Integrated GST
                                  "Cess"       → GST Compensation Cess
        parent                : Parent group (default: "Duties & Taxes")
        rate_of_tax           : Percentage of calculation (e.g. 9 for 9%, 12 for 12%).
                                Leave at 0 for CGST/SGST/IGST (resolved from voucher).
                                Required for Cess duty head.
                                Maps to RATEOFTAXCALCULATION in Tally XML.
        cess_valuation_method : Valuation method for Cess — "Based on Value" (default)
                                or "Based on Quantity". Only used when duty_head="Cess".
                                Maps to CESSVALUATIONMETHOD in Tally XML.
        tally_url             : Optional Tally URL override
    """
    valid_duty_heads = {"CGST", "SGST/UTGST", "IGST", "Cess"}
    if duty_head not in valid_duty_heads:
        raise ValueError(
            f"duty_head must be one of {sorted(valid_duty_heads)}, got {duty_head!r}"
        )

    # RATEOFTAXCALCULATION — only write when > 0
    rate_xml = (
        f"          <RATEOFTAXCALCULATION> {rate_of_tax}</RATEOFTAXCALCULATION>\n"
        if rate_of_tax > 0 else ""
    )

    # CESSVALUATIONMETHOD — only relevant for Cess duty head
    cess_xml = (
        f"          <CESSVALUATIONMETHOD>{_xe(cess_valuation_method)}</CESSVALUATIONMETHOD>\n"
        if duty_head == "Cess" else ""
    )

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <TAXTYPE>GST</TAXTYPE>
          <GSTDUTYHEAD>{_xe(duty_head)}</GSTDUTYHEAD>
{rate_xml}{cess_xml}          <AFFECTSSTOCK>No</AFFECTSSTOCK>
          <ISCOSTCENTRESON>No</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
          <ISGSTAPPLICABLE>No</ISGSTAPPLICABLE>
          <GSTDETAILS.LIST>          </GSTDETAILS.LIST>
          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":        "success" if ok else ("error" if errors else "no_change"),
        "created":       created,
        "altered":       altered,
        "errors":        errors,
        "raw_status":    _rx(raw, "STATUS") or "",
        "ledger_name":   name,
        "duty_head":     duty_head,
        "rate_of_tax":   rate_of_tax,
        "tally_url":     _resolve_url(tally_url),
    }


def create_roundoff_ledger(
    name: str,
    parent: str = "Indirect Incomes",
    rounding_method: str = "Normal Rounding",
    rounding_limit: float = 1.0,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Round-Off ledger in TallyPrime.

    Based on the actual "Round Off" ledger structure from Tally Masters XML
    analysis (Master-08956c1b.xml).

    Round-off ledgers use VATDEALERNATURE=Invoice Rounding which tells Tally
    this is a rounding account. The ROUNDINGMETHOD controls direction and
    ROUNDINGLIMIT sets the maximum rounding amount.

    No GST is applied to round-off ledgers.

    Key fields:
      VATDEALERNATURE  → Invoice Rounding  (marks it as a rounding ledger)
      ROUNDINGMETHOD   → Normal Rounding / Upward Rounding / Downward Rounding
      ROUNDINGLIMIT    → Maximum rounding amount (e.g. 1 for rounding to nearest rupee)

    Args:
        name            : Ledger name (e.g. "Round Off", "Rounding Off")
        parent          : Parent group. Use "Indirect Incomes" (default, when rounding
                          results in income) or "Indirect Expenses" (when it is an expense).
        rounding_method : Rounding direction —
                            "Normal Rounding"   → rounds to nearest (default)
                            "Upward Rounding"   → always rounds up
                            "Downward Rounding" → always rounds down
        rounding_limit  : Maximum rounding amount (default: 1). Tally will not
                          round beyond this value.
        tally_url       : Optional Tally URL override
    """
    valid_methods = {"Normal Rounding", "Upward Rounding", "Downward Rounding"}
    if rounding_method not in valid_methods:
        raise ValueError(
            f"rounding_method must be one of {sorted(valid_methods)}, got {rounding_method!r}"
        )

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <TAXTYPE>Others</TAXTYPE>
          <GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>
          <VATDEALERNATURE>Invoice Rounding</VATDEALERNATURE>
          <ROUNDINGMETHOD>{_xe(rounding_method)}</ROUNDINGMETHOD>
          <ROUNDINGLIMIT>{rounding_limit}</ROUNDINGLIMIT>
          <AFFECTSSTOCK>No</AFFECTSSTOCK>
          <ISCOSTCENTRESON>No</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
          <ISGSTAPPLICABLE>No</ISGSTAPPLICABLE>
          <GSTDETAILS.LIST>          </GSTDETAILS.LIST>
          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":           "success" if ok else ("error" if errors else "no_change"),
        "created":          created,
        "altered":          altered,
        "errors":           errors,
        "raw_status":       _rx(raw, "STATUS") or "",
        "ledger_name":      name,
        "rounding_method":  rounding_method,
        "rounding_limit":   rounding_limit,
        "tally_url":        _resolve_url(tally_url),
    }


def create_discount_ledger(
    name: str,
    parent: str = "Indirect Expenses",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Discount ledger in TallyPrime.

    Based on the actual "Discount Allowed", "Discount Received", and "Disc"
    ledger structures from Tally Masters XML analysis (Master-08956c1b.xml).

    Discount ledgers are simple expense/income ledgers with no GST details.
    GST on discounts is handled at the voucher level in TallyPrime, not in
    the ledger master.

    Key fields:
      TAXTYPE       → Others
      GSTAPPLICABLE → Not Applicable
      AFFECTSSTOCK  → No
      ISCOSTCENTRESON → Yes

    Typical usage:
      Discount Allowed  → name="Discount Allowed",  parent="Indirect Expenses"
      Discount Received → name="Discount Received", parent="Indirect Incomes"

    Args:
        name      : Ledger name (e.g. "Discount Allowed", "Discount Received",
                    "Trade Discount")
        parent    : Parent group.
                    "Indirect Expenses" (default — for discount allowed/given)
                    "Indirect Incomes"  (for discount received)
                    "Discount"          (if a Discount group exists in the company)
        tally_url : Optional Tally URL override
    """
    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
          <TAXTYPE>Others</TAXTYPE>
          <GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>
          <VATDEALERNATURE>Discount</VATDEALERNATURE>
          <AFFECTSSTOCK>No</AFFECTSSTOCK>
          <ISCOSTCENTRESON>Yes</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
          <ISGSTAPPLICABLE>No</ISGSTAPPLICABLE>
          <GSTDETAILS.LIST>          </GSTDETAILS.LIST>
          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":      "success" if ok else ("error" if errors else "no_change"),
        "created":     created,
        "altered":     altered,
        "errors":      errors,
        "raw_status":  _rx(raw, "STATUS") or "",
        "ledger_name": name,
        "tally_url":   _resolve_url(tally_url),
    }


def create_additional_ledger(
    name: str,
    parent: str = "Indirect Expenses",
    include_in_assessable_value: str = "Not Applicable",
    # ── Transport & Freight mode (include_in_assessable_value = "Not Applicable") ──
    effective_date: str = "",
    gst_type_of_supply: str = "Services",
    taxability: str = "Taxable",
    gst_nature_of_transaction: str = "Local Sales - Taxable",
    hsn_sac_code: str = "",
    hsn_description: str = "",
    gst_rate: float = 0.0,
    is_reverse_charge: bool = False,
    is_ineligible_itc: bool = True,
    # ── Insurance mode (include_in_assessable_value = "GST") ──
    appropriate_to: str = "Goods",
    method_of_calculation: str = "Based on Value",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create an additional expense/income ledger in TallyPrime.

    Supports two mutually exclusive modes controlled by `include_in_assessable_value`:

    ┌─────────────────────────────────────────────────────────────────────────────┐
    │ Mode A — "Not Applicable"  (Transport & Freight style)                      │
    │   GSTAPPLICABLE = Applicable                                                │
    │   APPROPRIATEFOR = Not Applicable                                           │
    │   Parameters active: effective_date (mandatory), gst_type_of_supply,       │
    │     taxability, gst_nature_of_transaction, hsn_sac_code, hsn_description,  │
    │     gst_rate, is_reverse_charge, is_ineligible_itc                         │
    │   GST rate logic: IGST = gst_rate, CGST = SGST = gst_rate / 2             │
    │   AFFECTSSTOCK = No  (expense/income, not stock)                           │
    ├─────────────────────────────────────────────────────────────────────────────┤
    │ Mode B — "GST"  (Insurance style)                                           │
    │   GSTAPPLICABLE = Not Applicable                                            │
    │   APPROPRIATEFOR = GST   (included in assessable value for GST)             │
    │   Parameters active: appropriate_to, method_of_calculation                 │
    │   effective_date / gst_rate / HSN / gst_nature_of_transaction NOT used     │
    │   ISEXCISEAPPLICABLE = Yes, EXCISEALLOCTYPE = method_of_calculation        │
    └─────────────────────────────────────────────────────────────────────────────┘

    Based on "Transport & Freight" and "Insurance" ledgers from Tally Masters XML
    analysis (Master (1) (1) (1).xml).

    Args:
        name                        : Ledger name (e.g. "Transport & Freight",
                                      "Insurance", "Freight Charges")
        parent                      : Parent group.
                                      "Indirect Expenses" (default)
                                      "Indirect Incomes"
        include_in_assessable_value : Controls which mode is active.
                                      "Not Applicable" → GST applicable, rates/HSN used
                                      "GST"            → GST not applicable, included
                                                         in assessable value; appropriate_to
                                                         and method_of_calculation apply
        effective_date              : [Mode A — mandatory] Date from which GST details
                                      are effective. Formats: DD-MM-YYYY, DD/MM/YYYY,
                                      YYYY-MM-DD, or YYYYMMDD (e.g. "01-04-2025").
                                      Not used in Mode B.
        gst_type_of_supply          : [Mode A] "Services" (default) or "Goods"
        taxability                  : [Mode A] "Taxable", "Exempt", "Nil Rated",
                                      "Non-GST"
        gst_nature_of_transaction   : [Mode A] e.g. "Local Sales - Taxable",
                                      "Interstate Sales - Taxable"
        hsn_sac_code                : [Mode A] HSN code for goods / SAC for services
                                      (e.g. "998234" for freight)
        hsn_description             : [Mode A] Description of the HSN/SAC code
                                      (e.g. "Freight")
        gst_rate                    : [Mode A] Total GST % (e.g. 18).
                                      IGST = gst_rate, CGST = SGST = gst_rate / 2
        is_reverse_charge           : [Mode A] True → ISREVERSECHARGEAPPLICABLE=Yes
        is_ineligible_itc           : [Mode A] True → GSTINELIGIBLEITC=Yes
                                      (ITC not claimable; default True per Tally sample)
        appropriate_to              : [Mode B] "Goods" (default) or "Services"
                                      Maps to GSTAPPROPRIATETO
        method_of_calculation       : [Mode B] "Based on Value" (default) or
                                      "Based on Quantity". Maps to EXCISEALLOCTYPE
        tally_url                   : Optional Tally URL override
    """
    is_insurance_mode = (include_in_assessable_value.strip().upper() == "GST")
    if not is_insurance_mode:
        if not effective_date:
            raise ValueError("effective_date is mandatory when include_in_assessable_value is 'Not Applicable' (Mode A).")
        fy_start = _parse_date(effective_date)


    if is_insurance_mode:
        # ── Mode B: Insurance-style ──────────────────────────────────────────
        ledger_body = f"""          <GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>
          <TAXTYPE>Others</TAXTYPE>
          <APPROPRIATEFOR>GST</APPROPRIATEFOR>
          <GSTAPPROPRIATETO>{_xe(appropriate_to)}</GSTAPPROPRIATETO>
          <EXCISEALLOCTYPE>{_xe(method_of_calculation)}</EXCISEALLOCTYPE>
          <AFFECTSSTOCK>No</AFFECTSSTOCK>
          <ISCOSTCENTRESON>Yes</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
          <ISGSTAPPLICABLE>No</ISGSTAPPLICABLE>
          <ISEXCISEAPPLICABLE>Yes</ISEXCISEAPPLICABLE>
          <GSTDETAILS.LIST>          </GSTDETAILS.LIST>
          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>"""
    else:
        # ── Mode A: Transport & Freight-style ────────────────────────────────
        igst_rate = gst_rate
        cgst_rate = gst_rate / 2
        sgst_rate = gst_rate / 2
        rev_charge   = "Yes" if is_reverse_charge else "No"
        ineligible    = "Yes" if is_ineligible_itc  else "No"
        nature_xml = (
            f"            <GSTNATUREOFTRANSACTION>{_xe(gst_nature_of_transaction)}"
            f"</GSTNATUREOFTRANSACTION>\n"
            if gst_nature_of_transaction else ""
        )

        gst_details_xml = f"""          <GSTDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <TAXABILITY>{_xe(taxability)}</TAXABILITY>
{nature_xml}            <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
            <GSTCALCSLABONMRP>No</GSTCALCSLABONMRP>
            <ISREVERSECHARGEAPPLICABLE>{rev_charge}</ISREVERSECHARGEAPPLICABLE>
            <ISNONGSTGOODS>No</ISNONGSTGOODS>
            <GSTINELIGIBLEITC>{ineligible}</GSTINELIGIBLEITC>
            <INCLUDEEXPFORSLABCALC>No</INCLUDEEXPFORSLABCALC>
            <STATEWISEDETAILS.LIST>
              <STATENAME></STATENAME>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{cgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{sgst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                <GSTRATE>{igst_rate}</GSTRATE>
              </RATEDETAILS.LIST>
              <RATEDETAILS.LIST>
                <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
                <GSTRATEVALUATIONTYPE>Not Applicable</GSTRATEVALUATIONTYPE>
              </RATEDETAILS.LIST>
            </STATEWISEDETAILS.LIST>
          </GSTDETAILS.LIST>"""

        if hsn_sac_code:
            hsn_desc_xml = (
                f"            <HSN>{_xe(hsn_description)}</HSN>\n"
                if hsn_description else ""
            )
            hsn_xml = f"""          <HSNDETAILS.LIST>
            <APPLICABLEFROM>{fy_start}</APPLICABLEFROM>
            <HSNCODE>{_xe(hsn_sac_code)}</HSNCODE>
{hsn_desc_xml}            <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
          </HSNDETAILS.LIST>"""
        else:
            hsn_xml = "          <HSNDETAILS.LIST>          </HSNDETAILS.LIST>"

        ledger_body = f"""          <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
          <TAXTYPE>Others</TAXTYPE>
          <GSTTYPEOFSUPPLY>{_xe(gst_type_of_supply)}</GSTTYPEOFSUPPLY>
          <APPROPRIATEFOR>Not Applicable</APPROPRIATEFOR>
          <AFFECTSSTOCK>No</AFFECTSSTOCK>
          <ISCOSTCENTRESON>Yes</ISCOSTCENTRESON>
          <ISBILLWISEON>No</ISBILLWISEON>
          <ISGSTAPPLICABLE>No</ISGSTAPPLICABLE>
{gst_details_xml}
{hsn_xml}"""

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>All Masters</ID>
  </HEADER>
  <BODY>
    <DESC/>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="{_xe(name)}" ACTION="Create">
          <NAME>{_xe(name)}</NAME>
          <PARENT>{_xe(parent)}</PARENT>
{ledger_body}
        </LEDGER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    created = _rx(raw, "CREATED") or "0"
    altered = _rx(raw, "ALTERED") or "0"
    errors  = re.findall(r"<LINEERROR\b[^>]*>([^<]+)</LINEERROR>", raw, re.IGNORECASE)
    ok = (created != "0" or altered != "0") and not errors
    return {
        "status":      "success" if ok else ("error" if errors else "no_change"),
        "created":     created,
        "altered":     altered,
        "errors":      errors,
        "raw_status":  _rx(raw, "STATUS") or "",
        "ledger_name": name,
        "mode":        "insurance" if is_insurance_mode else "transport_freight",
        "tally_url":   _resolve_url(tally_url),
    }


def fetch_all_groups(tally_url: str | None = None) -> list[dict[str, Any]]:
    """Fetch all account groups from TallyPrime."""
    xml = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Groups</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="List of Groups" ISMODIFY="No">
            <TYPE>Group</TYPE>
            <FETCH>Name,Parent,IsDeemedPositive,IsRevenue,IsSubledger,IsAddable</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(xml, tally_url))
    return [
        {
            "name": g.get("NAME") or _find_text(g, "NAME"),
            "parent": _find_text(g, "PARENT"),
            "is_revenue": _find_text(g, "ISREVENUE"),
            "is_addable": _find_text(g, "ISADDABLE"),
        }
        for g in root.findall(".//GROUP")
    ]


# ─────────────────────────────────────────────
# VOUCHERS
# ─────────────────────────────────────────────

def fetch_vouchers(
    voucher_type: str = "",
    from_date: str = "",
    to_date: str = "",
    party_name: str = "",
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch vouchers with optional filters.

    Filters are applied via proper TDL FILTER + SYSTEM Formulae elements.
    Date range uses SVFROMDATE / SVTODATE in STATICVARIABLES (YYYYMMDD format).
    voucher_type and party_name use $VoucherTypeName / $PartyLedgerName field formulae.
    """
    # ── Date range via STATICVARIABLES ────────────────────────────────────────
    date_vars = ""
    if from_date:
        date_vars += f"<SVFROMDATE>{from_date}</SVFROMDATE>\n        "
    if to_date:
        date_vars += f"<SVTODATE>{to_date}</SVTODATE>\n        "

    # ── TDL FILTER references inside <COLLECTION> ─────────────────────────────
    # Each <FILTER> tag names a formula defined in a <SYSTEM TYPE="Formulae"> block.
    # Invalid tags like <FILTERVCH> / <FILTERLEDGERNAME> are silently ignored by Tally.
    filter_refs = []
    system_formulae = []

    if voucher_type:
        filter_refs.append("<FILTER>MCPVchTypeFilter</FILTER>")
        system_formulae.append(
            f'<SYSTEM TYPE="Formulae" NAME="MCPVchTypeFilter">'
            f'$VoucherTypeName = "{_xe(voucher_type)}"</SYSTEM>'
        )

    if party_name:
        filter_refs.append("<FILTER>MCPPartyFilter</FILTER>")
        system_formulae.append(
            f'<SYSTEM TYPE="Formulae" NAME="MCPPartyFilter">'
            f'$PartyLedgerName = "{_xe(party_name)}"</SYSTEM>'
        )

    filter_xml  = "\n            ".join(filter_refs)
    systems_xml = "\n          ".join(system_formulae)

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>Voucher Collection</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        {date_vars}<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="Voucher Collection" ISMODIFY="No">
            <TYPE>Voucher</TYPE>
            <FETCH>VoucherNumber,Date,VoucherTypeName,PartyLedgerName,
                   Narration,Amount,TotalAmount</FETCH>
            {filter_xml}
          </COLLECTION>
          {systems_xml}
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(xml, tally_url))

    def _pos(s: str) -> str:
        """Return absolute amount string — Tally stores voucher amounts with internal sign."""
        return s.lstrip("-").strip()

    return [
        {
            "voucher_number": _find_text(v, "VOUCHERNUMBER"),
            "date": _find_text(v, "DATE"),
            "voucher_type": _find_text(v, "VOUCHERTYPENAME"),
            "party": _find_text(v, "PARTYLEDGERNAME"),
            "narration": _find_text(v, "NARRATION"),
            "amount": _pos(_find_text(v, "AMOUNT")),
            "total_amount": _pos(_find_text(v, "TOTALAMOUNT")),
        }
        for v in root.findall(".//VOUCHER")
    ]


def _post_voucher(voucher_xml: str, tally_url: str | None = None) -> dict[str, Any]:
    """
    Wrap a <VOUCHER> fragment in the old-style 'Import Data' envelope that
    TallyPrime's XML Gateway reliably accepts across all versions.
    """
    envelope = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF='TallyUDF'>
          {voucher_xml}
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(envelope, tally_url))
    errors = [e.text for e in root.findall(".//LINEERROR") if e.text]
    created = root.find(".//CREATED")
    return {
        "status": "success" if not errors else "error",
        "created": created.text if created is not None else "0",
        "errors": errors,
    }


def _ledger_entries_xml(entries: list[dict[str, Any]]) -> str:
    lines = []
    for e in entries:
        is_debit = str(e.get("is_debit", False)).lower()
        lines.append(f"""<ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>{e['ledger']}</LEDGERNAME>
          <AMOUNT>{e['amount']}</AMOUNT>
          <ISDEEMEDPOSITIVE>{is_debit}</ISDEEMEDPOSITIVE>
        </ALLLEDGERENTRIES.LIST>""")
    return "\n".join(lines)


def _item_net_amount(item: dict[str, Any]) -> float:
    """
    Return the net amount for an inventory line.
    amount in the JSON is already the final net value — passed through directly.
    """
    return float(item["amount"])


def _build_inventory_entry(
    item: dict[str, Any],
    cgst_rate: float,
    sgst_rate: float,
    igst_rate: float,
) -> str:
    """
    Build a single <ALLINVENTORYENTRIES.LIST> XML block for one stock item.
    All values are mapped directly from the item dict — no computation performed.

    Expected item dict keys:
        stock_item_name   (required)
        sales_ledger      (required) — credited via ACCOUNTINGALLOCATIONS
        amount            (required) — net line amount (already post-discount), mapped as-is
        rate              (optional) — mapped as-is to <RATE>
        quantity          (optional)
        unit              (optional)
        gst_rate          (optional) — per-item GST %; overrides fallback rates in RATEDETAILS
        discount_percent  (optional) — mapped as-is to <DISCOUNT>
        discount_amount   (optional) — mapped as-is to <DISCOUNTAMOUNT> / <BATCHDISCOUNTAMOUNT>
    """
    name    = _xe(item["stock_item_name"])
    ledger  = _xe(item["sales_ledger"])
    amount  = float(item["amount"])          # net amount — direct passthrough
    rate_v  = float(item.get("rate", 0))
    qty_v   = float(item.get("quantity", 0))
    unit_v  = _xe(str(item.get("unit", "")))

    # ── Discount fields — direct passthrough, no computation ─────────────────
    disc_pct = item.get("discount_percent")
    disc_amt = item.get("discount_amount")

    disc_pct_xml       = f"<DISCOUNT>{disc_pct}</DISCOUNT>"                       if disc_pct not in (None, "", 0, 0.0) else ""
    disc_amt_xml       = f"<DISCOUNTAMOUNT>{disc_amt}</DISCOUNTAMOUNT>"             if disc_amt not in (None, "", 0, 0.0) else ""
    batch_disc_amt_xml = f"<BATCHDISCOUNTAMOUNT>{disc_amt}</BATCHDISCOUNTAMOUNT>"   if disc_amt not in (None, "", 0, 0.0) else ""

    # ── Per-item GST rate ─────────────────────────────────────────────────────
    item_gst = item.get("gst_rate")
    if item_gst is not None and float(item_gst) != 0:
        line_cgst_rate = round(float(item_gst) / 2, 4)
        line_sgst_rate = round(float(item_gst) / 2, 4)
        line_igst_rate = float(item_gst)
    else:
        line_cgst_rate = cgst_rate
        line_sgst_rate = sgst_rate
        line_igst_rate = igst_rate

    rate_str = f"{rate_v}/{unit_v}" if unit_v else str(rate_v)
    qty_str  = f"{qty_v} {unit_v}".strip() if unit_v else str(qty_v)

    return f"""<ALLINVENTORYENTRIES.LIST>
    <STOCKITEMNAME>{name}</STOCKITEMNAME>
    <GSTOVRDNTAXABILITY>Taxable</GSTOVRDNTAXABILITY>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <GSTOVRDNISREVCHARGEAPPL>Not Applicable</GSTOVRDNISREVCHARGEAPPL>
    <RATE>{rate_str}</RATE>
    {disc_pct_xml}
    <AMOUNT>{amount}</AMOUNT>
    {disc_amt_xml}
    <ACTUALQTY>{qty_str}</ACTUALQTY>
    <BILLEDQTY>{qty_str}</BILLEDQTY>
    <BATCHALLOCATIONS.LIST>
      <GODOWNNAME>Main Location</GODOWNNAME>
      <BATCHNAME>Primary Batch</BATCHNAME>
      <AMOUNT>{amount}</AMOUNT>
      {batch_disc_amt_xml}
      <ACTUALQTY>{qty_str}</ACTUALQTY>
      <BILLEDQTY>{qty_str}</BILLEDQTY>
    </BATCHALLOCATIONS.LIST>
    <ACCOUNTINGALLOCATIONS.LIST>
      <LEDGERNAME>{ledger}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>{amount}</AMOUNT>
    </ACCOUNTINGALLOCATIONS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_cgst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_sgst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_igst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Not Applicable</GSTRATEVALUATIONTYPE>
    </RATEDETAILS.LIST>
  </ALLINVENTORYENTRIES.LIST>"""


def create_sales_voucher(
    date: str,
    party_ledger: str,
    items: list[dict[str, Any]],
    voucher_type: str = "Sales",
    voucher_number: str = "",
    narration: str = "",
    # ── GST ledgers (voucher-level) ───────────────────────────────────────────
    cgst_ledger: str = "",
    cgst_amount: float = 0.0,
    sgst_ledger: str = "",
    sgst_amount: float = 0.0,
    igst_ledger: str = "",
    igst_amount: float = 0.0,
    # ── Additional voucher-level ledgers (Freight, Insurance, Discount, etc.) ─
    additional_ledgers: list[dict[str, Any]] | None = None,
    # ── GST header fields ─────────────────────────────────────────────────────
    gst_registration_type: str = "",
    party_gstin: str = "",
    place_of_supply: str = "",
    state_name: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """
    Create a Sales (invoice) voucher in TallyPrime.

    items: list of dicts, each with:
        stock_item_name  (str, required)
        sales_ledger     (str, required) — per-item sales/income ledger
        amount           (float, required) — GROSS line amount before discount
        rate             (float, optional)
        quantity         (float, optional)
        unit             (str, optional)
        gst_rate         (float, optional) — per-line GST % (e.g. 5, 12, 18, 28; 0 if exempt)
        discount_percent (float, optional) — used when discount_amount absent
        discount_amount  (float, optional) — takes priority over discount_percent

    additional_ledgers: list of dicts with ledger_name, amount, is_addition (True=charge, False=deduction).
    cgst_ledger/sgst_ledger/igst_ledger: voucher-level GST output ledgers.
    Party debit = sum(net item amounts) + additions - deductions + GST.
    """
    item_list = items or []

    # ── Normalise additional ledgers ──────────────────────────────────────────
    extra_ledgers = additional_ledgers or []

    # ── Optional header XML ───────────────────────────────────────────────────
    vnum_xml   = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""
    gstreg_xml = f"<GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>" if gst_registration_type else ""
    gstin_xml  = f"<PARTYGSTIN>{_xe(party_gstin)}</PARTYGSTIN>"           if party_gstin    else ""
    pos_xml    = f"<PLACEOFSUPPLY>{_xe(place_of_supply)}</PLACEOFSUPPLY>" if place_of_supply else ""
    state_xml  = f"<STATENAME>{_xe(state_name)}</STATENAME>"              if state_name     else ""

    vch_type_safe = _xe(voucher_type)

    # ── Discount flags (voucher-level) ────────────────────────────────────────
    def _has_discount(it: dict) -> bool:
        return (it.get("discount_amount") not in (None, "", 0, 0.0) or
                it.get("discount_percent") not in (None, "", 0, 0.0))

    any_discount = any(_has_discount(i) for i in item_list)
    has_discounts_xml   = "<HASDISCOUNTS>Yes</HASDISCOUNTS>"               if any_discount else ""
    discount_format_xml = "<DISCOUNTFORMAT>Both Percentage &amp; Amount</DISCOUNTFORMAT>" if any_discount else ""

    # ── Compute party debit total (uses NET amounts after item-level discounts) ─
    base_total = sum(_item_net_amount(i) for i in item_list)
    total = base_total
    for ex in extra_ledgers:
        ex_amt = float(ex["amount"])
        if ex.get("is_addition", True):
            total += ex_amt   # Freight, Insurance etc. add to party total
        else:
            total -= ex_amt   # Discount etc. reduce party total
    if cgst_ledger and cgst_amount:
        total += cgst_amount
    if sgst_ledger and sgst_amount:
        total += sgst_amount
    if igst_ledger and igst_amount:
        total += igst_amount

    # ── Item Invoice mode ─────────────────────────────────────────────────────
    # Each item supplies its own gst_rate; fallback rates are 0 (no voucher-level fallback)
    inv_xml = "\n  ".join(
        _build_inventory_entry(it, 0.0, 0.0, 0.0)
        for it in item_list
    )

    gst_xml = ""
    if cgst_ledger and cgst_amount:
        gst_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(cgst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{cgst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """
    if sgst_ledger and sgst_amount:
        gst_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(sgst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{sgst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """
    if igst_ledger and igst_amount:
        gst_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(igst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{igst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """

    # ── Additional non-GST ledger entries (Freight, Insurance, Discount, etc.) ─
    for ex in extra_ledgers:
        ex_amt      = float(ex["amount"])
        is_addition = ex.get("is_addition", True)
        deemed_pos  = "No" if is_addition else "Yes"
        xml_amt     = ex_amt if is_addition else -ex_amt
        gst_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(ex["ledger_name"])}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>{deemed_pos}</ISDEEMEDPOSITIVE>
    <AMOUNT>{xml_amt}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """

    voucher_xml = f"""<VOUCHER REMOTEID="" VCHKEY="" VCHTYPE="{vch_type_safe}" ACTION="Create" OBJVIEW="Invoice Voucher View">
  <OBJECTUPDATEACTION>Create</OBJECTUPDATEACTION>
  <ISINVOICE>Yes</ISINVOICE>
  <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
  <DATE>{date}</DATE>
  <VOUCHERTYPENAME>{vch_type_safe}</VOUCHERTYPENAME>
  {vnum_xml}
  <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
  {gstreg_xml}
  {state_xml}
  {gstin_xml}
  {pos_xml}
  {has_discounts_xml}
  {discount_format_xml}
  <NARRATION>{_xe(narration)}</NARRATION>
  {inv_xml}
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
    <AMOUNT>-{total}</AMOUNT>
  </LEDGERENTRIES.LIST>
  {gst_xml}
</VOUCHER>"""

    return _post_voucher(voucher_xml, tally_url)


def _build_purchase_inventory_entry(
    item: dict[str, Any],
    cgst_rate: float,
    sgst_rate: float,
    igst_rate: float,
) -> str:
    """
    Build a single <ALLINVENTORYENTRIES.LIST> XML block for one purchased stock item.

    Sign conventions (Purchase — opposite of Sales):
      • ISDEEMEDPOSITIVE=Yes  (stock is debited — inward movement)
      • ISLASTDEEMEDPOSITIVE=Yes
      • AMOUNT  = negative (caller passes positive net amount, we negate for XML)
      • DISCOUNTAMOUNT / BATCHDISCOUNTAMOUNT = negative (same rule)
      • ACCOUNTINGALLOCATIONS: ISDEEMEDPOSITIVE=Yes, AMOUNT negative

    Expected item dict keys:
        stock_item_name   (required)
        purchase_ledger   (required) — debited via ACCOUNTINGALLOCATIONS
        amount            (required) — net line amount (positive), negated for XML
        rate              (optional)
        quantity          (optional)
        unit              (optional)
        gst_rate          (optional) — per-item GST %
        discount_percent  (optional) — mapped as-is to <DISCOUNT>
        discount_amount   (optional) — caller passes positive; negated for XML
    """
    name    = _xe(item["stock_item_name"])
    ledger  = _xe(item["purchase_ledger"])
    amount  = float(item["amount"])          # caller provides positive net amount
    rate_v  = float(item.get("rate", 0))
    qty_v   = float(item.get("quantity", 0))
    unit_v  = _xe(str(item.get("unit", "")))

    # ── Discount fields ───────────────────────────────────────────────────────
    disc_pct = item.get("discount_percent")
    disc_amt = item.get("discount_amount")

    disc_pct_xml = f"<DISCOUNT>{disc_pct}</DISCOUNT>" if disc_pct not in (None, "", 0, 0.0) else ""
    # Purchase: discount amounts are negative in XML
    if disc_amt not in (None, "", 0, 0.0):
        neg_disc = -abs(float(disc_amt))
        disc_amt_xml       = f"<DISCOUNTAMOUNT>{neg_disc}</DISCOUNTAMOUNT>"
        batch_disc_amt_xml = f"<BATCHDISCOUNTAMOUNT>{neg_disc}</BATCHDISCOUNTAMOUNT>"
    else:
        disc_amt_xml = batch_disc_amt_xml = ""

    # ── Per-item GST rate ─────────────────────────────────────────────────────
    item_gst = item.get("gst_rate")
    if item_gst is not None and float(item_gst) != 0:
        line_cgst_rate = round(float(item_gst) / 2, 4)
        line_sgst_rate = round(float(item_gst) / 2, 4)
        line_igst_rate = float(item_gst)
    else:
        line_cgst_rate = cgst_rate
        line_sgst_rate = sgst_rate
        line_igst_rate = igst_rate

    rate_str   = f"{rate_v}/{unit_v}" if unit_v else str(rate_v)
    qty_str    = f"{qty_v} {unit_v}".strip() if unit_v else str(qty_v)
    xml_amount = -amount                     # negate for purchase XML

    return f"""<ALLINVENTORYENTRIES.LIST>
    <STOCKITEMNAME>{name}</STOCKITEMNAME>
    <GSTOVRDNTAXABILITY>Taxable</GSTOVRDNTAXABILITY>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
    <GSTOVRDNISREVCHARGEAPPL>Not Applicable</GSTOVRDNISREVCHARGEAPPL>
    <RATE>{rate_str}</RATE>
    {disc_pct_xml}
    <AMOUNT>{xml_amount}</AMOUNT>
    {disc_amt_xml}
    <ACTUALQTY>{qty_str}</ACTUALQTY>
    <BILLEDQTY>{qty_str}</BILLEDQTY>
    <BATCHALLOCATIONS.LIST>
      <GODOWNNAME>Main Location</GODOWNNAME>
      <BATCHNAME>Primary Batch</BATCHNAME>
      <AMOUNT>{xml_amount}</AMOUNT>
      {batch_disc_amt_xml}
      <ACTUALQTY>{qty_str}</ACTUALQTY>
      <BILLEDQTY>{qty_str}</BILLEDQTY>
    </BATCHALLOCATIONS.LIST>
    <ACCOUNTINGALLOCATIONS.LIST>
      <LEDGERNAME>{ledger}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
      <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
      <AMOUNT>{xml_amount}</AMOUNT>
    </ACCOUNTINGALLOCATIONS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_cgst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_sgst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{line_igst_rate}</GSTRATE>
    </RATEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Not Applicable</GSTRATEVALUATIONTYPE>
    </RATEDETAILS.LIST>
  </ALLINVENTORYENTRIES.LIST>"""


def create_purchase_voucher(
    date: str,
    party_ledger: str,
    items: list[dict[str, Any]],
    voucher_type: str = "Purchase",
    voucher_number: str = "",
    reference: str = "",           # supplier's bill / invoice number
    narration: str = "",
    # ── GST ledgers (input tax credit) ───────────────────────────────────────
    cgst_ledger: str = "",
    cgst_amount: float = 0.0,
    sgst_ledger: str = "",
    sgst_amount: float = 0.0,
    igst_ledger: str = "",
    igst_amount: float = 0.0,
    # ── Additional voucher-level ledgers (Freight, Discount Received, etc.) ──
    additional_ledgers: list[dict[str, Any]] | None = None,
    # ── GST header fields ─────────────────────────────────────────────────────
    gst_registration_type: str = "",
    party_gstin: str = "",
    place_of_supply: str = "",
    state_name: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """
    Create a Purchase (invoice) voucher in TallyPrime using Item Invoice mode.

    Sign conventions (from Purchase XML analysis):
      • Inventory / accounting allocation amounts → negative  (ISDEEMEDPOSITIVE=Yes)
      • Party LEDGERENTRIES amount → positive (supplier credited, ISDEEMEDPOSITIVE=No)
      • GST input-tax LEDGERENTRIES amount → negative (debit to input ledger, ISDEEMEDPOSITIVE=Yes)
      • reference → supplier bill number; creates BILLALLOCATIONS.LIST entry for payables tracking

    items: list of dicts, each with:
        stock_item_name  (str, required)
        purchase_ledger  (str, required) — per-item purchase/expense ledger
        amount           (float, required) — NET line amount (positive); negated for XML
        rate             (float, optional)
        quantity         (float, optional)
        unit             (str, optional)
        gst_rate         (float, optional) — per-line GST %
        discount_percent (float, optional)
        discount_amount  (float, optional) — pass as positive; negated for XML
    """
    item_list     = items or []
    extra_ledgers = additional_ledgers or []

    # ── Optional header XML ───────────────────────────────────────────────────
    vnum_xml   = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""
    ref_xml    = f"<REFERENCE>{_xe(reference)}</REFERENCE>"              if reference      else ""
    gstreg_xml = f"<GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>" if gst_registration_type else ""
    gstin_xml  = f"<PARTYGSTIN>{_xe(party_gstin)}</PARTYGSTIN>"           if party_gstin    else ""
    pos_xml    = f"<PLACEOFSUPPLY>{_xe(place_of_supply)}</PLACEOFSUPPLY>" if place_of_supply else ""
    state_xml  = f"<STATENAME>{_xe(state_name)}</STATENAME>"              if state_name     else ""

    vch_type_safe = _xe(voucher_type)

    # ── Discount flags (voucher-level) ────────────────────────────────────────
    def _has_discount(it: dict) -> bool:
        return (it.get("discount_amount") not in (None, "", 0, 0.0) or
                it.get("discount_percent") not in (None, "", 0, 0.0))

    any_discount = any(_has_discount(i) for i in item_list)
    has_discounts_xml   = "<HASDISCOUNTS>Yes</HASDISCOUNTS>"                           if any_discount else ""
    discount_format_xml = "<DISCOUNTFORMAT>Both Percentage &amp; Amount</DISCOUNTFORMAT>" if any_discount else ""

    # ── Party (supplier) total — positive ─────────────────────────────────────
    base_total = sum(float(i["amount"]) for i in item_list)
    total = base_total
    for ex in extra_ledgers:
        ex_amt = float(ex["amount"])
        if ex.get("is_addition", True):
            total += ex_amt
        else:
            total -= ex_amt
    if cgst_ledger and cgst_amount:
        total += cgst_amount
    if sgst_ledger and sgst_amount:
        total += sgst_amount
    if igst_ledger and igst_amount:
        total += igst_amount

    # ── Inventory entries (item invoice mode) ─────────────────────────────────
    inv_xml = "\n  ".join(
        _build_purchase_inventory_entry(it, 0.0, 0.0, 0.0)
        for it in item_list
    )

    # ── GST LEDGERENTRIES (input tax credit) ──────────────────────────────────
    # ISDEEMEDPOSITIVE=Yes, amounts negative (debit to GST Input ledger)
    ledger_xml = ""
    if cgst_ledger and cgst_amount:
        ledger_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(cgst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <AMOUNT>-{cgst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """
    if sgst_ledger and sgst_amount:
        ledger_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(sgst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <AMOUNT>-{sgst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """
    if igst_ledger and igst_amount:
        ledger_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(igst_ledger)}</LEDGERNAME>
    <METHODTYPE>GST</METHODTYPE>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <AMOUNT>-{igst_amount}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """

    # ── Additional non-GST ledger entries ─────────────────────────────────────
    # is_addition=True  → expense/charge (debit): ISDEEMEDPOSITIVE=Yes, amount negative
    # is_addition=False → income/deduction (credit): ISDEEMEDPOSITIVE=No, amount positive
    for ex in extra_ledgers:
        ex_amt      = float(ex["amount"])
        is_addition = ex.get("is_addition", True)
        deemed_pos  = "Yes" if is_addition else "No"
        xml_amt     = -ex_amt if is_addition else ex_amt
        ledger_xml += f"""<LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(ex["ledger_name"])}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>{deemed_pos}</ISDEEMEDPOSITIVE>
    <AMOUNT>{xml_amt}</AMOUNT>
  </LEDGERENTRIES.LIST>
  """

    # ── Bill allocation (for payables tracking) ───────────────────────────────
    bill_alloc_xml = ""
    if reference:
        bill_alloc_xml = f"""<BILLALLOCATIONS.LIST>
      <NAME>{_xe(reference)}</NAME>
      <BILLTYPE>New Ref</BILLTYPE>
      <AMOUNT>{total}</AMOUNT>
    </BILLALLOCATIONS.LIST>"""

    voucher_xml = f"""<VOUCHER REMOTEID="" VCHKEY="" VCHTYPE="{vch_type_safe}" ACTION="Create" OBJVIEW="Invoice Voucher View">
  <OBJECTUPDATEACTION>Create</OBJECTUPDATEACTION>
  <ISINVOICE>Yes</ISINVOICE>
  <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
  <DATE>{date}</DATE>
  <VOUCHERTYPENAME>{vch_type_safe}</VOUCHERTYPENAME>
  {vnum_xml}
  {ref_xml}
  <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
  {gstreg_xml}
  {state_xml}
  {gstin_xml}
  {pos_xml}
  {has_discounts_xml}
  {discount_format_xml}
  <NARRATION>{_xe(narration)}</NARRATION>
  {inv_xml}
  <LEDGERENTRIES.LIST>
    <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{total}</AMOUNT>
    {bill_alloc_xml}
  </LEDGERENTRIES.LIST>
  {ledger_xml}
</VOUCHER>"""

    return _post_voucher(voucher_xml, tally_url)


def create_payment_voucher(
    date: str, party_ledger: str, bank_or_cash_ledger: str, amount: float,
    voucher_number: str = "", narration: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    vnum_xml = f"<VOUCHERNUMBER>{voucher_number}</VOUCHERNUMBER>" if voucher_number else ""
    return _post_voucher(f"""<VOUCHER REMOTEID="" VCHTYPE="Payment" ACTION="Create" OBJVIEW="Accounting Voucher View">
  <DATE>{date}</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
  {vnum_xml}<PARTYLEDGERNAME>{party_ledger}</PARTYLEDGERNAME>
  <NARRATION>{narration}</NARRATION>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>{party_ledger}</LEDGERNAME>
    <AMOUNT>-{amount}</AMOUNT><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE></ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>{bank_or_cash_ledger}</LEDGERNAME>
    <AMOUNT>{amount}</AMOUNT><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE></ALLLEDGERENTRIES.LIST>
</VOUCHER>""", tally_url)


def create_receipt_voucher(
    date: str, party_ledger: str, bank_or_cash_ledger: str, amount: float,
    voucher_number: str = "", narration: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    vnum_xml = f"<VOUCHERNUMBER>{voucher_number}</VOUCHERNUMBER>" if voucher_number else ""
    return _post_voucher(f"""<VOUCHER REMOTEID="" VCHTYPE="Receipt" ACTION="Create" OBJVIEW="Accounting Voucher View">
  <DATE>{date}</DATE><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
  {vnum_xml}<PARTYLEDGERNAME>{party_ledger}</PARTYLEDGERNAME>
  <NARRATION>{narration}</NARRATION>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>{bank_or_cash_ledger}</LEDGERNAME>
    <AMOUNT>{amount}</AMOUNT><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE></ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST><LEDGERNAME>{party_ledger}</LEDGERNAME>
    <AMOUNT>-{amount}</AMOUNT><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE></ALLLEDGERENTRIES.LIST>
</VOUCHER>""", tally_url)


def create_journal_voucher(
    date: str, entries: list[dict[str, Any]],
    voucher_number: str = "", narration: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    vnum_xml = f"<VOUCHERNUMBER>{voucher_number}</VOUCHERNUMBER>" if voucher_number else ""
    return _post_voucher(f"""<VOUCHER REMOTEID="" VCHTYPE="Journal" ACTION="Create" OBJVIEW="Accounting Voucher View">
  <DATE>{date}</DATE><VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
  {vnum_xml}<NARRATION>{narration}</NARRATION>
  {_ledger_entries_xml(entries)}
</VOUCHER>""", tally_url)


# ─────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────

def fetch_trial_balance(
    from_date: str = "",
    to_date: str = "",
    include_opening: bool = True,
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch trial balance using Tally's built-in Trial Balance report.

    Uses TYPE=Data with ID='Trial Balance' which triggers Tally's own report
    computation engine — the same engine used when you export Trial Balance
    from Tally's UI (File > Export > XML). This reliably handles any date
    range without the hanging issues caused by custom TDL Ledger collection
    period-balance computation (which forces Tally to recompute all vouchers
    on-the-fly and can hang indefinitely).

    Response mirrors the actual TrialBal XML structure (DSP* display format):
      DSPOPDRAMTA → Opening Debit amount
      DSPOPCRAMTA → Opening Credit amount
      DSPCLDRAMTA → Closing Debit amount
      DSPCLCRAMTA → Closing Credit amount

    Both group-level and ledger-level rows are returned in document order,
    matching Tally's on-screen Trial Balance hierarchy.

    Args:
        include_opening: When True (default) returns opening_dr/opening_cr columns.
                         When False only closing_dr/closing_cr are returned,
                         matching Tally's "without opening balance" view.
    """
    date_vars = ""
    if from_date:
        date_vars += f"        <SVFROMDATE>{from_date}</SVFROMDATE>\n"
    if to_date:
        date_vars += f"        <SVTODATE>{to_date}</SVTODATE>\n"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Trial Balance</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{date_vars}        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    root = _parse_xml(_post_xml(xml, tally_url, timeout=120.0))

    result = []
    children = list(root)
    i = 0
    while i < len(children):
        child = children[i]
        if child.tag == "DSPACCNAME":
            name_el = child.find("DSPDISPNAME")
            name = (name_el.text or "").strip() if name_el is not None else ""
            entry: dict[str, Any] = {"name": name}
            # Paired DSPACCINFO sibling immediately follows
            if i + 1 < len(children) and children[i + 1].tag == "DSPACCINFO":
                i += 1
                info = children[i]
                cl_dr = (info.findtext("DSPCLDRAMT/DSPCLDRAMTA") or "").strip()
                cl_cr = (info.findtext("DSPCLCRAMT/DSPCLCRAMTA") or "").strip()
                entry["closing_dr"] = cl_dr
                entry["closing_cr"] = cl_cr
                if include_opening:
                    entry["opening_dr"] = (info.findtext("DSPOPDRAMT/DSPOPDRAMTA") or "").strip()
                    entry["opening_cr"] = (info.findtext("DSPOPCRAMT/DSPOPCRAMTA") or "").strip()
            result.append(entry)
        i += 1
    return result


def fetch_daybook(from_date: str = "", to_date: str = "", tally_url: str | None = None) -> list[dict[str, Any]]:
    return fetch_vouchers(from_date=from_date, to_date=to_date, tally_url=tally_url)


def fetch_balance_sheet(from_date: str = "", to_date: str = "", tally_url: str | None = None) -> dict[str, Any]:
    """Fetch Balance Sheet using Tally's built-in Balance Sheet report.

    Uses TYPE=Data with ID='Balance Sheet' which triggers Tally's own report
    computation engine — the same engine used when you export Balance Sheet
    from Tally's UI. This avoids the hanging issues caused by custom TDL
    Ledger collection period-balance computation.

    Mirrors the actual BSheet XML structure (BS* display format):
      BSSUBAMT  → individual ledger/account amount
      BSMAINAMT → group total

    Both sides (Liabilities and Assets) are returned as a flat ordered list
    in document order, matching Tally's on-screen Balance Sheet sequence.
    Group rows have main_amount populated; individual ledger rows have sub_amount.
    """
    date_vars = ""
    if from_date:
        date_vars += f"        <SVFROMDATE>{from_date}</SVFROMDATE>\n"
    if to_date:
        date_vars += f"        <SVTODATE>{to_date}</SVTODATE>\n"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Balance Sheet</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{date_vars}        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    root = _parse_xml(_post_xml(xml, tally_url, timeout=120.0))

    entries = []
    children = list(root)
    i = 0
    while i < len(children):
        child = children[i]
        if child.tag == "BSNAME":
            dname = child.findtext("DSPACCNAME/DSPDISPNAME") or ""
            entry: dict[str, Any] = {"name": dname.strip()}
            if i + 1 < len(children) and children[i + 1].tag == "BSAMT":
                i += 1
                amt = children[i]
                entry["sub_amount"]  = (amt.findtext("BSSUBAMT")  or "").strip()
                entry["main_amount"] = (amt.findtext("BSMAINAMT") or "").strip()
            entries.append(entry)
        i += 1
    return {"entries": entries}


def fetch_profit_loss(from_date: str = "", to_date: str = "", tally_url: str | None = None) -> dict[str, Any]:
    """Fetch Profit & Loss using Tally's built-in Profit & Loss report.

    Uses TYPE=Data with ID='Profit and Loss' which triggers Tally's own
    report computation engine — the same engine used when you export P&L
    from Tally's UI. This avoids the hanging issues caused by custom TDL
    Ledger collection period-balance computation.

    Mirrors the actual PandL XML structure (mixed PL*/BS* display format):
      PLSUBAMT / BSSUBAMT → individual ledger/account amount (sub_amount)
      BSMAINAMT           → group total                      (main_amount)

    Both income and expense entries are returned as a flat ordered list
    in document order, matching Tally's on-screen P&L hierarchy.
    Group rows have main_amount populated; individual ledger rows have sub_amount.
    """
    date_vars = ""
    if from_date:
        date_vars += f"        <SVFROMDATE>{from_date}</SVFROMDATE>\n"
    if to_date:
        date_vars += f"        <SVTODATE>{to_date}</SVTODATE>\n"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Profit and Loss</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{date_vars}        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    root = _parse_xml(_post_xml(xml, tally_url, timeout=120.0))

    entries = []
    children = list(root)
    i = 0
    while i < len(children):
        child = children[i]
        name = ""
        sub_amount = ""
        main_amount = ""
        consumed = False

        if child.tag == "DSPACCNAME":
            # Direct DSPACCNAME → paired with PLAMT or BSAMT
            name_el = child.find("DSPDISPNAME")
            name = (name_el.text or "").strip() if name_el is not None else ""
            if i + 1 < len(children) and children[i + 1].tag in ("PLAMT", "BSAMT"):
                i += 1
                amt = children[i]
                sub_amount  = (amt.findtext("PLSUBAMT") or amt.findtext("BSSUBAMT") or "").strip()
                main_amount = (amt.findtext("BSMAINAMT") or "").strip()
            consumed = True

        elif child.tag == "BSNAME":
            # BSNAME wraps DSPACCNAME → paired with BSAMT
            dname = child.findtext("DSPACCNAME/DSPDISPNAME") or ""
            name = dname.strip()
            if i + 1 < len(children) and children[i + 1].tag == "BSAMT":
                i += 1
                amt = children[i]
                sub_amount  = (amt.findtext("BSSUBAMT")  or "").strip()
                main_amount = (amt.findtext("BSMAINAMT") or "").strip()
            consumed = True

        if consumed and name:
            entries.append({"name": name, "sub_amount": sub_amount, "main_amount": main_amount})
        i += 1
    return {"entries": entries}


def fetch_stock_summary(tally_url: str | None = None) -> list[dict[str, Any]]:
    xml = """<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE><ID>Stock Collection</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="Stock Collection" ISMODIFY="No">
        <TYPE>Stock Item</TYPE>
        <FETCH>Name,Parent,ClosingBalance,ClosingValue,BaseUnits,
               OpeningBalance,OpeningValue,StandardCost,StandardSellingPrice</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(xml, tally_url))
    return [
        {
            "name": item.get("NAME") or _find_text(item, "NAME"),
            "parent": _find_text(item, "PARENT"),
            "base_unit": _find_text(item, "BASEUNITS"),
            "opening_qty": _find_text(item, "OPENINGBALANCE"),
            "opening_value": _find_text(item, "OPENINGVALUE"),
            "closing_qty": _find_text(item, "CLOSINGBALANCE"),
            "closing_value": _find_text(item, "CLOSINGVALUE"),
            "standard_cost": _find_text(item, "STANDARDCOST"),
            "standard_price": _find_text(item, "STANDARDSELLINGPRICE"),
        }
        for item in root.findall(".//STOCKITEM")
    ]


# ─────────────────────────────────────────────
# OUTSTANDING RECEIVABLES
# ─────────────────────────────────────────────

# Month abbreviations used in Tally's D-Mon-YY date format
_TALLY_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_tally_date(s: str) -> "date | None":
    """Parse Tally's 'D-Mon-YY' date string (e.g. '2-Jan-26', '14-Mar-26').

    Returns a datetime.date object or None if parsing fails.
    Year mapping: 00-49 → 2000-2049, 50-99 → 1950-1999.
    """
    from datetime import date as _date_cls
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2})$", s.strip())
    if not m:
        return None
    day  = int(m.group(1))
    mon  = _TALLY_MONTHS.get(m.group(2).capitalize())
    yr2  = int(m.group(3))
    if mon is None:
        return None
    year = 2000 + yr2 if yr2 < 50 else 1900 + yr2
    try:
        return _date_cls(year, mon, day)
    except ValueError:
        return None


def _parse_ledger_outstanding_bills(
    root: Any,
    single_party: str = "",
) -> dict[str, dict[str, float]]:
    """Parse BILLOP values from a Ledger Outstanding XML response.

    Handles two formats:

    **Multi-party** (no SVLEDGER filter, all debtors in one response):
      BILLPARTY tag separates each ledger's block.
      Structure per block: BILLPARTY → BILLFIXED(BILLREF + ...) → BILLOP → ... → BILLOVERDUE

    **Single-party** (SVLEDGER set, one ledger's bills only):
      No BILLPARTY tags.  All BILLFIXEDs belong to ``single_party``.
      Structure: BILLFIXED(BILLREF + ...) → BILLOP → ... → BILLOVERDUE

    Returns
    -------
    dict  party_name → {bill_ref → opening_amount}
    """
    result:      dict[str, dict[str, float]] = {}
    current_party: str               = single_party
    party_bills:   dict[str, float]  = {}
    pending_ref:   str               = ""
    pending_op:    float | None      = None
    has_billparty: bool              = False

    for elem in root.iter():
        tag = elem.tag.upper()

        if tag == "BILLPARTY":
            has_billparty = True
            # Commit any accumulated bills for the previous party
            if current_party and party_bills:
                result.setdefault(current_party, {}).update(party_bills)
            current_party = (elem.text or "").strip()
            party_bills   = {}
            pending_ref   = ""
            pending_op    = None

        elif tag == "BILLFIXED":
            # New bill row — reset accumulators
            pending_ref = ""
            pending_op  = None

        elif tag == "BILLREF":
            pending_ref = (elem.text or "").strip()

        elif tag == "BILLOP":
            s = (elem.text or "").strip()
            if s:
                try:
                    pending_op = abs(float(s))
                except ValueError:
                    pass

        elif tag == "BILLOVERDUE":
            # BILLOVERDUE is the last tag in each bill row — commit the record
            if pending_ref and pending_op is not None and pending_op > 0:
                party_bills[pending_ref] = pending_op
            pending_ref = ""
            pending_op  = None

    # Flush the last party block
    if current_party and party_bills:
        result.setdefault(current_party, {}).update(party_bills)

    return result


def _parse_ledger_collection_bills(
    root: Any,
) -> tuple[dict[str, dict[str, float]], int, int]:
    """Parse bill opening amounts from a TYPE=Collection Ledger response.

    Expected XML structure:

        <LEDGER NAME="PartyName">
          <BILLALLOCATIONS>
            <NAME>bill_ref</NAME>
            <AMOUNT>opening_amount</AMOUNT>
          </BILLALLOCATIONS>
        </LEDGER>

    Returns
    -------
    (result_dict, ledger_count, billalloc_count)
      result_dict    : party_name → {bill_ref → opening_amount}
      ledger_count   : number of <LEDGER> elements found (diagnostic)
      billalloc_count: number of <BILLALLOCATIONS> elements found (diagnostic)
    """
    result:          dict[str, dict[str, float]] = {}
    ledger_count:    int = 0
    billalloc_count: int = 0

    for ledger_elem in root.iter("LEDGER"):
        ledger_count += 1
        party = (ledger_elem.get("NAME") or ledger_elem.get("name") or "").strip()
        if not party:
            name_child = ledger_elem.find("NAME")
            if name_child is not None:
                party = (name_child.text or "").strip()
        if not party:
            continue

        party_bills: dict[str, float] = {}
        for ba in ledger_elem.iter("BILLALLOCATIONS"):
            billalloc_count += 1
            ref_el = ba.find("NAME")
            amt_el = ba.find("AMOUNT")
            if ref_el is None or amt_el is None:
                continue
            ref = (ref_el.text or "").strip()
            s   = (amt_el.text or "").strip()
            if ref and s:
                try:
                    party_bills[ref] = abs(float(s))
                except ValueError:
                    pass
        if party_bills:
            result[party] = party_bills

    return result, ledger_count, billalloc_count


def _parse_bill_alloc_collection(
    root: Any,
    known_parties: set[str],
) -> tuple[dict[str, dict[str, float]], int]:
    """Parse a TYPE=Collection BillAllocation response.

    In TallyPrime, each Sales/DR voucher carries BillAllocations with:
      BILLTYPE = "New Ref"   → original bill creation
      AMOUNT                 → original bill amount  (≡ BILLOP)
      NAME                   → bill reference string
      LEDGERNAME             → party ledger name (parent LedgerEntry field)

    When queried as a top-level collection (TYPE=BillAllocation), the
    elements are exported as <BILLALLOCATION> tags (singular).

    Returns
    -------
    (result_dict, element_count)
      result_dict  : party → {bill_ref → opening_amount}
      element_count: number of <BILLALLOCATION> elements found (diagnostic)
    """
    result:        dict[str, dict[str, float]] = {}
    element_count: int = 0

    for ba in root.iter("BILLALLOCATION"):
        element_count += 1
        name_el = ba.find("NAME")
        amt_el  = ba.find("AMOUNT")
        led_el  = ba.find("LEDGERNAME")

        if name_el is None or amt_el is None:
            continue

        bill_ref = (name_el.text or "").strip()
        amt_s    = (amt_el.text or "").strip()
        ledger   = (led_el.text or "").strip() if led_el is not None else ""

        if not bill_ref or not amt_s:
            continue
        # Filter to known parties only (skip unrelated ledgers)
        if known_parties and ledger and ledger not in known_parties:
            continue

        try:
            amt = abs(float(amt_s))
        except ValueError:
            continue

        if amt > 0 and ledger:
            result.setdefault(ledger, {})[bill_ref] = amt

    return result, element_count


def _fetch_bill_openings(
    party_names: list[str],
    from_date_8: str,
    to_date_8: str,
    tally_url: str | None,
    ledger_group: str = "Sundry Debtors",
    timeout: float = 30.0,
) -> tuple[dict[str, dict[str, float]], str]:
    """Fetch bill opening amounts (original bill amounts before partial payments).

    Four strategies are tried in order — the first one that returns data wins.

    Strategy 1 — TYPE=Collection BillAllocation (primary, NEW)
    -----------------------------------------------------------
    Query BillAllocation objects directly from vouchers via a TDL collection.
    Sales/DR vouchers create BillAllocations with BILLTYPE="New Ref" whose
    AMOUNT = original bill amount (≡ BILLOP).  LEDGERNAME = the party ledger.
    This is F12-config-independent and works at the voucher level.

    Strategy 2 — TYPE=Collection Ledger + BILLALLOCATIONS (fallback)
    -----------------------------------------------------------------
    Export Ledger objects CHILDOF the debtors group and FETCH their
    BILLALLOCATIONS sub-objects.  Returns detailed diagnostics (ledger count,
    billalloc count) so we can distinguish "no ledgers found" vs "no sub-data".

    Strategy 3 — TYPE=Data "Ledger Outstandings" all-parties
    ---------------------------------------------------------
    Single request without SVLEDGER; relies on BILLPARTY headers.
    Historically returns empty on this installation.

    Strategy 4 — TYPE=Data "Ledger Outstandings" per-party SVLEDGER
    ----------------------------------------------------------------
    Individual requests per party using SVLEDGER.
    Historically also returns empty on this installation.

    Returns
    -------
    tuple of:
      - dict mapping  party_name → {bill_ref → opening_amount}
      - str: human-readable summary for diagnostics
    """
    # Opening balance fetch disabled — all strategies timed out or returned
    # empty on this installation.  Return immediately so no XML is sent to
    # TallyPrime (avoids the TDL error popup triggered by the BillAllocation
    # collection formula).
    return {}, "opening balance fetch disabled"

    _party_set = set(party_names)

    # ================================================================== #
    # Strategy 1: TYPE=Collection BillAllocation (company-wide)           #
    # ================================================================== #
    # Pull all New Ref BillAllocations in the period.  We use _early_date
    # as SVFROMDATE because BILLOP (opening amount) is attached to the
    # original voucher — it's the face value of the bill when first raised
    # — so we need to cover the full age of bills that may have been raised
    # in a prior period (e.g. FY 2024-25 invoices that carry forward into
    # FY 2025-26).  SVTODATE caps the *closing* date so only bills that
    # existed as-of to_date_8 are included (i.e. no future-dated bills).
    #
    # NOTE: CHILDOF / BELONGSTO are applied AFTER the collection is built
    # in-memory, so they do not reduce the scan time.  The BillAllocation
    # collection always scans every voucher in the company date-range.
    # For large companies this times out; we fall through to Strategy 2.
    _early_date = "20240401"   # 2 years back — covers any realistic bill age

    def _build_bill_alloc_xml() -> str:
        sv  = f"        <SVFROMDATE>{_early_date}</SVFROMDATE>\n"
        sv += f"        <SVTODATE>{to_date_8}</SVTODATE>\n"
        sv += "        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>\n"
        return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>NewRefBillAllocs</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{sv}      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="NewRefBillAllocs" ISMODIFY="No">
            <TYPE>BillAllocation</TYPE>
            <CHILDOF></CHILDOF>
            <BELONGSTO>No</BELONGSTO>
            <FETCH>NAME, AMOUNT, BILLTYPE, LEDGERNAME</FETCH>
            <FILTER>IsNewRefBill</FILTER>
          </COLLECTION>
          <SYSTEM:FORMULA NAME="IsNewRefBill">$BILLTYPE = "New Ref"</SYSTEM:FORMULA>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

    s1_debug = ""
    try:
        s1_raw   = _post_xml(_build_bill_alloc_xml(), tally_url, timeout=timeout)
        s1_root  = _parse_xml(s1_raw)
        s1_err   = (s1_root.findtext(".//LINEERROR") or "").strip()
        s1_snip  = s1_raw[:400].replace("\r\n", " ").replace("\n", " ")
        if s1_err:
            s1_debug = f"LINEERROR={s1_err}"
        else:
            result, elem_count = _parse_bill_alloc_collection(s1_root, _party_set)
            if result:
                summary = (
                    f"BillAllocation collection: {len(result)}/{len(party_names)} parties "
                    f"[Strategy 1]; {elem_count} BILLALLOCATION elements. "
                    f"Snippet: {s1_snip[:200]}"
                )
                logger.debug(summary)
                return result, summary
            s1_debug = (
                f"{elem_count} BILLALLOCATION elements found but 0 usable "
                f"(LEDGERNAME empty or no known-party match). Snippet: {s1_snip[:200]}"
            )
    except Exception as exc:
        s1_debug = str(exc)

    logger.debug("Strategy 1 (BillAllocation coll) failed: %s", s1_debug)

    # ================================================================== #
    # Strategy 2: TYPE=Collection Ledger + BILLALLOCATIONS                #
    # ================================================================== #
    def _build_ledger_collection_xml() -> str:
        sv  = f"        <SVTODATE>{to_date_8}</SVTODATE>\n"
        sv += "        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>\n"
        return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>LedBillOpeningsColl</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{sv}      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="LedBillOpeningsColl" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <CHILDOF>{_xe(ledger_group)}</CHILDOF>
            <BELONGSTO>Yes</BELONGSTO>
            <FETCH>NAME, BILLALLOCATIONS.NAME, BILLALLOCATIONS.AMOUNT, BILLALLOCATIONS.CLOSINGBALANCE, BILLALLOCATIONS.BILLDATE</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

    s2_debug = ""
    try:
        s2_raw   = _post_xml(_build_ledger_collection_xml(), tally_url, timeout=timeout)
        s2_root  = _parse_xml(s2_raw)
        s2_err   = (s2_root.findtext(".//LINEERROR") or "").strip()
        s2_snip  = s2_raw[:400].replace("\r\n", " ").replace("\n", " ")
        if s2_err:
            s2_debug = f"LINEERROR={s2_err}"
        else:
            result, led_cnt, ba_cnt = _parse_ledger_collection_bills(s2_root)
            if result:
                summary = (
                    f"Ledger+BILLALLOCATIONS collection: {len(result)}/{len(party_names)} parties "
                    f"[Strategy 2]; {led_cnt} LEDGERs, {ba_cnt} BILLALLOCATIONS. "
                    f"Snippet: {s2_snip[:200]}"
                )
                logger.debug(summary)
                return result, summary
            s2_debug = (
                f"{led_cnt} LEDGER elements, {ba_cnt} BILLALLOCATIONS, 0 usable parties. "
                f"Snippet: {s2_snip[:200]}"
            )
    except Exception as exc:
        s2_debug = str(exc)

    logger.debug("Strategy 2 (Ledger+BILLALLOCATIONS coll) failed: %s", s2_debug)

    # ================================================================== #
    # Strategy 3: "Ledger Outstandings" all-parties (no SVLEDGER)         #
    # ================================================================== #
    _LEDGER_RPT_IDS = [
        "Ledger Outstanding",
        "Ledger Outstandings",
        "Outstanding Ledger",
        "Ledger Bills",
    ]

    def _build_all_parties_xml(report_id: str) -> str:
        sv  = f"        <SVFROMDATE>{from_date_8}</SVFROMDATE>\n" if from_date_8 else ""
        sv += f"        <SVTODATE>{to_date_8}</SVTODATE>\n"
        sv += "        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>\n"
        return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>{_xe(report_id)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{sv}      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    def _build_single_party_xml(report_id: str, party: str) -> str:
        sv  = f"        <SVTODATE>{to_date_8}</SVTODATE>\n"
        sv += f"        <SVLEDGER>{_xe(party)}</SVLEDGER>\n"
        sv += "        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>\n"
        return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>{_xe(report_id)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{sv}      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    working_rpt_id: str      = ""
    all_parties_raw: str     = ""
    all_parties_root: Any    = None
    id_errors: list[str]     = []

    for rid in _LEDGER_RPT_IDS:
        try:
            raw  = _post_xml(_build_all_parties_xml(rid), tally_url, timeout=timeout)
            root = _parse_xml(raw)
            err  = (root.findtext(".//LINEERROR") or "").strip()
            if err:
                id_errors.append(f"{rid}: {err}")
                continue
            working_rpt_id   = rid
            all_parties_raw  = raw
            all_parties_root = root
            break
        except Exception as exc:
            id_errors.append(f"{rid}: {exc}")

    probe_snip = (all_parties_raw[:400].replace("\r\n", " ").replace("\n", " ")
                  if all_parties_raw else f"id_errors={id_errors}")

    if working_rpt_id and all_parties_root is not None:
        result = _parse_ledger_outstanding_bills(all_parties_root)
        if result:
            summary = (
                f"Ledger Outstanding ('{working_rpt_id}'): "
                f"{len(result)}/{len(party_names)} parties [Strategy 3 all-parties]."
            )
            logger.debug(summary)
            return result, summary

    if not working_rpt_id:
        probe_snip = f"no valid report; id_errors={id_errors}"

    logger.debug(
        "Strategy 3 (all-parties '%s') empty. Probe: %s — trying per-party SVLEDGER.",
        working_rpt_id, probe_snip[:200],
    )

    # ================================================================== #
    # Strategy 4: per-party SVLEDGER fallback                             #
    # ================================================================== #
    if not working_rpt_id:
        summary = (
            f"All opening-amount strategies failed. "
            f"S1(BillAlloc): {s1_debug[:120]}. "
            f"S2(Led+BA): {s2_debug[:120]}. "
            f"S3/S4(LedOutstanding): {probe_snip[:120]}"
        )
        return {}, summary

    result:    dict[str, dict[str, float]] = {}
    errors:    list[str] = []
    successes: int       = 0

    for party in party_names:
        try:
            raw  = _post_xml(_build_single_party_xml(working_rpt_id, party), tally_url, timeout=timeout)
            root = _parse_xml(raw)

            tally_err = (root.findtext(".//LINEERROR") or "").strip()
            if tally_err:
                errors.append(f"{party}: LINEERROR={tally_err}")
                continue

            party_result = _parse_ledger_outstanding_bills(root, single_party=party)
            if party_result:
                result.update(party_result)
                successes += 1

        except Exception as exc:
            errors.append(f"{party}: {exc}")

    summary = (
        f"Ledger Outstanding ('{working_rpt_id}'): "
        f"{successes}/{len(party_names)} parties [Strategy 4 per-party SVLEDGER]. "
        f"S1: {s1_debug[:80]}. S2: {s2_debug[:80]}. S3: {probe_snip[:80]}"
    )
    if errors:
        summary += f"; errors: {errors[:3]}"
    logger.debug(summary)
    return result, summary


def fetch_outstanding_receivables(
    from_date: str = "",
    as_of_date: str = "",
    party_name: str = "",
    ledger_group: str = "Sundry Debtors",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Fetch Ledger-wise Bill-wise Outstanding Receivables from TallyPrime.

    Uses TYPE=Collection with a custom TDL query (reliable across all
    TallyPrime versions via the Gateway XML API).  The TYPE=Data report
    approach ("Outstanding Receivables") is not universally available and
    returns a LINEERROR on many installations.

    Approach
    --------
    Queries all Ledgers that BELONGSTO ``ledger_group`` (default "Sundry
    Debtors") and have a Dr closing balance.  For each ledger the
    BillAllocations sub-objects are fetched, giving one row per pending
    bill reference.  Overdue days are computed from the bill DueDate and
    the requested as-of date (no dependency on a built-in report ID).

    Response fields
    ---------------
    Each entry in ``bills``:
      party         : ledger / party name
      bill_ref      : bill / invoice reference number
      bill_date     : date the bill was raised  (YYYYMMDD)
      due_date      : date payment is/was due   (YYYYMMDD)
      outstanding   : pending amount (positive, INR)
      days_overdue  : days past due as of as_of_date (0 = not yet due)

    Aging buckets
    -------------
      current_not_due  : days_overdue = 0
      overdue_1_30     : 1 – 30
      overdue_31_60    : 31 – 60
      overdue_61_90    : 61 – 90
      overdue_above_90 : 91+

    Args:
        from_date:     Start of the reporting period (optional).
                       Formats: DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, YYYYMMDD.
        as_of_date:    End / "as-of" date.  Defaults to today.
        party_name:    Optional substring filter on party name (case-insensitive).
        ledger_group:  Tally group containing debtors (default "Sundry Debtors").
                       Change if your debtors sit under a different group name.
        tally_url:     Override the default TallyPrime Gateway URL for this call.
    """
    from datetime import date as _date_cls

    to_date_8 = _parse_date(as_of_date) if as_of_date else _date_cls.today().strftime("%Y%m%d")

    # Candidates for the TYPE=Data report ID used by TallyPrime's Bills Receivable
    # report.  Different installations / versions register it under different names.
    # We try each one in order and use the first response that does NOT contain a
    # LINEERROR (i.e. Tally found the report).
    _REPORT_ID_CANDIDATES = [
        # User's saved F12 view with "Show Opening Amount" — try all likely name variants
        "Bills Receivable - My View",       # dash with spaces
        "Bills Receivable-My View",         # dash without spaces
        "My View",                          # just the view name
        "Bills Receivable : My View",       # colon separator
        "Bills ReceivableBills Receivable - My View",  # TallyPrime breadcrumb-style name
        # Standard report fallbacks (BILLOP absent without F12 view)
        "Bills Receivable",                 # confirmed working ID (user-verified)
        "Bills Outstanding",                # Tally UI display name
        "Receivables",                      # alternate internal name in some builds
        "Outstanding Receivables",          # older Tally/ERP9 name
        "Outstandings",                     # catch-all fallback
    ]

    def _build_xml(report_id: str) -> str:
        sv = f"        <SVTODATE>{to_date_8}</SVTODATE>\n"
        if from_date:
            sv = f"        <SVFROMDATE>{_parse_date(from_date)}</SVFROMDATE>\n" + sv
        # SVLEDWISE=Yes → Ledger-wise Bill-wise view (groups bills under each party)
        sv += "        <SVLEDWISE>Yes</SVLEDWISE>\n"
        sv += "        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>\n"
        # F12 "Show Opening Amount" TDL variable is named "BILLCFG ShowOpAmt" (with space).
        # Cannot be sent as an XML tag name (spaces are illegal in tag names).
        # Override it via a TDL <VARIABLE> block where the name goes in the NAME *attribute*
        # (attribute values DO support spaces) — this sets the variable for the request session.
        return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>{_xe(report_id)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{sv}      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <VARIABLE NAME="BILLCFG ShowOpAmt" ISMODIFY="Yes">
            <DEFAULT>Yes</DEFAULT>
          </VARIABLE>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

    raw = ""
    root = None
    used_id = ""
    _id_errors: dict[str, str] = {}   # rid → LINEERROR text for diagnostics
    for rid in _REPORT_ID_CANDIDATES:
        raw  = _post_xml(_build_xml(rid), tally_url, timeout=60.0)
        root = _parse_xml(raw)
        err  = (root.findtext(".//LINEERROR") or "").strip()
        if not err:
            used_id = rid
            break   # found a working report ID
        _id_errors[rid] = err
    else:
        # All candidates failed — return the last error with diagnostic info
        return {
            "error": f"Tally could not find the Bills Receivable report. "
                     f"Tried IDs: {_REPORT_ID_CANDIDATES}. "
                     f"Last Tally response: {raw[:500] if raw else '(empty)'}",
            "as_of_date": to_date_8, "from_date": _parse_date(from_date) if from_date else "",
            "total_outstanding": 0, "party_count": 0, "party_summary": [],
            "aging_summary": {
                "current_not_due": 0.0, "overdue_1_30": 0.0, "overdue_31_60": 0.0,
                "overdue_61_90": 0.0, "overdue_above_90": 0.0,
            },
            "bills": [], "bill_count": 0, "tally_url": _resolve_url(tally_url),
            "_raw_xml": raw[:3000] if raw else "",
        }

    # ── State-machine parser for the flat BILLFIXED/BILLCL/… structure ────────
    # TallyPrime's ledger-wise Bills Receivable report exports a flat <ENVELOPE>
    # (or <ENVELOPE><BODY><DATA>) whose elements repeat in groups:
    #
    #   <BILLFIXED>            ← determines row type via child content
    #     <BILLPARTY>…</BILLPARTY>           → party header row
    #   </BILLFIXED>
    #
    #   <BILLFIXED>            OR
    #     <BILLDATE>…</BILLDATE>             → bill detail row
    #     <BILLREF>…</BILLREF>
    #     [<BILLPARTY>…</BILLPARTY>]         (present in bill-wise view too)
    #   </BILLFIXED>
    #   <BILLCL>amount</BILLCL>              ← pending amount (negative)
    #   <BILLDUE>date</BILLDUE>
    #   <BILLOVERDUE>days</BILLOVERDUE>
    #
    #   <BILLFIXED/>            ← ledger-total row (empty children)
    #   <LEDBILLCL>amount</LEDBILLCL>        ← party sub-total (negative)
    #
    # The iter() walk handles both flat-under-ENVELOPE and wrapped-under-DATA.

    _REPORT_TAGS = {"BILLFIXED", "BILLOP", "BILLCL", "BILLDUE", "BILLOVERDUE",
                    "LEDBILLOP", "LEDBILLCL"}
    direct = [e for e in root if e.tag.upper() in _REPORT_TAGS]
    elements = list(root) if direct else list(root.iter())

    bills:         list[dict[str, Any]] = []
    party_totals:  dict[str, float]     = {}   # pending (BILLCL) per party
    party_opening: dict[str, float]     = {}   # opening (BILLOP) per party
    aging: dict[str, float] = {
        "current_not_due":  0.0,
        "overdue_1_30":     0.0,
        "overdue_31_60":    0.0,
        "overdue_61_90":    0.0,
        "overdue_above_90": 0.0,
    }
    current_party = ""
    pending: dict[str, Any] | None = None

    for elem in elements:
        tag = elem.tag.upper()

        if tag == "BILLFIXED":
            bill_party = (elem.findtext("BILLPARTY") or "").strip()
            bill_date  = (elem.findtext("BILLDATE")  or "").strip()
            bill_ref   = (elem.findtext("BILLREF")   or "").strip()

            if bill_date and bill_ref:
                # Bill detail row — present in both ledger-wise and bill-wise layouts
                if bill_party:
                    current_party = bill_party   # bill-wise: party on same row
                pending = {
                    "party": current_party, "bill_ref": bill_ref,
                    "bill_date": bill_date, "due_date": "",
                    "days_overdue": 0, "opening": 0.0, "outstanding": 0.0,
                }
            elif bill_party:
                current_party = bill_party       # ledger-wise: party header row
                pending = None
            else:
                pending = None                   # ledger-total row

        elif tag == "BILLOP":
            # Opening / original bill amount (before partial payments)
            s = (elem.text or "").strip()
            if pending is not None and s:
                try:
                    pending["opening"] = abs(float(s))
                except ValueError:
                    pass

        elif tag == "BILLCL":
            # Closing / pending amount (remaining after partial payments)
            s = (elem.text or "").strip()
            if pending is not None and s:
                try:
                    pending["outstanding"] = abs(float(s))
                except ValueError:
                    pass

        elif tag == "LEDBILLOP":
            # Party-level opening total
            s = (elem.text or "").strip()
            if current_party and s:
                try:
                    party_opening[current_party] = abs(float(s))
                except ValueError:
                    pass

        elif tag == "LEDBILLCL":
            # Party-level pending total
            s = (elem.text or "").strip()
            if current_party and s:
                try:
                    party_totals[current_party] = abs(float(s))
                except ValueError:
                    pass

        elif tag == "BILLDUE":
            if pending is not None:
                pending["due_date"] = (elem.text or "").strip()

        elif tag == "BILLOVERDUE":
            s = (elem.text or "").strip()
            if pending is not None:
                try:
                    pending["days_overdue"] = int(s) if s else 0
                except ValueError:
                    pending["days_overdue"] = 0

                amt     = pending["outstanding"]
                opening = pending["opening"]
                # Track whether BILLOP was received from Tally or had to be defaulted.
                # billop_from_tally = True means Tally populated BILLOP (static var worked).
                billop_from_tally = opening > 0.0
                # If BILLOP was absent/empty (static var not yet effective), fall back
                if not billop_from_tally and amt > 0:
                    opening = amt
                if amt > 0 and pending["bill_ref"]:
                    bills.append({
                        "party":             pending["party"],
                        "bill_ref":          pending["bill_ref"],
                        "bill_date":         pending["bill_date"],
                        "due_date":          pending["due_date"],
                        "opening":           round(opening, 2),
                        "outstanding":       round(amt, 2),
                        "days_overdue":      pending["days_overdue"],
                        "_opening_from_tally": billop_from_tally,
                    })
                    od = pending["days_overdue"]
                    if od <= 0:    aging["current_not_due"]  += amt
                    elif od <= 30: aging["overdue_1_30"]     += amt
                    elif od <= 60: aging["overdue_31_60"]    += amt
                    elif od <= 90: aging["overdue_61_90"]    += amt
                    else:          aging["overdue_above_90"] += amt
                pending = None

    # ── Optional party_name filter ─────────────────────────────────────────────
    if party_name:
        pn_lower      = party_name.lower()
        bills         = [b for b in bills if pn_lower in b["party"].lower()]
        party_totals  = {k: v for k, v in party_totals.items() if pn_lower in k.lower()}
        party_opening = {k: v for k, v in party_opening.items() if pn_lower in k.lower()}
        aging         = {k: 0.0 for k in aging}
        for b in bills:
            od, amt = b["days_overdue"], b["outstanding"]
            if od <= 0:    aging["current_not_due"]  += amt
            elif od <= 30: aging["overdue_1_30"]     += amt
            elif od <= 60: aging["overdue_31_60"]    += amt
            elif od <= 90: aging["overdue_61_90"]    += amt
            else:          aging["overdue_above_90"] += amt

    # Determine whether BILLOP was actually populated by Tally via the display report.
    billop_available = any(b["_opening_from_tally"] for b in bills)

    # ── Per-party Object fallback for Opening Amount ───────────────────────────
    # If the TYPE=Data display report did not populate BILLOP, fetch opening
    # amounts via individual TYPE=Object/Ledger requests — one per party.
    # The inline-TDL Collection approach failed (BELONGSTO+FETCH returns 0 records);
    # direct Object export reliably includes BillAllocation.OpeningBalance regardless
    # of the F12 "Show Opening Amount" UI setting.
    _coll_debug_snippet = ""   # diagnostic summary from fallback
    if not billop_available and bills:
        _from8 = _parse_date(from_date) if from_date else ""
        _party_names = list(dict.fromkeys(b["party"] for b in bills))  # unique, ordered
        opening_data, _coll_debug_snippet = _fetch_bill_openings(
            _party_names, _from8, to_date_8, tally_url,
            ledger_group=ledger_group, timeout=30.0
        )
        if opening_data:
            for b in bills:
                party_ops = opening_data.get(b["party"], {})
                new_op = party_ops.get(b["bill_ref"], 0.0)
                if new_op > 0.0:
                    b["opening"] = round(new_op, 2)
                    b["_opening_from_tally"] = True
                    # Also update party_opening from Tally source
                    party_opening[b["party"]] = round(
                        party_opening.get(b["party"], 0.0) + new_op, 2
                    )
            billop_available = any(b["_opening_from_tally"] for b in bills)

    # In bill-wise layout LEDBILLCL/LEDBILLOP elements are absent, so derive
    # party totals from the parsed bills in that case.
    if not party_totals and bills:
        for b in bills:
            party_totals[b["party"]]  = round(party_totals.get(b["party"], 0.0)  + b["outstanding"], 2)
    # If party_opening was not set from Tally (LEDBILLOP absent AND fallback found nothing),
    # derive from bills (opening = outstanding as last resort).
    if not party_opening and bills:
        for b in bills:
            party_opening[b["party"]] = round(party_opening.get(b["party"], 0.0) + b["opening"], 2)

    total_outstanding = round(sum(party_totals.values()), 2)
    total_opening     = round(sum(party_opening.values()), 2)

    # ── Build ledger-wise grouped output (mirrors Excel / Tally UI layout) ───
    # Group bills under each party in the ORDER they appear in the Tally report,
    # so the caller sees a structure identical to the Excel Ledger-wise Bills view.
    from collections import OrderedDict
    party_order: list[str] = list(OrderedDict.fromkeys(b["party"] for b in bills))

    bills_by_party: list[dict[str, Any]] = []
    for party in party_order:
        party_bills = [
            {
                "bill_ref":    b["bill_ref"],
                "bill_date":   b["bill_date"],
                "due_date":    b["due_date"],
                "opening":     b["opening"],
                "outstanding": b["outstanding"],
                "days_overdue":b["days_overdue"],
            }
            for b in bills if b["party"] == party
        ]
        p_outstanding = round(party_totals.get(party, sum(x["outstanding"] for x in party_bills)), 2)
        p_opening     = round(party_opening.get(party, sum(x["opening"] for x in party_bills)), 2)
        bills_by_party.append({
            "party":       party,
            "opening":     p_opening,
            "outstanding": p_outstanding,
            "bill_count":  len(party_bills),
            "bills":       party_bills,
        })

    # Summary sorted highest outstanding first (for quick overview)
    party_summary = [
        {
            "party":       p["party"],
            "opening":     p["opening"],
            "outstanding": p["outstanding"],
            "bill_count":  p["bill_count"],
        }
        for p in sorted(bills_by_party, key=lambda x: -x["outstanding"])
    ]

    result: dict[str, Any] = {
        "as_of_date":        to_date_8,
        "from_date":         _parse_date(from_date) if from_date else "",
        "total_opening":     total_opening,
        "total_outstanding": total_outstanding,
        "party_count":       len(bills_by_party),
        "bill_count":        len(bills),
        "party_summary":     party_summary,
        "aging_summary":     {k: round(v, 2) for k, v in aging.items()},
        "bills_by_party":    bills_by_party,
        "tally_url":         _resolve_url(tally_url),
    }

    return result
