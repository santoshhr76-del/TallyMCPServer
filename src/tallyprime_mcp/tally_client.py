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


def _format_company_currency(raw: str | None) -> str:
    """Map TallyPrime's COMPANY.CURRENCYNAME (often just the symbol like '₹'
    or an XML entity '&#8377;') into a readable label like 'INR (₹)'.

    Non-Indian symbols pass through untouched so multi-currency setups
    still display correctly.
    """
    if not raw:
        return "INR (₹)"
    s = str(raw).strip()
    # Known Indian rupee representations (raw char, hex/dec entities, common
    # mojibake variants). Map them all to the canonical "INR (₹)".
    if s in {"₹", "&#8377;", "&#x20B9;", "&#X20B9;", "Rs", "Rs."}:
        return "INR (₹)"
    # If TallyPrime ever returns just the text "INR" (some older versions),
    # add the symbol for visual clarity.
    if s.upper() == "INR":
        return "INR (₹)"
    # Anything else (USD, EUR, …) is shown verbatim.
    return s


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


def _collection_objects(root: ET.Element, tag: str) -> list[ET.Element]:
    """Return the real <TAG> data objects from a Tally Collection response.

    Every Collection response carries a <CMPINFO> header block whose COUNTER
    fields reuse object tag names — e.g. <VOUCHER>2</VOUCHER>, <LEDGER>0</LEDGER>,
    <STOCKITEM>0</STOCKITEM>. A naive root.findall(".//TAG") therefore returns
    those empty counter elements FIRST, and code that summarises match[0] ends
    up reading an empty element. Prefer the BODY/DATA/COLLECTION path; if that
    yields nothing, fall back to elements that have children or attributes
    (the CMPINFO counters have neither).
    """
    objs = root.findall(f".//DATA/COLLECTION/{tag}")
    if objs:
        return objs
    return [el for el in root.findall(f".//{tag}") if len(el) or el.attrib]


# ─────────────────────────────────────────────
# COMPANY / CONNECTION
# ─────────────────────────────────────────────

def _fetch_company_gstin(tally_url: str | None = None) -> dict[str, str]:
    """Fetch the company GSTIN from the GST Tax Unit / GST Registration master.

    TallyPrime 2.x+ (the new GST experience) stores the company's GSTIN inside a
    Tax Unit / GST Registration master created in F11 — NOT in the Company
    master's <GSTREGISTRATIONNUMBER> field (which comes back blank). We probe the
    likely collection object types and return the first GSTIN found.

    Only GST tax units are considered — Excise / VAT / Service-tax units are
    excluded via a server-side `$UsedFor = "GST"` filter, with a TAXTYPE guard
    as backup. The non-GST "Default Tax Unit" is ignored (it carries no GSTIN).

    Returns {} when nothing is found (e.g. older TallyPrime, where the Company
    field is authoritative and this lookup isn't needed).
    """
    for coll_type in ("TaxUnit", "GSTRegistration", "CMPGSTRegistration"):
        # Server-side filter: restrict the TaxUnit collection to GST units so
        # Excise/VAT/other tax-type units never come back. Only applied to the
        # TaxUnit type (the other fallback types don't expose $UsedFor).
        if coll_type == "TaxUnit":
            filter_xml = "<FILTER>MCPGSTOnly</FILTER>"
            system_xml = '<SYSTEM TYPE="Formulae" NAME="MCPGSTOnly">$UsedFor = "GST"</SYSTEM>'
        else:
            filter_xml = ""
            system_xml = ""
        xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPCmpGSTReg</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPCmpGSTReg" ISMODIFY="No">
            <TYPE>{coll_type}</TYPE>
            <FETCH>Name,GSTRegNumber,TaxRegistration,UsedFor,TaxType,GSTIN,
                   GSTRegistrationNumber,GSTRegistrationType,
                   StateName,State,ApplicableFrom</FETCH>
            {filter_xml}
          </COLLECTION>
          {system_xml}
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
        try:
            raw = _post_xml(xml, tally_url)
        except Exception:
            continue

        # Parse element-by-element and return the FIRST GST unit that carries a
        # GSTIN, so the name/state we report belong to that same unit (a flat
        # regex over the whole response would mis-attribute them). TallyPrime
        # stores the GSTIN in <GSTREGNUMBER> and in the <TAXUNIT> element's
        # TAXREGISTRATION attribute; older builds use <GSTIN>.
        try:
            root = _parse_xml(raw)
        except Exception:
            root = None

        if root is not None:
            for tu in _collection_objects(root, "TAXUNIT"):
                # Backup guard (in case the server filter is unsupported): never
                # treat a non-GST unit's registration number as the GSTIN.
                tax_type = (tu.get("TAXTYPE") or "").strip().upper()
                used_for = _find_text(tu, "USEDFOR").strip().upper()
                if tax_type and tax_type != "GST":
                    continue
                if used_for and used_for != "GST":
                    continue
                gstin = (
                    _find_text(tu, "GSTREGNUMBER")
                    or (tu.get("TAXREGISTRATION") or "").strip()
                    or _find_text(tu, "GSTIN")
                    or _find_text(tu, "GSTREGISTRATIONNUMBER")
                )
                if gstin:
                    return {
                        "gstin":                 gstin,
                        "gst_registration_type": _find_text(tu, "GSTREGISTRATIONTYPE"),
                        "tax_unit":              tu.get("NAME") or _find_text(tu, "NAME"),
                        "state":                 _find_text(tu, "STATENAME"),
                    }

        # Fallback for collection types that don't wrap results in <TAXUNIT>
        # (single-registration companies, alternate object types).
        gstin = _rx(raw, "GSTREGNUMBER") or _rx(raw, "GSTIN") or _rx(raw, "GSTREGISTRATIONNUMBER")
        if gstin:
            return {
                "gstin":                 gstin,
                "gst_registration_type": _rx(raw, "GSTREGISTRATIONTYPE"),
                "tax_unit":              _rx(raw, "TAXUNIT@NAME") or _rx(raw, "NAME"),
                "state":                 _rx(raw, "STATENAME"),
            }
    return {}


def get_active_company(tally_url: str | None = None) -> dict[str, Any]:
    """
    Return the currently open company in TallyPrime.

    Uses a Collection request (the only type guaranteed to work on all
    TallyPrime versions via the Gateway XML API).

    The company GSTIN is taken DIRECTLY from the GST Tax Unit master (F11) via
    _fetch_company_gstin(); the Company master's own GST field is not consulted.
    The non-GST "Default Tax Unit" is ignored (it carries no GSTIN).
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
                   StateName,CountryName,PhoneNumber,MobileNo,
                   Email,Website,Address,BooksFrom,IsSimpleGSTEnabled</FETCH>
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

    # GSTIN is read DIRECTLY from the GST Tax Unit master (F11) — the Company
    # master's GSTREGISTRATIONNUMBER field is intentionally not used (blank in
    # TallyPrime 2.x+). The non-GST "Default Tax Unit" is ignored.
    reg = _fetch_company_gstin(tally_url)
    gstin                 = reg.get("gstin", "")
    gst_registration_type = reg.get("gst_registration_type", "")
    tax_unit              = reg.get("tax_unit", "")

    return {
        # NAME appears both as attribute <COMPANY NAME="..."> and child <NAME>...</NAME>
        "name":          _rx(raw, "COMPANY@NAME") or _rx(raw, "NAME"),
        "starting_from": _rx(raw, "STARTINGFROM"),
        "ending_at":     _rx(raw, "ENDINGAT"),
        # TallyPrime's COMPANY.CURRENCYNAME field actually returns the *name
        # attribute* of the currency master — for Indian Tally this is "₹"
        # (the symbol), not the text "INR". The text "INR" lives in the
        # Currency master's <MAILINGNAME>. We don't want to round-trip a
        # second XML fetch just for a label, so we translate the common
        # known cases here. Other (non-Indian) currencies pass through as-is.
        "currency":      _format_company_currency(_rx(raw, "CURRENCYNAME")),
        "books_from":    _rx(raw, "BOOKSFROM"),
        "gstin":         gstin,
        "gst_registration_type": gst_registration_type,
        "tax_unit":      tax_unit,
        "state":         _rx(raw, "STATENAME"),
        "country":       _rx(raw, "COUNTRYNAME"),
        "phone":         _rx(raw, "PHONENUMBER"),
        "mobile":        _rx(raw, "MOBILENO"),
        "email":         _rx(raw, "EMAIL"),
        "website":       _rx(raw, "WEBSITE"),
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

def fetch_ledgers_of_group(
    group_name: str = "Sundry Debtors",
    from_date: str = "",
    to_date: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """List every ledger under a Tally group with its opening/closing balance.

    Uses a Collection request with <CHILDOF> + <BELONGSTO>Yes</BELONGSTO>, which
    returns all ledgers belonging to the group *including nested sub-groups*
    (e.g. parties filed under a sub-group of 'Sundry Debtors'), rather than only
    those whose immediate Parent equals the group name.

    Optional from_date / to_date set the reporting period via SVFROMDATE /
    SVTODATE, so ClosingBalance is computed as of to_date and OpeningBalance as
    of from_date — the same figures Tally shows on the group screen for that
    period. Dates accept DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD or YYYYMMDD.

    Sign convention follows Tally: debit balances are negative, credit positive
    (so Sundry Debtors parties are normally negative, Sundry Creditors positive).
    """
    date_vars = ""
    norm_from = _parse_date(from_date) if from_date else ""
    norm_to = _parse_date(to_date) if to_date else ""
    if norm_from:
        date_vars += f"        <SVFROMDATE>{norm_from}</SVFROMDATE>\n"
    if norm_to:
        date_vars += f"        <SVTODATE>{norm_to}</SVTODATE>\n"

    safe_group = group_name.replace("&", "&amp;").replace('"', "&quot;")

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPLedgersOfGroup</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{date_vars}        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPLedgersOfGroup" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <CHILDOF>{safe_group}</CHILDOF>
            <BELONGSTO>Yes</BELONGSTO>
            <FETCH>Name,Parent,ClosingBalance,OpeningBalance</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

    root = _parse_xml(_post_xml(xml, tally_url, timeout=120.0))

    ledgers = [
        {
            "name": led.get("NAME") or _find_text(led, "NAME"),
            "parent": _find_text(led, "PARENT"),
            "opening_balance": _find_text(led, "OPENINGBALANCE"),
            "closing_balance": _find_text(led, "CLOSINGBALANCE"),
        }
        for led in _collection_objects(root, "LEDGER")
    ]

    return {
        "group": group_name,
        "from_date": norm_from,
        "to_date": norm_to,
        "ledger_count": len(ledgers),
        "ledgers": ledgers,
    }


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
        for ledger in _collection_objects(root, "LEDGER")
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
                   IncomeTaxNumber,LedgerContact,LedgerMobile,Email,
                   CreditLimit,BillCreditPeriod,IsBillWiseOn,
                   LedMailingDetails,LedGSTRegDetails,ContactDetails</FETCH>
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
    _ledgers = _collection_objects(root, "LEDGER")
    ledger = _ledgers[0] if _ledgers else None
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

    # Opening/closing balance: the Dr/Cr label is derived purely from the sign
    # of the amount TallyPrime returns, which is consistent for every ledger
    # regardless of its parent group (Sundry Debtors, Creditors, etc.):
    #   negative value -> Dr,  positive value -> Cr.
    def _fmt_balance(val: str) -> str:
        """Convert Tally's signed numeric balance to a Dr/Cr string (sign-based, group-independent)."""
        v = val.strip()
        if not v or v == "0" or v == "0.00":
            return "0.00"
        try:
            n = float(v)
            if n < 0:
                return f"{abs(n):.2f} Dr"
            return f"{n:.2f} Cr"
        except ValueError:
            return v  # already has Dr/Cr or is non-numeric — return as-is

    opening_balance = _fmt_balance(
        _find_text(ledger, "OPENINGBALANCE") or _rx(raw, "OPENINGBALANCE")
    )
    closing_balance = _fmt_balance(
        _find_text(ledger, "CLOSINGBALANCE") or _rx(raw, "CLOSINGBALANCE")
    )

    # ── Robust field extraction ──────────────────────────────────────────────
    # TallyPrime stores some master fields nested inside sub-lists (e.g. the
    # GST identification number lives in <GSTIN> inside LEDGSTREGDETAILS.LIST,
    # NOT in the voucher-level <PARTYGSTIN> tag). So for each field we try, in
    # order: a direct child, any descendant (.//TAG), then a regex over the raw
    # XML. The first non-empty match wins. Tag names confirmed from the Tally
    # ledger-master XML export: GSTIN, LEDGERMOBILE, EMAIL, INCOMETAXNUMBER.
    def _field(*tags: str) -> str:
        for tag in tags:
            val = (
                _find_text(ledger, tag)
                or _find_text(ledger, f".//{tag}")
                or _rx(raw, tag)
            )
            if val:
                return val
        return ""

    # Contact-person name: top-level <LEDGERCONTACT> mirrors the value, but the
    # authoritative source is <NAME> inside <CONTACTDETAILS.LIST>. We must scope
    # the NAME lookup to that sub-list — a bare _field("NAME") would return the
    # ledger's own name instead.
    contact = ledger.find("CONTACTDETAILS.LIST")
    contact_person = (
        _find_text(ledger, "LEDGERCONTACT")
        or (_find_text(contact, "NAME") if contact is not None else "")
        or _rx(raw, "LEDGERCONTACT")
    )

    return {
        "name":                 ledger.get("NAME") or _find_text(ledger, "NAME") or _rx(raw, "NAME"),
        "parent":               _find_text(ledger, "PARENT")                or _rx(raw, "PARENT"),
        "opening_balance":      opening_balance,
        "closing_balance":      closing_balance,
        "currency":             _find_text(ledger, "CURRENCYNAME")          or _rx(raw, "CURRENCYNAME"),
        "gst_registration_type":_field("GSTREGISTRATIONTYPE"),
        # Ledger-master GSTIN is <GSTIN>; fall back to the voucher-level
        # <PARTYGSTIN> tag for older TallyPrime versions / cached masters.
        "gstin":                _field("GSTIN", "PARTYGSTIN"),
        "pan":                  _field("INCOMETAXNUMBER", "LEDGERPAN"),
        "phone":                _field("LEDGERMOBILE", "LEDGERPHONE", "PHONENUMBER"),
        "email":                _field("EMAIL"),
        "contact_person":       contact_person,
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
        for g in _collection_objects(root, "GROUP")
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
        for v in _collection_objects(root, "VOUCHER")
    ]


def fetch_voucher_by_number(voucher_number: str, tally_url: str | None = None) -> dict[str, Any]:
    """Fetch the COMPLETE voucher object for a given voucher number.

    Returns the full raw voucher XML plus a parsed summary covering:
      • voucher header (number, date, type, reference, narration)
      • party details (name, GSTIN, state, place of supply, reg. type)
      • ledger entries (ledger name, amount, Dr/Cr)
      • inventory items (item, quantity, rate, amount)
      • rolled-up totals (debit, credit, inventory value)

    Note: voucher numbers are not guaranteed unique across voucher types or
    financial years, so `matched_count` reports how many vouchers matched;
    the structured summary describes the FIRST match while `raw_xml` contains
    every match returned by TallyPrime.

    The request sets SVFROMDATE/SVTODATE to a wide range (2000–2099) because a
    Voucher collection WITHOUT an explicit date range only covers Tally's
    current period — which silently returns no results for older vouchers.
    Matching is exact on the stored voucher number string (e.g. 'INV-001').
    """
    safe_num = str(voucher_number).replace("&", "&amp;").replace('"', "&quot;")
    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPVoucherByNumber</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVFROMDATE>20000401</SVFROMDATE>
        <SVTODATE>20991231</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPVoucherByNumber" ISMODIFY="No">
            <TYPE>Voucher</TYPE>
            <FETCH>Date,VoucherNumber,VoucherTypeName,Reference,ReferenceDate,
                   Narration,PartyLedgerName,PartyName,PartyGSTIN,
                   GSTRegistrationType,PlaceOfSupply,StateName,CountryOfResidence,
                   Amount,LedgerEntries,AllLedgerEntries,
                   InventoryEntries,AllInventoryEntries</FETCH>
            <FILTER>MCPVchNumFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="MCPVchNumFilter">$VoucherNumber = "{safe_num}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)
    vouchers = _collection_objects(root, "VOUCHER")
    if not vouchers:
        return {
            "error": f"No voucher found with number '{voucher_number}'",
            "tally_url": _resolve_url(tally_url),
            "raw_xml": raw,
        }

    voucher = vouchers[0]   # summarise the first match

    def _amt(val: str) -> float:
        try:
            return float(val) if val not in ("", None) else 0.0
        except ValueError:
            return 0.0

    # ── Party / header ────────────────────────────────────────────────────────
    party = {
        "name":              _find_text(voucher, "PARTYLEDGERNAME") or _find_text(voucher, "PARTYNAME"),
        "gstin":             _find_text(voucher, "PARTYGSTIN"),
        "state":             _find_text(voucher, "STATENAME"),
        "place_of_supply":   _find_text(voucher, "PLACEOFSUPPLY"),
        "registration_type": _find_text(voucher, "GSTREGISTRATIONTYPE"),
    }

    # ── Ledger entries (prefer ALLLEDGERENTRIES, fall back to LEDGERENTRIES) ───
    led_lists = voucher.findall("ALLLEDGERENTRIES.LIST") or voucher.findall("LEDGERENTRIES.LIST")
    ledger_entries: list[dict[str, Any]] = []
    tot_dr = tot_cr = 0.0
    for le in led_lists:
        lname = _find_text(le, "LEDGERNAME")
        if not lname:
            continue
        amt = abs(_amt(_find_text(le, "AMOUNT")))
        # ISDEEMEDPOSITIVE=Yes => debit side; No => credit side.
        is_dr = _find_text(le, "ISDEEMEDPOSITIVE").strip().lower() == "yes"
        ledger_entries.append({"ledger": lname, "amount": round(amt, 2), "dr_cr": "Dr" if is_dr else "Cr"})
        if is_dr:
            tot_dr += amt
        else:
            tot_cr += amt

    # ── Inventory items (prefer ALLINVENTORYENTRIES, fall back to INVENTORYENTRIES) ─
    inv_lists = voucher.findall("ALLINVENTORYENTRIES.LIST") or voucher.findall("INVENTORYENTRIES.LIST")
    inventory_items: list[dict[str, Any]] = []
    tot_inv = 0.0
    for ie in inv_lists:
        iname = _find_text(ie, "STOCKITEMNAME")
        if not iname:
            continue
        amt = abs(_amt(_find_text(ie, "AMOUNT")))
        # The sales/purchase ledger for this line lives in ACCOUNTINGALLOCATIONS.LIST.
        line_ledger = _find_text(ie, "ACCOUNTINGALLOCATIONS.LIST/LEDGERNAME")
        inventory_items.append({
            "item":       iname,
            "quantity":   (_find_text(ie, "BILLEDQTY") or _find_text(ie, "ACTUALQTY")).strip(),
            "rate":       _find_text(ie, "RATE").strip(),
            "amount":     round(amt, 2),
            "ledger":     line_ledger,
            "hsn":        _find_text(ie, "GSTHSNNAME") or _find_text(ie, "HSNCODE"),
        })
        tot_inv += amt

    return {
        "voucher_number": _find_text(voucher, "VOUCHERNUMBER"),
        "date":           _find_text(voucher, "DATE"),
        "voucher_type":   _find_text(voucher, "VOUCHERTYPENAME"),
        "reference":      _find_text(voucher, "REFERENCE"),
        "narration":      _find_text(voucher, "NARRATION"),
        "party":          party,
        "ledger_entries": ledger_entries,
        "inventory_items": inventory_items,
        "totals": {
            "ledger_debit":    round(tot_dr, 2),
            "ledger_credit":   round(tot_cr, 2),
            "inventory_value": round(tot_inv, 2),
        },
        "matched_count":  len(vouchers),
        "raw_xml":        raw,
        "tally_url":      _resolve_url(tally_url),
    }


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
    # ── Line items (array of dicts) ────────────────────────────────────────────
    items: list[dict[str, Any]] | None = None,
    # ── Voucher-level GST tax ledger entries ───────────────────────────────────
    cgst_ledger: str = "",
    cgst_amount: float = 0.0,
    sgst_ledger: str = "",
    sgst_amount: float = 0.0,
    igst_ledger: str = "",
    igst_amount: float = 0.0,
    # ── Voucher-level fields ───────────────────────────────────────────────────
    voucher_type: str = "Sales",
    voucher_number: str = "",
    narration: str = "",
    # ── Additional voucher-level ledgers (Freight, Insurance, Discount, etc.) ─
    additional_ledgers: list[dict[str, Any]] | None = None,
    # ── GST header fields ─────────────────────────────────────────────────────
    gst_registration_type: str = "",
    party_gstin: str = "",
    place_of_supply: str = "",
    state_name: str = "",
    country: str = "India",        # Buyer's country → <COUNTRYOFRESIDENCE> + <CONSIGNEECOUNTRYNAME>
    cmp_gstin: str = "",
    # ── Bill reference ─────────────────────────────────────────────────────────
    bill_name: str = "",
    bill_type: str = "New Ref",
    # ── E-Invoice (IRN) fields — written into TallyPrime's dedicated tags ─────
    irn: str = "",
    irn_qr_code: str = "",
    irn_ack_no: str = "",
    irn_ack_date: str = "",        # YYYYMMDD (8 digits)
    irn_ack_datetime: str = "",    # YYYYMMDDHHMMSSsss (17 digits)
    # ── E-Way Bill fields — written into <EWAYBILLDETAILS.LIST> ──────────────
    ewb_no: str = "",              # NIC EwbNo
    ewb_date: str = "",            # YYYYMMDD (8 digits)
    ewb_veh_no: str = "",          # vehicle number (e.g. KA01AB1234)
    ewb_trans_mode: str = "1",     # 1=Road, 2=Rail, 3=Air, 4=Ship
    ewb_veh_type: str = "R",       # R=Regular, O=Over-Dimensional Cargo
    tally_url: str | None = None,
) -> dict[str, Any]:
    """
    Create a Sales (invoice) voucher in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>Vouchers</ID> envelope with SVVCHIMPORTFORMAT.

    Supports multiple inventory line items via the `items` array.
    Each item dict must have:
        stock_item_name  (str, required) — product name as in TallyPrime
        sales_ledger     (str, required) — income ledger to credit
        quantity         (float) — item quantity
        unit             (str) — unit of measure (e.g. 'Nos', 'Kg')
        rate             (float) — price per unit
        amount           (float) — net line amount (post-discount, pre-tax)
        gst_rate         (float) — GST % for this item (e.g. 5, 12, 18, 28; 0 if exempt)
        hsn              (str, optional) — HSN/SAC code
        godown           (str, optional) — godown name (default: Main Location)
        discount_percent (float, optional) — discount %
        discount_amount  (float, optional) — discount amount

    GST tax ledger entries (voucher-level):
        cgst_ledger/cgst_amount  — CGST output ledger and amount (intrastate)
        sgst_ledger/sgst_amount  — SGST output ledger and amount (intrastate)
        igst_ledger/igst_amount  — IGST output ledger and amount (interstate)

    GST header fields:
        party_gstin, place_of_supply, state_name, cmp_gstin, gst_registration_type

    additional_ledgers: list of dicts with ledger_name, amount, is_addition.
    Party debit = sum of item amounts + additions - deductions + GST taxes.

    E-Invoice fields (TallyPrime's dedicated XML tags, optional):
        irn               → <IRN>
        irn_qr_code       → <IRNQRCODE>            (raw signed-QR JWT)
        irn_ack_no        → <IRNACKNO>
        irn_ack_date      → <IRNACKDATE>           (YYYYMMDD)
        irn_ack_datetime  → accepted for compatibility but not emitted

    E-Way Bill fields (written into <EWAYBILLDETAILS.LIST>, optional):
        ewb_no            → <BILLNUMBER>
        ewb_date          → <BILLDATE>              (YYYYMMDD)
        Sending ewb_no flips <ISEWAYBILLAPPLICABLE> to Yes automatically.
    """
    item_list = items or []
    extra_ledgers = additional_ledgers or []

    # ── Optional header XML ───────────────────────────────────────────────────
    vnum_xml   = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""
    gstreg_xml = f"<GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>" if gst_registration_type else ""
    gstin_xml  = f"<PARTYGSTIN>{_xe(party_gstin)}</PARTYGSTIN>"           if party_gstin    else ""
    pos_xml    = f"<PLACEOFSUPPLY>{_xe(place_of_supply)}</PLACEOFSUPPLY>" if place_of_supply else ""
    state_xml  = f"<STATENAME>{_xe(state_name)}</STATENAME>"              if state_name     else ""
    # COUNTRYOFRESIDENCE goes right after STATENAME (per Tally exporter sample).
    country_xml = f"<COUNTRYOFRESIDENCE>{_xe(country)}</COUNTRYOFRESIDENCE>" if country else ""
    cmpgstin_xml = f"<CMPGSTIN>{_xe(cmp_gstin)}</CMPGSTIN>"              if cmp_gstin      else ""
    # Consignee tags — mirror the buyer's state name and country
    # (consignee == buyer in our simple-import path).
    consignee_state_xml   = f"<CONSIGNEESTATENAME>{_xe(state_name)}</CONSIGNEESTATENAME>"     if state_name else ""
    consignee_country_xml = f"<CONSIGNEECOUNTRYNAME>{_xe(country)}</CONSIGNEECOUNTRYNAME>"     if country    else ""

    # ── E-Invoice / IRN tags ──────────────────────────────────────────────────
    # Match TallyPrime's exporter layout (Sales_EINVBIL-2 sample):
    #   <IRNACKDATE>   — right after <DATE> at the top of the voucher
    #   <IRN>          — in the upper party block (after PARTYLEDGERNAME)
    #   <IRNQRCODE>    — placed *separately*, after the company-GSTIN block
    #                    (Tally drops the QR if it's clustered next to <IRN>)
    #   <IRNACKNO>,
    #   <IRNIRPSOURCE> — clustered after the QR
    # IRNACKUPDATEDATETIME is intentionally omitted.
    # We only emit a tag if a value was supplied.
    irn_ack_date_xml = (
        f"<IRNACKDATE>{_xe(str(irn_ack_date))}</IRNACKDATE>"
        if irn_ack_date else ""
    )
    # Upper-block IRN tag (just <IRN>)
    irn_xml = f"<IRN>{_xe(irn)}</IRN>" if irn else ""
    # Standalone <IRNQRCODE> on its own line, far from <IRN>.
    # RESETIRNQRCODE=No is critical: without it Tally treats the import as a
    # "regenerate QR" request and silently drops the QR string we just sent.
    # IRNJSONEXPORTED / IRNCANCELLED keep Tally's e-invoice state machine sane.
    if irn_qr_code:
        irn_qrcode_xml = (
            f"<IRNQRCODE>{_xe(irn_qr_code)}</IRNQRCODE>\n                    "
            f"<RESETIRNQRCODE>No</RESETIRNQRCODE>\n                    "
            f"<IRNJSONEXPORTED>No</IRNJSONEXPORTED>\n                    "
            f"<IRNCANCELLED>No</IRNCANCELLED>"
        )
    else:
        irn_qrcode_xml = ""
    # AckNo + IRPSource, placed together near the QR
    irn_ackno_parts = []
    if irn_ack_no:
        irn_ackno_parts.append(f"<IRNACKNO>{_xe(str(irn_ack_no))}</IRNACKNO>")
    # NIC1 = NIC's primary IRP (einv1api). NIC2 would be einv2api.
    if irn or irn_ack_no:
        irn_ackno_parts.append("<IRNIRPSOURCE>NIC1</IRNIRPSOURCE>")
    irn_ackno_xml = "\n                    ".join(irn_ackno_parts)

    # ── E-Way Bill block ──────────────────────────────────────────────────────
    # TallyPrime stores EWB data inside <EWAYBILLDETAILS.LIST> rather than as
    # direct children of <VOUCHER>. Sample exporter layout:
    #   <EWAYBILLDETAILS.LIST>
    #     <BILLDATE>YYYYMMDD</BILLDATE>
    #     <BILLNUMBER>...</BILLNUMBER>
    #   </EWAYBILLDETAILS.LIST>
    # We also flip <ISEWAYBILLAPPLICABLE> to Yes so Tally treats the voucher
    # as EWB-bearing.
    ewb_xml = ""
    ewb_applicable_xml = ""
    if ewb_no:
        ewb_inner_parts = []
        if ewb_date:
            ewb_inner_parts.append(f"<BILLDATE>{_xe(str(ewb_date))}</BILLDATE>")
        ewb_inner_parts.append(f"<BILLNUMBER>{_xe(str(ewb_no))}</BILLNUMBER>")

        # Optional <TRANSPORTDETAILS.LIST> — emitted only when we have a
        # vehicle number. Tally formats these as "code - label" pairs:
        #   TRANSPORTMODE: "1 - Road" / "2 - Rail" / "3 - Air" / "4 - Ship"
        #   VEHICLETYPE:   "R - Regular" / "O - Over Dimensional Cargo"
        if ewb_veh_no:
            mode_map = {"1": "1 - Road", "2": "2 - Rail",
                        "3": "3 - Air",  "4": "4 - Ship"}
            type_map = {"R": "R - Regular", "O": "O - Over Dimensional Cargo"}
            mode_str = mode_map.get(str(ewb_trans_mode), "1 - Road")
            type_str = type_map.get(str(ewb_veh_type),  "R - Regular")
            transport_xml = (
                "<TRANSPORTDETAILS.LIST>\n                            "
                f"<TRANSPORTMODE>{_xe(mode_str)}</TRANSPORTMODE>\n                            "
                f"<VEHICLENUMBER>{_xe(ewb_veh_no)}</VEHICLENUMBER>\n                            "
                f"<VEHICLETYPE>{_xe(type_str)}</VEHICLETYPE>\n                        "
                "</TRANSPORTDETAILS.LIST>"
            )
            ewb_inner_parts.append(transport_xml)

        ewb_xml = (
            "<EWAYBILLDETAILS.LIST>\n                        "
            + "\n                        ".join(ewb_inner_parts)
            + "\n                    </EWAYBILLDETAILS.LIST>"
        )
        ewb_applicable_xml = "<ISEWAYBILLAPPLICABLE>Yes</ISEWAYBILLAPPLICABLE>"

    vch_type_safe = _xe(voucher_type)

    # ── GST Registration tag ──────────────────────────────────────────────────
    gst_reg_tag_xml = ""
    if gst_registration_type:
        gst_reg_tag_xml = f"""<GSTREGISTRATION.LIST>
                        <REGISTRATIONNAME>GST</REGISTRATIONNAME>
                        <APPLICABLEFROM>{date}</APPLICABLEFROM>
                        <STATE>{_xe(state_name or place_of_supply)}</STATE>
                        <REGISTRATIONNUMBER>{_xe(cmp_gstin or '')}</REGISTRATIONNUMBER>
                    </GSTREGISTRATION.LIST>"""

    # ── Build ALLINVENTORYENTRIES.LIST for each item ──────────────────────────
    inv_xml_parts = []
    for it in item_list:
        name    = _xe(it["stock_item_name"])
        ledger  = _xe(it.get("sales_ledger", ""))
        amt     = float(it.get("amount", 0))
        rate_v  = float(it.get("rate", 0))
        qty_v   = float(it.get("quantity", 0))
        unit_v  = _xe(str(it.get("unit", "")))
        godown  = _xe(str(it.get("godown", "Main Location")))
        hsn_v   = it.get("hsn", "")

        # Per-item GST rate
        item_gst = float(it.get("gst_rate", 0))
        if item_gst > 0:
            line_cgst_rate = round(item_gst / 2, 4)
            line_sgst_rate = round(item_gst / 2, 4)
            line_igst_rate = item_gst
        else:
            line_cgst_rate = 0.0
            line_sgst_rate = 0.0
            line_igst_rate = 0.0

        # Discount fields
        disc_pct = it.get("discount_percent")
        disc_amt = it.get("discount_amount")
        disc_pct_xml       = f"<DISCOUNT>{disc_pct}</DISCOUNT>"                     if disc_pct not in (None, "", 0, 0.0) else ""
        disc_amt_xml       = f"<DISCOUNTAMOUNT>{disc_amt}</DISCOUNTAMOUNT>"           if disc_amt not in (None, "", 0, 0.0) else ""
        batch_disc_amt_xml = f"<BATCHDISCOUNTAMOUNT>{disc_amt}</BATCHDISCOUNTAMOUNT>" if disc_amt not in (None, "", 0, 0.0) else ""

        rate_str = f"{rate_v}/{unit_v}" if unit_v else str(rate_v)
        qty_str  = f"{qty_v} {unit_v}".strip() if unit_v else str(qty_v)

        # HSN tag
        hsn_xml = f"<HSNMASTERNAME>{_xe(hsn_v)}</HSNMASTERNAME>" if hsn_v else ""

        inv_xml_parts.append(f"""<ALLINVENTORYENTRIES.LIST>
                        <STOCKITEMNAME>{name}</STOCKITEMNAME>
                        {hsn_xml}
                        <GSTOVRDNTAXABILITY>Taxable</GSTOVRDNTAXABILITY>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>
                        <GSTOVRDNISREVCHARGEAPPL>Not Applicable</GSTOVRDNISREVCHARGEAPPL>
                        <RATE>{rate_str}</RATE>
                        {disc_pct_xml}
                        <AMOUNT>{amt}</AMOUNT>
                        {disc_amt_xml}
                        <ACTUALQTY>{qty_str}</ACTUALQTY>
                        <BILLEDQTY>{qty_str}</BILLEDQTY>
                        <BATCHALLOCATIONS.LIST>
                            <GODOWNNAME>{godown}</GODOWNNAME>
                            <BATCHNAME>Primary Batch</BATCHNAME>
                            <AMOUNT>{amt}</AMOUNT>
                            {batch_disc_amt_xml}
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                        </BATCHALLOCATIONS.LIST>
                        <ACCOUNTINGALLOCATIONS.LIST>
                            <LEDGERNAME>{ledger}</LEDGERNAME>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{amt}</AMOUNT>
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
                    </ALLINVENTORYENTRIES.LIST>""")

    inv_xml = "\n                    ".join(inv_xml_parts)

    # ── Compute party debit total ─────────────────────────────────────────────
    base_total = sum(float(it.get("amount", 0)) for it in item_list)
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

    # ── Party ledger entry (ISDEEMEDPOSITIVE=Yes, negative total) ─────────────
    bill_xml = ""
    if bill_name:
        bill_xml = f"""
                        <BILLALLOCATIONS.LIST>
                            <NAME>{_xe(bill_name)}</NAME>
                            <BILLTYPE>{_xe(bill_type)}</BILLTYPE>
                            <AMOUNT>-{total}</AMOUNT>
                        </BILLALLOCATIONS.LIST>"""

    party_xml = f"""<LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
                        <AMOUNT>-{total}</AMOUNT>{bill_xml}
                    </LEDGERENTRIES.LIST>"""

    # ── GST tax ledger entries (ISDEEMEDPOSITIVE=No, positive amounts) ────────
    gst_xml = ""
    if cgst_ledger and cgst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(cgst_ledger)}</LEDGERNAME>
                        <METHODTYPE>GST</METHODTYPE>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>
                        <AMOUNT>{cgst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""
    if sgst_ledger and sgst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(sgst_ledger)}</LEDGERNAME>
                        <METHODTYPE>GST</METHODTYPE>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>
                        <AMOUNT>{sgst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""
    if igst_ledger and igst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(igst_ledger)}</LEDGERNAME>
                        <METHODTYPE>GST</METHODTYPE>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>
                        <AMOUNT>{igst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""

    # ── Additional non-GST ledger entries (Freight, Insurance, Discount, etc.) ─
    extra_xml = ""
    for ex in extra_ledgers:
        ex_amt      = float(ex["amount"])
        is_addition = ex.get("is_addition", True)
        deemed_pos  = "No" if is_addition else "Yes"
        xml_amt     = ex_amt if is_addition else -ex_amt
        extra_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(ex["ledger_name"])}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>{deemed_pos}</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>{deemed_pos}</ISLASTDEEMEDPOSITIVE>
                        <AMOUNT>{xml_amt}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""

    # ── Build the full XML envelope ───────────────────────────────────────────
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Vouchers</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVVCHIMPORTFORMAT>XML</SVVCHIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE>
                <VOUCHER VCHTYPE="{vch_type_safe}" ACTION="Create" OBJVIEW="Invoice Voucher View">
                    <OBJECTUPDATEACTION>Create</OBJECTUPDATEACTION>
                    <ISINVOICE>Yes</ISINVOICE>
                    <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
                    <DATE>{date}</DATE>
                    {irn_ack_date_xml}
                    <VOUCHERTYPENAME>{vch_type_safe}</VOUCHERTYPENAME>
                    {vnum_xml}
                    <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
                    {irn_xml}
                    {gstreg_xml}
                    {state_xml}
                    {country_xml}
                    {gstin_xml}
                    {pos_xml}
                    {cmpgstin_xml}
                    {gst_reg_tag_xml}
                    {consignee_state_xml}
                    {consignee_country_xml}
                    {irn_qrcode_xml}
                    {irn_ackno_xml}
                    {ewb_applicable_xml}
                    <NARRATION>{_xe(narration)}</NARRATION>
                    {ewb_xml}
                    {inv_xml}
                    {party_xml}
                    {gst_xml}
                    {extra_xml}
                </VOUCHER>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    errors = [e.text for e in root.findall(".//LINEERROR") if e.text]

    if int(created) >= 1:
        return {
            "status": "success",
            "message": f"Sales voucher created on {date} for party '{party_ledger}' with {len(item_list)} item(s), total ₹{total}",
            "created": int(created),
        }
    else:
        return {
            "status": "error",
            "message": "TallyPrime did not confirm creation of the sales voucher.",
            "errors": errors,
            "raw_response": raw[:2000],
        }


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
    items: list[dict[str, Any]] | None = None,
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
    cmp_gstin: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """
    Create a Purchase (invoice) voucher in TallyPrime using Item Invoice mode.

    Uses <TYPE>Data</TYPE> + <ID>Vouchers</ID> envelope with SVVCHIMPORTFORMAT.

    Sign conventions (Purchase):
      • Inventory ISDEEMEDPOSITIVE=Yes, amounts negative (stock debited — inward)
      • ACCOUNTINGALLOCATIONS: ISDEEMEDPOSITIVE=Yes, amounts negative
      • Party LEDGERENTRIES: ISDEEMEDPOSITIVE=No, ISPARTYLEDGER=Yes, amount positive
      • GST input-tax LEDGERENTRIES: ISDEEMEDPOSITIVE=Yes, ISPARTYLEDGER=No, amounts negative
      • reference → supplier bill number; creates BILLALLOCATIONS.LIST for payables tracking

    items: list of dicts, each with:
        stock_item_name  (str, required)
        purchase_ledger  (str, required) — per-item purchase/expense ledger
        amount           (float, required) — NET line amount (positive); negated for XML
        rate             (float, optional)
        quantity         (float, optional)
        unit             (str, optional)
        gst_rate         (float, optional) — per-line GST %
        hsn              (str, optional) — HSN/SAC code
        godown           (str, optional) — godown name (default: Main Location)
        discount_percent (float, optional)
        discount_amount  (float, optional) — pass as positive; negated for XML
    """
    item_list     = items or []
    extra_ledgers = additional_ledgers or []

    # ── Optional header XML ───────────────────────────────────────────────────
    vnum_xml     = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""
    ref_xml      = f"<REFERENCE>{_xe(reference)}</REFERENCE>"              if reference      else ""
    gstreg_xml   = f"<GSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</GSTREGISTRATIONTYPE>" if gst_registration_type else ""
    gstin_xml    = f"<PARTYGSTIN>{_xe(party_gstin)}</PARTYGSTIN>"           if party_gstin    else ""
    pos_xml      = f"<PLACEOFSUPPLY>{_xe(place_of_supply)}</PLACEOFSUPPLY>" if place_of_supply else ""
    state_xml    = f"<STATENAME>{_xe(state_name)}</STATENAME>"              if state_name     else ""
    cmpgstin_xml = f"<CMPGSTIN>{_xe(cmp_gstin)}</CMPGSTIN>"                if cmp_gstin      else ""

    vch_type_safe = _xe(voucher_type)

    # ── GST Registration tag ──────────────────────────────────────────────────
    gst_reg_tag_xml = ""
    if gst_registration_type:
        gst_reg_tag_xml = f"""<GSTREGISTRATION TAXTYPE="GST" TAXREGISTRATION="{_xe(cmp_gstin or '')}">{_xe(state_name or place_of_supply)} Registration</GSTREGISTRATION>
                    <CMPGSTREGISTRATIONTYPE>{_xe(gst_registration_type)}</CMPGSTREGISTRATIONTYPE>
                    <CMPGSTSTATE>{_xe(state_name or place_of_supply)}</CMPGSTSTATE>
                    <COUNTRYOFRESIDENCE>India</COUNTRYOFRESIDENCE>"""

    # ── Build ALLINVENTORYENTRIES.LIST for each item ──────────────────────────
    inv_xml_parts = []
    for it in item_list:
        name    = _xe(it["stock_item_name"])
        ledger  = _xe(it.get("purchase_ledger", ""))
        amt     = float(it.get("amount", 0))
        rate_v  = float(it.get("rate", 0))
        qty_v   = float(it.get("quantity", 0))
        unit_v  = _xe(str(it.get("unit", "")))
        godown  = _xe(str(it.get("godown", "Main Location")))
        hsn_v   = it.get("hsn", "")

        # Per-item GST rate
        item_gst = float(it.get("gst_rate", 0))
        if item_gst > 0:
            line_cgst_rate = round(item_gst / 2, 4)
            line_sgst_rate = round(item_gst / 2, 4)
            line_igst_rate = item_gst
        else:
            line_cgst_rate = 0.0
            line_sgst_rate = 0.0
            line_igst_rate = 0.0

        # Discount fields — purchase amounts are negative in XML
        disc_pct = it.get("discount_percent")
        disc_amt = it.get("discount_amount")
        disc_pct_xml = f"<DISCOUNT>{disc_pct}</DISCOUNT>" if disc_pct not in (None, "", 0, 0.0) else ""
        if disc_amt not in (None, "", 0, 0.0):
            neg_disc = -abs(float(disc_amt))
            disc_amt_xml       = f"<DISCOUNTAMOUNT>{neg_disc}</DISCOUNTAMOUNT>"
            batch_disc_amt_xml = f"<BATCHDISCOUNTAMOUNT>{neg_disc}</BATCHDISCOUNTAMOUNT>"
        else:
            disc_amt_xml = batch_disc_amt_xml = ""

        rate_str   = f"{rate_v}/{unit_v}" if unit_v else str(rate_v)
        qty_str    = f"{qty_v} {unit_v}".strip() if unit_v else str(qty_v)
        xml_amount = -amt  # negate for purchase XML

        # HSN tag
        hsn_xml = f"<HSNMASTERNAME>{_xe(hsn_v)}</HSNMASTERNAME>" if hsn_v else ""

        inv_xml_parts.append(f"""<ALLINVENTORYENTRIES.LIST>
                        <STOCKITEMNAME>{name}</STOCKITEMNAME>
                        {hsn_xml}
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
                            <GODOWNNAME>{godown}</GODOWNNAME>
                            <BATCHNAME>Primary Batch</BATCHNAME>
                            <DESTINATIONGODOWNNAME>{godown}</DESTINATIONGODOWNNAME>
                            <AMOUNT>{xml_amount}</AMOUNT>
                            {batch_disc_amt_xml}
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                        </BATCHALLOCATIONS.LIST>
                        <ACCOUNTINGALLOCATIONS.LIST>
                            <LEDGERNAME>{ledger}</LEDGERNAME>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
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
                        <RATEDETAILS.LIST>
                            <GSTRATEDUTYHEAD>State Cess</GSTRATEDUTYHEAD>
                            <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
                        </RATEDETAILS.LIST>
                    </ALLINVENTORYENTRIES.LIST>""")

    inv_xml = "\n                    ".join(inv_xml_parts)

    # ── Compute party credit total (positive) ─────────────────────────────────
    base_total = sum(float(i.get("amount", 0)) for i in item_list)
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

    # ── Party ledger entry (ISDEEMEDPOSITIVE=No, positive total) ──────────────
    bill_xml = ""
    if reference:
        bill_xml = f"""
                        <BILLALLOCATIONS.LIST>
                            <NAME>{_xe(reference)}</NAME>
                            <BILLTYPE>New Ref</BILLTYPE>
                            <AMOUNT>{total}</AMOUNT>
                        </BILLALLOCATIONS.LIST>"""

    party_xml = f"""<LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                        <AMOUNT>{total}</AMOUNT>{bill_xml}
                    </LEDGERENTRIES.LIST>"""

    # ── GST tax ledger entries (ISDEEMEDPOSITIVE=Yes, negative amounts) ───────
    gst_xml = ""
    if cgst_ledger and cgst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(cgst_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>No</ISPARTYLEDGER>
                        <AMOUNT>-{cgst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""
    if sgst_ledger and sgst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(sgst_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>No</ISPARTYLEDGER>
                        <AMOUNT>-{sgst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""
    if igst_ledger and igst_amount:
        gst_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(igst_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>No</ISPARTYLEDGER>
                        <AMOUNT>-{igst_amount}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""

    # ── Additional non-GST ledger entries ─────────────────────────────────────
    extra_xml = ""
    for ex in extra_ledgers:
        ex_amt      = float(ex["amount"])
        is_addition = ex.get("is_addition", True)
        deemed_pos  = "Yes" if is_addition else "No"
        xml_amt     = -ex_amt if is_addition else ex_amt
        extra_xml += f"""
                    <LEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(ex["ledger_name"])}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>{deemed_pos}</ISDEEMEDPOSITIVE>
                        <ISLASTDEEMEDPOSITIVE>{deemed_pos}</ISLASTDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>No</ISPARTYLEDGER>
                        <AMOUNT>{xml_amt}</AMOUNT>
                    </LEDGERENTRIES.LIST>"""

    # ── Build the full XML envelope ───────────────────────────────────────────
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Vouchers</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVVCHIMPORTFORMAT>XML</SVVCHIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE>
                <VOUCHER VCHTYPE="{vch_type_safe}" ACTION="Create" OBJVIEW="Invoice Voucher View">
                    <DATE>{date}</DATE>
                    {gstreg_xml}
                    {state_xml}
                    {gstin_xml}
                    {pos_xml}
                    {cmpgstin_xml}
                    {gst_reg_tag_xml}
                    <VOUCHERTYPENAME>{vch_type_safe}</VOUCHERTYPENAME>
                    {vnum_xml}
                    {ref_xml}
                    <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
                    <PARTYNAME>{_xe(party_ledger)}</PARTYNAME>
                    <BASICBASEPARTYNAME>{_xe(party_ledger)}</BASICBASEPARTYNAME>
                    <PARTYMAILINGNAME>{_xe(party_ledger)}</PARTYMAILINGNAME>
                    <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
                    <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
                    <EFFECTIVEDATE>{date}</EFFECTIVEDATE>
                    <ISINVOICE>Yes</ISINVOICE>
                    <NARRATION>{_xe(narration)}</NARRATION>
                    {inv_xml}
                    {party_xml}
                    {gst_xml}
                    {extra_xml}
                </VOUCHER>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    errors = [e.text for e in root.findall(".//LINEERROR") if e.text]

    if int(created) >= 1:
        return {
            "status": "success",
            "message": f"Purchase voucher created on {date} for party '{party_ledger}' with {len(item_list)} item(s), total ₹{total}",
            "created": int(created),
        }
    else:
        return {
            "status": "error",
            "message": "TallyPrime did not confirm creation of the purchase voucher.",
            "errors": errors,
            "raw_response": raw[:2000],
        }


def create_payment_voucher(
    date: str,
    party_ledger: str,
    bank_or_cash_ledger: str,
    amount: float,
    voucher_number: str = "",
    narration: str = "",
    bill_name: str = "",
    bill_type: str = "New Ref",
    transaction_type: str = "",
    transfer_mode: str = "",
    ifsc_code: str = "",
    bank_name: str = "",
    account_number: str = "",
    instrument_number: str = "",
    instrument_date: str = "",
    payment_favouring: str = "",
    email: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Payment voucher in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>Vouchers</ID> envelope with SVVCHIMPORTFORMAT.

    Party ledger is debited (ISDEEMEDPOSITIVE=Yes, negative amount).
    Bank/Cash ledger is credited (ISDEEMEDPOSITIVE=No, positive amount).

    Optionally includes:
      - BILLALLOCATIONS.LIST for bill-wise payment tracking
      - BANKALLOCATIONS.LIST for bank transfer details (NEFT/RTGS/IMPS etc.)

    Args:
        date:                Payment date in YYYYMMDD format
        party_ledger:        Supplier/party being paid
        bank_or_cash_ledger: Bank or Cash ledger name
        amount:              Payment amount
        voucher_number:      Optional voucher number
        narration:           Optional remarks
        bill_name:           Bill reference name/number for BILLALLOCATIONS
        bill_type:           Bill type: 'New Ref', 'Agst Ref', 'Advance' (default: New Ref)
        transaction_type:    e.g. 'Inter Bank Transfer', 'Others'
        transfer_mode:       e.g. 'NEFT', 'RTGS', 'IMPS', 'UPI'
        ifsc_code:           Bank IFSC code
        bank_name:           Beneficiary bank name
        account_number:      Beneficiary account number
        instrument_number:   Transaction/instrument reference number
        instrument_date:     Instrument date YYYYMMDD (defaults to voucher date)
        payment_favouring:   Payment in favour of (beneficiary name)
        email:               Email for payment notification
        tally_url:           Optional TallyPrime Gateway URL override
    """
    vnum_xml = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""

    # Bill allocation XML (optional)
    bill_xml = ""
    if bill_name:
        bill_xml = f"""
            <BILLALLOCATIONS.LIST>
                <NAME>{_xe(bill_name)}</NAME>
                <BILLTYPE>{_xe(bill_type)}</BILLTYPE>
                <AMOUNT>-{amount}</AMOUNT>
            </BILLALLOCATIONS.LIST>"""

    # Bank allocation XML (optional — for NEFT/RTGS/IMPS etc.)
    bank_alloc_xml = ""
    if transaction_type or transfer_mode:
        inst_date = instrument_date or date
        bank_alloc_xml = f"""
            <BANKALLOCATIONS.LIST>
                <DATE>{inst_date}</DATE>
                <INSTRUMENTDATE>{inst_date}</INSTRUMENTDATE>
                {f'<EMAIL>{_xe(email)}</EMAIL>' if email else ''}
                <TRANSACTIONTYPE>{_xe(transaction_type)}</TRANSACTIONTYPE>
                {f'<IFSCODE>{_xe(ifsc_code)}</IFSCODE>' if ifsc_code else ''}
                {f'<BANKNAME>{_xe(bank_name)}</BANKNAME>' if bank_name else ''}
                {f'<ACCOUNTNUMBER>{_xe(account_number)}</ACCOUNTNUMBER>' if account_number else ''}
                {f'<PAYMENTFAVOURING>{_xe(payment_favouring or party_ledger)}</PAYMENTFAVOURING>'}
                <TRANSACTIONNAME>Primary</TRANSACTIONNAME>
                {f'<TRANSFERMODE>{_xe(transfer_mode)}</TRANSFERMODE>' if transfer_mode else ''}
                {f'<INSTRUMENTNUMBER>{_xe(instrument_number)}</INSTRUMENTNUMBER>' if instrument_number else ''}
                <PAYMENTMODE>Transacted</PAYMENTMODE>
                <BANKPARTYNAME>{_xe(party_ledger)}</BANKPARTYNAME>
                <AMOUNT>{amount}</AMOUNT>
            </BANKALLOCATIONS.LIST>"""

    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Vouchers</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVVCHIMPORTFORMAT>XML</SVVCHIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE>
                <VOUCHER VCHTYPE="Payment" ACTION="Create" OBJVIEW="Accounting Voucher View">
                    <DATE>{date}</DATE>
                    <VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
                    {vnum_xml}
                    <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
                    <NARRATION>{_xe(narration)}</NARRATION>
                    <ALLLEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                        <AMOUNT>-{amount}</AMOUNT>{bill_xml}
                    </ALLLEDGERENTRIES.LIST>
                    <ALLLEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(bank_or_cash_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                        <AMOUNT>{amount}</AMOUNT>{bank_alloc_xml}
                    </ALLLEDGERENTRIES.LIST>
                </VOUCHER>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    errors = [e.text for e in root.findall(".//LINEERROR") if e.text]

    if int(created) >= 1:
        return {
            "status": "success",
            "message": f"Payment voucher created: ₹{amount} from {bank_or_cash_ledger} to {party_ledger}.",
            "created": created,
            "errors": errors,
        }
    else:
        return {
            "status": "error",
            "message": f"Failed to create payment voucher.",
            "created": created,
            "errors": errors or [raw[:500]],
        }


def create_receipt_voucher(
    date: str,
    party_ledger: str,
    bank_or_cash_ledger: str,
    amount: float,
    voucher_number: str = "",
    narration: str = "",
    transaction_type: str = "",
    bank_name: str = "",
    payment_favouring: str = "",
    instrument_number: str = "",
    instrument_date: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a Receipt voucher in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>Vouchers</ID> envelope with SVVCHIMPORTFORMAT.

    Party ledger is credited (ISDEEMEDPOSITIVE=No, positive amount).
    Bank/Cash ledger is debited (ISDEEMEDPOSITIVE=Yes, negative amount).

    Optionally includes:
      - BANKALLOCATIONS.LIST for cheque/DD/transfer details

    Args:
        date:                Receipt date in YYYYMMDD format
        party_ledger:        Customer/party who paid
        bank_or_cash_ledger: Bank or Cash ledger receiving the amount
        amount:              Receipt amount
        voucher_number:      Optional voucher number
        narration:           Optional remarks
        transaction_type:    e.g. 'Cheque/DD', 'Inter Bank Transfer'
        bank_name:           Payer bank name
        payment_favouring:   Payment in favour of (defaults to party_ledger)
        instrument_number:   Cheque/transaction reference number
        instrument_date:     Instrument date YYYYMMDD (defaults to voucher date)
        tally_url:           Optional TallyPrime Gateway URL override
    """
    vnum_xml = f"<VOUCHERNUMBER>{_xe(voucher_number)}</VOUCHERNUMBER>" if voucher_number else ""

    # Bank allocation XML (optional — for Cheque/DD/NEFT etc.)
    bank_alloc_xml = ""
    if transaction_type or instrument_number:
        inst_date = instrument_date or date
        bank_alloc_xml = f"""
            <BANKALLOCATIONS.LIST>
                <DATE>{inst_date}</DATE>
                <INSTRUMENTDATE>{inst_date}</INSTRUMENTDATE>
                <TRANSACTIONTYPE>{_xe(transaction_type)}</TRANSACTIONTYPE>
                {f'<BANKNAME>{_xe(bank_name)}</BANKNAME>' if bank_name else ''}
                <PAYMENTFAVOURING>{_xe(payment_favouring or party_ledger)}</PAYMENTFAVOURING>
                {f'<INSTRUMENTNUMBER>{_xe(instrument_number)}</INSTRUMENTNUMBER>' if instrument_number else ''}
                <PAYMENTMODE>Transacted</PAYMENTMODE>
                <BANKPARTYNAME>{_xe(party_ledger)}</BANKPARTYNAME>
                <ISCONNECTEDPAYMENT>No</ISCONNECTEDPAYMENT>
                <AMOUNT>-{amount}</AMOUNT>
            </BANKALLOCATIONS.LIST>"""

    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Vouchers</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVVCHIMPORTFORMAT>XML</SVVCHIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE xmlns:UDF="TallyUDF">
                <VOUCHER VCHTYPE="Receipt" ACTION="Create" OBJVIEW="Accounting Voucher View">
                    <DATE>{date}</DATE>
                    <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
                    {vnum_xml}
                    <PARTYLEDGERNAME>{_xe(party_ledger)}</PARTYLEDGERNAME>
                    <NARRATION>{_xe(narration)}</NARRATION>
                    <ALLLEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(party_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                        <AMOUNT>{amount}</AMOUNT>
                    </ALLLEDGERENTRIES.LIST>
                    <ALLLEDGERENTRIES.LIST>
                        <LEDGERNAME>{_xe(bank_or_cash_ledger)}</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                        <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                        <AMOUNT>-{amount}</AMOUNT>{bank_alloc_xml}
                    </ALLLEDGERENTRIES.LIST>
                </VOUCHER>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    errors = [e.text for e in root.findall(".//LINEERROR") if e.text]

    if int(created) >= 1:
        return {
            "status": "success",
            "message": f"Receipt voucher created: ₹{amount} from {party_ledger} into {bank_or_cash_ledger}.",
            "created": created,
            "errors": errors,
        }
    else:
        return {
            "status": "error",
            "message": f"Failed to create receipt voucher.",
            "created": created,
            "errors": errors or [raw[:500]],
        }


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

def _trial_balance_for_period(
    from8: str,
    to8: str,
    include_opening: bool,
    tally_url: str | None,
) -> list[dict[str, Any]]:
    """Trial balance for a single period (from8/to8 already YYYYMMDD or '')."""
    date_vars = ""
    if from8:
        date_vars += f"        <SVFROMDATE>{from8}</SVFROMDATE>\n"
    if to8:
        date_vars += f"        <SVTODATE>{to8}</SVTODATE>\n"

    # EXPLODEFLAG=Yes renders the report in "Detailed" format with all levels
    # expanded — drilling every primary group down through its sub-groups
    # (e.g. Capital Account -> Share Capital, Reserves & Surplus) instead of
    # returning only the collapsed top-level groups. Mirrors the on-screen config
    # "Format of Report: Detailed / Expand all levels in Detailed format: Yes".
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
{date_vars}        <EXPLODEFLAG>Yes</EXPLODEFLAG>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
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


def fetch_trial_balance(
    from_date: str = "",
    to_date: str = "",
    include_opening: bool = True,
    financial_years: list[str] | None = None,
    tally_url: str | None = None,
) -> Any:
    """Fetch trial balance using Tally's built-in Trial Balance report.

    Uses TYPE=Data with ID='Trial Balance' which triggers Tally's own report
    computation engine — the same engine used when you export Trial Balance
    from Tally's UI. Handles any date range without the hang caused by custom
    TDL period-balance computation.

    Response mirrors the actual TrialBal XML structure (DSP* display format):
      DSPOPDRAMTA / DSPOPCRAMTA → Opening Debit / Credit
      DSPCLDRAMTA / DSPCLCRAMTA → Closing Debit / Credit
    Both group-level and ledger-level rows are returned in document order.

    - financial_years: list like ["2024-25", "2025-26"] for a single-click
      multi-year fetch. Returns {"periods": [{financial_year, from_date,
      to_date, entry_count, entries:[...]}, ...]}.
    - Otherwise pass from_date/to_date (YYYYMMDD) for a single period; the
      result is the flat entries list (backward compatible).

    Args:
        include_opening: When True (default) returns opening_dr/opening_cr too;
                         when False only closing_dr/closing_cr.
    """
    if financial_years:
        periods = []
        for fy in financial_years:
            f8, t8 = _fy_to_period(str(fy))
            entries = _trial_balance_for_period(f8, t8, include_opening, tally_url)
            periods.append({
                "financial_year": str(fy),
                "from_date":       f8,
                "to_date":         t8,
                "entry_count":     len(entries),
                "entries":         entries,
            })
        return {
            "financial_years": [str(fy) for fy in financial_years],
            "period_count":    len(periods),
            "periods":         periods,
        }

    return _trial_balance_for_period(from_date, to_date, include_opening, tally_url)


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


def create_simple_unit(
    name: str,
    original_name: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a new simple Unit of Measure in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>All Masters</ID> — the correct format for
    TallyPrime Import requests via the XML Gateway.

    Args:
        name:          Unit symbol/short name (e.g. 'Box', 'Kg', 'Nos')
        original_name: Full/formal name of the unit (e.g. 'Boxes', 'Kilograms', 'Numbers').
                       If empty, defaults to the same as name.
        tally_url:     Optional TallyPrime Gateway URL override
    """
    formal_name = original_name or name

    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>All Masters</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVMSTIMPORTFORMAT>XML</SVMSTIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE xmlns:UDF="TallyUDF">
                <UNIT NAME="{_xe(name)}" ACTION="Create">
                    <NAME>{_xe(name)}</NAME>
                    <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
                    <ORIGINALNAME>{_xe(formal_name)}</ORIGINALNAME>
                </UNIT>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    altered = _find_text(root, ".//ALTERED", "0")
    errors  = _find_text(root, ".//ERRORS",  "0")

    if int(created) >= 1 or int(altered) >= 1:
        return {
            "status": "success",
            "message": f"Unit '{name}' ({formal_name}) created successfully.",
            "name": name,
            "original_name": formal_name,
            "created": created,
            "altered": altered,
        }
    else:
        error_desc = _find_text(root, ".//LINEERROR", "")
        if not error_desc:
            error_desc = _find_text(root, ".//LASTERROR", "")
        return {
            "status": "error",
            "message": f"Failed to create unit '{name}'.",
            "errors": errors,
            "error_details": error_desc or raw[:500],
        }


def get_unit(
    name: str,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Fetch details of a specific Unit of Measure from TallyPrime by name.

    Uses <TYPE>Object</TYPE> + <SUBTYPE>Unit</SUBTYPE> export request.

    Args:
        name:      Exact unit name as it appears in TallyPrime (e.g. 'Nos', 'Kg')
        tally_url: Optional TallyPrime Gateway URL override
    """
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Object</TYPE>
        <SUBTYPE>Unit</SUBTYPE>
        <ID TYPE="Name">{_xe(name)}</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <FETCHLIST>
                <FETCH>Name</FETCH>
            </FETCHLIST>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    _units = _collection_objects(root, "UNIT")
    unit = _units[0] if _units else None
    if unit is None:
        return {"error": f"Unit '{name}' not found in TallyPrime."}

    return {
        "name": unit.get("NAME") or _find_text(unit, "NAME"),
    }


def get_all_units(
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all simple Units of Measure from TallyPrime.

    Uses a TDL collection with a filter for IsSimpleUnit to return only
    simple (non-compound) units.

    Args:
        tally_url: Optional TallyPrime Gateway URL override
    """
    xml = """<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>TSPLSimpleUnits</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="TSPL SimpleUnits" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
                        <TYPE>Unit</TYPE>
                        <NATIVEMETHOD>Name, OriginalName, IsSimpleUnit</NATIVEMETHOD>
                        <FILTERS>TSPLSimpleUnitsOnly</FILTERS>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="TSPLSimpleUnitsOnly" ISMODIFY="No" ISFIXED="No" ISINTERNAL="No">$IsSimpleUnit  </SYSTEM>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    units = []
    for unit in _collection_objects(root, "UNIT"):
        units.append({
            "name": unit.get("NAME") or _find_text(unit, "NAME"),
            "original_name": _find_text(unit, "ORIGINALNAME"),
            "is_simple_unit": _find_text(unit, "ISSIMPLEUNIT"),
        })
    return units


def create_compound_unit(
    name: str,
    base_units: str,
    additional_units: str,
    conversion: int,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a new compound Unit of Measure in TallyPrime.

    A compound unit relates two simple units via a conversion factor,
    e.g. '1 Kg = 1000 gm'.

    Uses <TYPE>Data</TYPE> + <ID>All Masters</ID> — the correct format for
    TallyPrime Import requests via the XML Gateway.

    Args:
        name:             Compound unit name (e.g. 'Kg of 1000 gm')
        base_units:       Primary/base unit symbol (e.g. 'Kg')
        additional_units: Secondary unit symbol (e.g. 'gm')
        conversion:       How many additional_units make 1 base_unit (e.g. 1000)
        tally_url:        Optional TallyPrime Gateway URL override
    """
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>All Masters</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVMSTIMPORTFORMAT>XML</SVMSTIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE xmlns:UDF="TallyUDF">
                <UNIT NAME="{_xe(name)}" ACTION="Create">
                    <NAME>{_xe(name)}</NAME>
                    <BASEUNITS>{_xe(base_units)}</BASEUNITS>
                    <ADDITIONALUNITS>{_xe(additional_units)}</ADDITIONALUNITS>
                    <ISSIMPLEUNIT>No</ISSIMPLEUNIT>
                    <CONVERSION> {conversion}</CONVERSION>
                </UNIT>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    altered = _find_text(root, ".//ALTERED", "0")
    errors  = _find_text(root, ".//ERRORS",  "0")

    if int(created) >= 1 or int(altered) >= 1:
        return {
            "status": "success",
            "message": f"Compound unit '{name}' created successfully (1 {base_units} = {conversion} {additional_units}).",
            "name": name,
            "base_units": base_units,
            "additional_units": additional_units,
            "conversion": conversion,
            "created": created,
            "altered": altered,
        }
    else:
        error_desc = _find_text(root, ".//LINEERROR", "")
        if not error_desc:
            error_desc = _find_text(root, ".//LASTERROR", "")
        return {
            "status": "error",
            "message": f"Failed to create compound unit '{name}'.",
            "errors": errors,
            "error_details": error_desc or raw[:500],
        }


def get_all_stock_groups(
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all Stock Groups from TallyPrime.

    Uses <TYPE>Collection</TYPE> + <ID>StockGroup</ID> export request.
    Returns a list of stock groups with name and parent.

    Args:
        tally_url: Optional TallyPrime Gateway URL override
    """
    xml = """<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>StockGroup</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    groups = []
    for group in _collection_objects(root, "STOCKGROUP"):
        groups.append({
            "name": group.get("NAME") or _find_text(group, "NAME"),
            "parent": _find_text(group, "PARENT"),
        })
    return groups


def get_stock_group(
    name: str,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Fetch details of a specific Stock Group from TallyPrime by name.

    Uses <TYPE>Object</TYPE> + <SUBTYPE>Stock Group</SUBTYPE> export request.

    Args:
        name:      Exact stock group name as it appears in TallyPrime
        tally_url: Optional TallyPrime Gateway URL override
    """
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Object</TYPE>
        <SUBTYPE>Stock Group</SUBTYPE>
        <ID TYPE="Name">{_xe(name)}</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <FETCHLIST>
                <FETCH>Name</FETCH>
                <FETCH>Parent</FETCH>
                <FETCH>Opening Balance</FETCH>
                <FETCH>Closing Balance</FETCH>
            </FETCHLIST>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    _groups = _collection_objects(root, "STOCKGROUP")
    group = _groups[0] if _groups else None
    if group is None:
        return {"error": f"Stock group '{name}' not found in TallyPrime."}

    return {
        "name": group.get("NAME") or _find_text(group, "NAME"),
        "parent": _find_text(group, "PARENT"),
        "opening_balance": _find_text(group, "OPENINGBALANCE"),
        "closing_balance": _find_text(group, "CLOSINGBALANCE"),
    }


def create_stock_group(
    name: str,
    parent: str = "",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a new Stock Group in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>All Masters</ID> — the correct format for
    TallyPrime Import requests via the XML Gateway.

    Args:
        name:      Stock group name (e.g. 'Tea Products', 'Electronics')
        parent:    Parent stock group (leave empty for top-level under Primary)
        tally_url: Optional TallyPrime Gateway URL override
    """
    parent_xml = f"<PARENT>{_xe(parent)}</PARENT>" if parent else ""

    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>All Masters</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVMSTIMPORTFORMAT>XML</SVMSTIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE xmlns:UDF="TallyUDF">
                <STOCKGROUP NAME="{_xe(name)}" Action="Create">
                    <NAME>{_xe(name)}</NAME>
                    {parent_xml}
                </STOCKGROUP>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    created = _find_text(root, ".//CREATED", "0")
    altered = _find_text(root, ".//ALTERED", "0")
    errors  = _find_text(root, ".//ERRORS",  "0")

    if int(created) >= 1 or int(altered) >= 1:
        return {
            "status": "success",
            "message": f"Stock group '{name}' created successfully.",
            "name": name,
            "parent": parent or "(Primary)",
            "created": created,
            "altered": altered,
        }
    else:
        error_desc = _find_text(root, ".//LINEERROR", "")
        if not error_desc:
            error_desc = _find_text(root, ".//LASTERROR", "")
        return {
            "status": "error",
            "message": f"Failed to create stock group '{name}'.",
            "errors": errors,
            "error_details": error_desc or raw[:500],
        }


def get_stock_items_of_group(
    group_name: str,
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all Stock Items belonging to a specific Stock Group in TallyPrime.

    Uses a TDL collection with CHILDOF filter to retrieve only items under
    the specified stock group.

    Args:
        group_name: Exact stock group name as it appears in TallyPrime (e.g. 'Gadgets', 'Raw Materials')
        tally_url:  Optional TallyPrime Gateway URL override
    """
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>TSPLStockOfGroup</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="TSPLStockOfGroup" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No">
                        <TYPE>StockItem</TYPE>
                        <CHILDOF>&quot;{_xe(group_name)}&quot;</CHILDOF>
                        <NATIVEMETHOD>Name, Parent, ClosingBalance, ClosingValue, BaseUnits</NATIVEMETHOD>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    items = []
    for item in _collection_objects(root, "STOCKITEM"):
        items.append({
            "name": item.get("NAME") or _find_text(item, "NAME"),
            "parent": _find_text(item, "PARENT"),
            "base_units": _find_text(item, "BASEUNITS"),
            "closing_balance": _find_text(item, "CLOSINGBALANCE"),
            "closing_value": _find_text(item, "CLOSINGVALUE"),
        })
    return items


def get_all_stock_items(
    tally_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all Stock Items from TallyPrime.

    Uses <TYPE>Collection</TYPE> + <ID>StockItem</ID> export request.
    Returns a list of stock items with name and parent group.

    Args:
        tally_url: Optional TallyPrime Gateway URL override
    """
    xml = """<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>StockItem</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    items = []
    for item in _collection_objects(root, "STOCKITEM"):
        items.append({
            "name": item.get("NAME") or _find_text(item, "NAME"),
            "parent": _find_text(item, "PARENT"),
        })
    return items


def get_stock_item(
    name: str,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Fetch details of a specific Stock Item from TallyPrime by name.

    Uses a Collection request with a TDL name-filter — the same reliable
    pattern used by fetch_ledger. (The earlier <TYPE>Object</TYPE> approach is
    not supported by the TallyPrime XML Gateway and frequently returns
    "not found" even when the item exists.)

    Args:
        name:      Exact stock item name as it appears in TallyPrime
        tally_url: Optional TallyPrime Gateway URL override
    """
    safe_name = name.replace("&", "&amp;").replace('"', "&quot;")
    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>MCPStockItemDetail</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="MCPStockItemDetail" ISMODIFY="No">
            <TYPE>StockItem</TYPE>
            <FETCH>Name,Parent,BaseUnits,ClosingBalance,ClosingValue</FETCH>
            <FILTER>MCPStockItemByName</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="MCPStockItemByName">$Name = "{safe_name}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    _items = _collection_objects(root, "STOCKITEM")
    item = _items[0] if _items else None
    if item is None:
        return {
            "error": f"Stock item '{name}' not found in TallyPrime.",
            "tally_url": _resolve_url(tally_url),
        }

    return {
        "name":            item.get("NAME") or _find_text(item, "NAME")  or _rx(raw, "NAME"),
        "parent":          _find_text(item, "PARENT")          or _rx(raw, "PARENT"),
        "base_units":      _find_text(item, "BASEUNITS")       or _rx(raw, "BASEUNITS"),
        "closing_balance": _find_text(item, "CLOSINGBALANCE")  or _rx(raw, "CLOSINGBALANCE"),
        "closing_value":   _find_text(item, "CLOSINGVALUE")    or _rx(raw, "CLOSINGVALUE"),
        "tally_url":       _resolve_url(tally_url),
    }


def create_stock_item(
    name: str,
    parent: str = "\x04 Primary",
    base_units: str = "nos",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Create a new Stock Item in TallyPrime.

    Uses <TYPE>Data</TYPE> + <ID>All Masters</ID> — the correct format for
    TallyPrime Import requests via the XML Gateway.

    Args:
        name:       Stock item name (e.g. 'Tea Powder')
        parent:     Stock group (default: Primary)
        base_units: Unit of measure (e.g. 'nos', 'kg', 'pcs')
        tally_url:  Optional TallyPrime Gateway URL override
    """
    xml = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Import</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>All Masters</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVMSTIMPORTFORMAT>XML</SVMSTIMPORTFORMAT>
            </STATICVARIABLES>
            <TALLYMESSAGE>
                <StockItem NAME="{_xe(name)}" Action="Create">
                    <NAME>{_xe(name)}</NAME>
                    <PARENT>&#4; {_xe(parent)}</PARENT>
                    <BaseUnits>{_xe(base_units)}</BaseUnits>
                </StockItem>
            </TALLYMESSAGE>
        </DESC>
    </BODY>
</ENVELOPE>"""
    raw = _post_xml(xml, tally_url)
    root = _parse_xml(raw)

    # Tally returns <CREATED>1</CREATED> on success
    created = _find_text(root, ".//CREATED", "0")
    altered = _find_text(root, ".//ALTERED", "0")
    errors  = _find_text(root, ".//ERRORS",  "0")

    if int(created) >= 1 or int(altered) >= 1:
        return {
            "status": "success",
            "message": f"Stock item '{name}' created successfully.",
            "name": name,
            "parent": parent,
            "base_units": base_units,
            "created": created,
            "altered": altered,
        }
    else:
        # Try to extract error details from response
        error_desc = _find_text(root, ".//LINEERROR", "")
        if not error_desc:
            error_desc = _find_text(root, ".//LASTERROR", "")
        return {
            "status": "error",
            "message": f"Failed to create stock item '{name}'.",
            "errors": errors,
            "error_details": error_desc or raw[:500],
        }


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
        for item in _collection_objects(root, "STOCKITEM")
    ]


def _stock_value_num(s: str) -> float:
    """Parse a Tally stock value (Indian commas, optional Dr/Cr) into a float."""
    s = (s or "").strip().replace(",", "")
    if not s or s == "-":
        return 0.0
    neg = s.endswith("Cr")           # Cr stock value (rare) -> negative
    s = s.replace("Dr", "").replace("Cr", "").strip()
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _fy_to_period(fy: str) -> tuple[str, str]:
    """Convert a financial-year label to (from_yyyymmdd, to_yyyymmdd).

    Accepts '2024-25', '2024-2025', '2024/25', 'FY2024-25', or '2024'
    (April 1 of the year to March 31 of the next).
    """
    yrs = re.findall(r"\d{4}", fy) + re.findall(r"(?<!\d)\d{2}(?!\d)", fy)
    m = re.search(r"(\d{4})\D+(\d{2,4})", fy)
    if m:
        y1 = int(m.group(1))
        end = m.group(2)
        y2 = int(end) if len(end) == 4 else (y1 // 100) * 100 + int(end)
        if y2 <= y1:
            y2 = y1 + 1
    else:
        m2 = re.search(r"(\d{4})", fy)
        if not m2:
            raise ValueError(f"Unrecognised financial year: {fy!r}")
        y1 = int(m2.group(1))
        y2 = y1 + 1
    return f"{y1}0401", f"{y2}0331"


def _fetch_stock_masters(tally_url: str | None) -> tuple[dict[str, str], list[str]]:
    """Item -> stock group map (masters only; instant, no valuation)."""
    coll_xml = """<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE><ID>MCP Stock Items</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="MCP Stock Items" ISMODIFY="No">
        <TYPE>Stock Item</TYPE><FETCH>Name,Parent</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(coll_xml, tally_url, timeout=60.0))
    item_group: dict[str, str] = {}
    order: list[str] = []
    for it in _collection_objects(root, "STOCKITEM"):
        nm = (it.get("NAME") or _find_text(it, "NAME")).strip()
        if nm and nm not in item_group:
            item_group[nm] = _find_text(it, "PARENT")
            order.append(nm)
    return item_group, order


def _stock_summary_for_period(
    item_group: dict[str, str],
    order: list[str],
    from8: str,
    to8: str,
    tally_url: str | None,
) -> dict[str, Any]:
    """Item-wise opening/closing stock value for one period via the built-in
    Stock Summary report (batch valuation — fast, no per-item hang)."""
    date_vars = ""
    if from8:
        date_vars += f"    <SVFROMDATE>{from8}</SVFROMDATE>\n"
    if to8:
        date_vars += f"    <SVTODATE>{to8}</SVTODATE>\n"

    rep_xml = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE><ID>Stock Summary</ID></HEADER>
  <BODY><DESC><STATICVARIABLES>
{date_vars}    <DSPSHOWOPENING>Yes</DSPSHOWOPENING>
    <EXPLODEFLAG>Yes</EXPLODEFLAG>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
  </STATICVARIABLES></DESC></BODY>
</ENVELOPE>"""
    root = _parse_xml(_post_xml(rep_xml, tally_url, timeout=120.0))

    op_val: dict[str, float] = {}
    cl_val: dict[str, float] = {}
    kids = list(root)
    for i, ch in enumerate(kids):
        if ch.tag != "DSPACCNAME":
            continue
        name = (ch.findtext("DSPDISPNAME") or "").strip()
        if name not in item_group:
            continue   # group sub-total row — skip
        info = kids[i + 1] if i + 1 < len(kids) and kids[i + 1].tag == "DSPSTKINFO" else None
        if info is None:
            continue
        # report values are Dr-negative; flip so assets read positive (like Excel)
        op_val[name] = -_stock_value_num(info.findtext("DSPSTKOP/DSPOPAMTA") or "")
        cl_val[name] = -_stock_value_num(info.findtext("DSPSTKCL/DSPCLAMTA") or "")

    items: list[dict[str, Any]] = []
    total_open = 0.0
    total_close = 0.0
    for nm in order:
        o = round(op_val.get(nm, 0.0), 2)
        c = round(cl_val.get(nm, 0.0), 2)
        items.append({
            "stock_item":    nm,
            "stock_group":   item_group[nm],
            "opening_value": o,
            "closing_value": c,
        })
        total_open += o
        total_close += c

    return {
        "from_date":           from8,
        "to_date":             to8,
        "item_count":          len(items),
        "total_opening_value": round(total_open, 2),
        "total_closing_value": round(total_close, 2),
        "items":               items,
    }


def fetch_stock_summary_sch3(
    from_date: str = "",
    to_date: str = "",
    financial_years: list[str] | None = None,
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Schedule III style item-wise Stock Summary (value only), per financial year.

    Returns every stock item with its stock group (parent), opening value and
    closing value, plus grand totals — matching the 'Details of Stock' export
    (Particulars | Stock Group | Opening Balance | Closing Balance).

    Uses Tally's built-in Stock Summary report (batch valuation) scoped by
    SVFROMDATE/SVTODATE — fast for any financial year, no per-item hang. Values
    are sign-flipped to the report convention (assets positive).

    - financial_years: list like ["2024-25", "2025-26"] for a single-click
      multi-year fetch. Returns {"periods": [<one result per FY>, ...]}.
    - Otherwise pass from_date/to_date for a single period (DD-MM-YYYY,
      DD/MM/YYYY, YYYY-MM-DD or YYYYMMDD); omit both for the current period.
    """
    item_group, order = _fetch_stock_masters(tally_url)

    if financial_years:
        periods = []
        for fy in financial_years:
            f8, t8 = _fy_to_period(str(fy))
            res = _stock_summary_for_period(item_group, order, f8, t8, tally_url)
            res = {"financial_year": str(fy), **res}
            periods.append(res)
        return {
            "financial_years": [str(fy) for fy in financial_years],
            "period_count":    len(periods),
            "periods":         periods,
        }

    from8 = _parse_date(from_date) if from_date else ""
    to8   = _parse_date(to_date) if to_date else ""
    return _stock_summary_for_period(item_group, order, from8, to8, tally_url)


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
    bill_kind: str = "receivable",
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
    if bill_kind == "payable":
        # Payables (Sundry Creditors) — Bills Payable report family
        _REPORT_ID_CANDIDATES = [
            "Bills Payable",                # standard payables report ID
            "Bills Outstanding",            # combined display name (covers payables too)
            "Payables",                     # alternate internal name in some builds
            "Outstanding Payables",         # older Tally/ERP9 name
            "Outstandings",                 # catch-all fallback
        ]
    else:
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


# Ageing buckets emitted by the MCP Group Ageing TDL report, mapped from the
# TDL's BUCKET codes to display labels. Slab boundaries (days, by bill date):
# 0-90 / 90-180 / 180-365 / 365-1035 / 1035+ , taken from the saved view's
# Age From / Age To arrays. "On Account" is derived (closing - dated bills).
_AGEING_BUCKET_MAP = [
    ("B0_90",      "< 90 days"),
    ("B90_180",    "90 to 180 days"),
    ("B180_365",   "180 to 365 days"),
    ("B365_1035",  "365 to 1035 days"),
    ("B1035_PLUS", "> 1035 days"),
]
_AGEING_LABELS = [lbl for _, lbl in _AGEING_BUCKET_MAP] + ["On Account"]


def _ageing_num(s: str) -> float:
    """Parse a Tally display amount (with Indian commas) into a float."""
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _ageing_signed(mag_text: str, isdr_text: str) -> float:
    """Apply sign in the Group-Outstandings convention: Dr negative, Cr positive."""
    mag = _ageing_num(mag_text)
    return -mag if (isdr_text or "").strip().lower() == "yes" else mag


def _ageing_for_period(
    group_name: str,
    from8: str,
    to8: str,
    report_id: str,
    tally_url: str | None,
) -> dict[str, Any]:
    """Ageing for a single period (from8/to8 already YYYYMMDD or ''). Ages bills
    as of to8 (the as-of date). Returns the per-period result or an error dict."""
    date_vars = ""
    if from8:
        date_vars += f"        <SVFROMDATE>{from8}</SVFROMDATE>\n"
    if to8:
        date_vars += f"        <SVTODATE>{to8}</SVTODATE>\n"

    xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>{_xe(report_id)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
{date_vars}        <MCPGroup>{_xe(group_name)}</MCPGroup>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""

    raw = _post_xml(xml, tally_url, timeout=120.0)
    if "Unknown Request" in raw or "Could not find Report" in raw:
        return {
            "error": f"The '{report_id}' TDL report is not loaded in TallyPrime. "
                     "Load tdl/mcp_group_ageing.txt via F1 > TDL & Add-Ons "
                     "(or restart Tally), then retry. "
                     f"Tally said: {raw.strip()[:200]}",
            "group": group_name,
            "tally_url": _resolve_url(tally_url),
        }

    root = _parse_xml(raw)
    label_for = {code: lbl for code, lbl in _AGEING_BUCKET_MAP}

    # Sum signed bill amounts per (party, bucket); track each party's dated total.
    day: dict[str, dict[str, float]] = {}
    dated_sum: dict[str, float] = {}
    for bill in root.findall(".//BILL"):
        party = (bill.findtext("PARTY") or "").strip()
        label = label_for.get((bill.findtext("BUCKET") or "").strip())
        if not party or label is None:
            continue   # ONACCOUNT-bucket bills excluded; On Account derived below
        val = _ageing_signed(bill.findtext("AMT"), bill.findtext("BDP"))
        day.setdefault(party, {})
        day[party][label] = round(day[party].get(label, 0.0) + val, 2)
        dated_sum[party] = round(dated_sum.get(party, 0.0) + val, 2)

    # One row per ledger; total = closing, On Account = closing - dated bills.
    parties: list[dict[str, Any]] = []
    for led in root.findall(".//LEDGER"):
        party = (led.findtext("LPARTY") or "").strip()
        if not party:
            continue
        total = _ageing_signed(led.findtext("LCLOSING"), led.findtext("LDP"))
        row: dict[str, Any] = {"party": party, "total": round(total, 2)}
        for _, label in _AGEING_BUCKET_MAP:
            row[label] = round(day.get(party, {}).get(label, 0.0), 2)
        row["On Account"] = round(total - dated_sum.get(party, 0.0), 2)
        parties.append(row)

    parties.sort(key=lambda r: -abs(r["total"]))

    ageing_totals = {
        label: round(sum(r.get(label, 0.0) for r in parties), 2)
        for label in _AGEING_LABELS
    }
    grand = round(sum(r["total"] for r in parties), 2)

    return {
        "group":             group_name,
        "report_id":         report_id,
        "from_date":         from8,
        "to_date":           to8,
        "buckets":           _AGEING_LABELS,
        "party_count":       len(parties),
        "total_outstanding": grand,
        "ageing_totals":     ageing_totals,
        "parties":           parties,
    }


def fetch_ageing_analysis(
    group_name: str = "Sundry Debtors",
    from_date: str = "",
    to_date: str = "",
    financial_years: list[str] | None = None,
    report_id: str = "MCP Group Ageing",
    tally_url: str | None = None,
) -> dict[str, Any]:
    """Bill-wise ageing analysis for a group, via the MCP Group Ageing TDL report.

    The XML gateway cannot render Tally's age-wise Group Outstandings columns
    (that layout is a saved report-view feature, not a gateway parameter). This
    instead calls a custom TDL report (tdl/mcp_group_ageing.txt, loaded in Tally
    via F1 > TDL & Add-Ons) that has Tally itself compute, per outstanding bill:
    party, bill date, age in days (by bill date), ageing bucket (slabs
    0-90/90-180/180-365/365-1035/1035+), the bill's outstanding amount and its
    Dr/Cr flag; plus each ledger's net closing balance.

    Amounts are summed per (party, bucket); each party's total comes from its
    ledger closing, and On Account = closing - sum(dated bills). Output matches
    Tally's own Group Outstandings ageing sign-for-sign. Works for any group, so
    Sundry Creditors flips signs naturally (Cr positive).

    - financial_years: list like ["2024-25", "2025-26"] for a single-click
      multi-year fetch. Each year is aged AS OF its closing date (31-Mar).
      Returns {"periods": [<one result per FY>, ...]}.
    - Otherwise pass from_date/to_date for a single as-of period; omit both for
      the current period.

    Single-period result: group, report_id, from_date, to_date, buckets,
    party_count, total_outstanding, ageing_totals and parties.
    """
    if financial_years:
        periods = []
        for fy in financial_years:
            f8, t8 = _fy_to_period(str(fy))
            res = _ageing_for_period(group_name, f8, t8, report_id, tally_url)
            if res.get("error"):
                return res   # e.g. TDL report not loaded — surface immediately
            res = {"financial_year": str(fy), **res}
            periods.append(res)
        return {
            "group":           group_name,
            "report_id":       report_id,
            "financial_years": [str(fy) for fy in financial_years],
            "period_count":    len(periods),
            "periods":         periods,
        }

    from8 = _parse_date(from_date) if from_date else ""
    to8   = _parse_date(to_date) if to_date else ""
    return _ageing_for_period(group_name, from8, to8, report_id, tally_url)
# end of module
