"""
TallyPrime MCP Server
Exposes TallyPrime accounting data and operations as MCP tools.

Every tool accepts an optional `tally_url` parameter, allowing a single
cloud-deployed MCP server to connect to different TallyPrime instances
at runtime (e.g. different Cloudflare Tunnel URLs per client/company).

Usage:
    python -m tallyprime_mcp.server        # stdio mode (Claude Desktop)
    python -m tallyprime_mcp.server_http   # HTTP/SSE mode (cloud)
"""

import json
import logging
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from . import tally_client as tc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Server("tallyprime-mcp")

# ── Shared tally_url property added to every tool schema ─────────────────────
TALLY_URL_PROP = {
    "tally_url": {
        "type": "string",
        "description": (
            "TallyPrime Gateway URL to connect to "
            "(e.g. https://xyz.trycloudflare.com or http://localhost:9000). "
            "Overrides the server's default TALLY_URL env var for this call only."
        ),
        "default": "",
    }
}


# ═══════════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ═══════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Company ───────────────────────────────────────────────
        types.Tool(
            name="get_active_company",
            description=(
                "Get the currently active/open company in TallyPrime. "
                "Returns company name, financial year dates, base currency, "
                "GSTIN, state, country, phone, email, and address."
            ),
            inputSchema={
                "type": "object",
                "properties": {**TALLY_URL_PROP},
                "required": [],
            },
        ),

        # ── Ledgers ───────────────────────────────────────────────
        types.Tool(
            name="get_all_ledgers",
            description="List all ledgers in the active TallyPrime company with their parent group, opening and closing balances.",
            inputSchema={
                "type": "object",
                "properties": {**TALLY_URL_PROP},
                "required": [],
            },
        ),
        types.Tool(
            name="get_ledger",
            description="Get full details of a specific ledger including GSTIN, PAN, address, credit terms, and bill-wise settings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact ledger name as it appears in TallyPrime"},
                    **TALLY_URL_PROP,
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="create_party_ledger",
            description="Create a new party ledger (account) in TallyPrime under a specified group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Ledger name to create"},
                    "parent": {"type": "string", "description": "Parent group (e.g. 'Sundry Debtors', 'Bank Accounts', 'Sales Accounts')"},
                    "opening_balance": {"type": "number", "description": "Opening balance amount (default 0)", "default": 0},
                    "gstin": {"type": "string", "description": "GST Identification Number (15-char)", "default": ""},
                    "gst_registration_type": {
                        "type": "string",
                        "description": "GST registration type",
                        "enum": ["Regular", "Composition", "Unregistered", "Consumer", "Overseas", "SEZ"],
                        "default": "Regular",
                    },
                    "address": {"type": "string", "description": "Party address (use \\n for multiple lines)", "default": ""},
                    "state": {"type": "string", "description": "State name (e.g. 'Karnataka', 'Delhi')", "default": ""},
                    "country": {"type": "string", "description": "Country name", "default": "India"},
                    "pincode": {"type": "string", "description": "PIN / ZIP code", "default": ""},
                    "phone": {"type": "string", "description": "Phone number", "default": ""},
                    "email": {"type": "string", "description": "Email address", "default": ""},
                    "credit_period": {"type": "string", "description": "Credit period (e.g. '30 Days')", "default": ""},
                    "credit_limit": {"type": "number", "description": "Credit limit amount", "default": 0},
                    **TALLY_URL_PROP,
                },
                "required": ["name", "parent"],
            },
        ),
        types.Tool(
            name="create_sales_ledger",
            description=(
                "Create a new Sales or Income ledger in TallyPrime with GST details. "
                "Provide a single gst_rate (e.g. 18 for 18% GST) — "
                "IGST is set to gst_rate, CGST and SGST are each set to gst_rate/2. "
                "effective_date sets APPLICABLEFROM for GST and HSN details."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Ledger name (e.g. 'GST Sales 18%', 'Local Sales 5%', 'Exempt Sales')"},
                    "effective_date": {
                        "type": "string",
                        "description": (
                            "Date from which GST details are effective (APPLICABLEFROM). "
                            "Accepted formats: DD-MM-YYYY (e.g. '01-04-2025'), DD/MM/YYYY, "
                            "YYYY-MM-DD, or YYYYMMDD."
                        ),
                    },
                    "parent": {"type": "string", "description": "Parent group", "default": "Sales Accounts"},
                    "gst_type_of_supply": {
                        "type": "string",
                        "description": "Type of supply",
                        "enum": ["Goods", "Services"],
                        "default": "Goods",
                    },
                    "taxability": {
                        "type": "string",
                        "description": "GST taxability",
                        "enum": ["Taxable", "Exempt", "Nil Rated", "Non-GST"],
                        "default": "Taxable",
                    },
                    "gst_nature_of_transaction": {
                        "type": "string",
                        "description": "GST nature of transaction (e.g. 'Local Sales - Taxable', 'Interstate Sales - Taxable', 'Exports - Taxable'). Leave blank to omit.",
                        "default": "",
                    },
                    "hsn_sac_code": {"type": "string", "description": "HSN code (goods) or SAC code (services)", "default": ""},
                    "hsn_description": {"type": "string", "description": "Description of the HSN/SAC code (e.g. 'Steel', 'Software Services')", "default": ""},
                    "gst_rate": {
                        "type": "number",
                        "description": "Total GST rate % (e.g. 18 for 18% GST, 5 for 5%). IGST = gst_rate; CGST = SGST = gst_rate / 2.",
                        "default": 0,
                    },
                    "is_reverse_charge": {"type": "boolean", "description": "Mark as Reverse Charge Applicable", "default": False},
                    **TALLY_URL_PROP,
                },
                "required": ["name", "effective_date"],
            },
        ),
        types.Tool(
            name="create_purchase_ledger",
            description=(
                "Create a new Purchase or Expense ledger in TallyPrime with GST details. "
                "Provide a single gst_rate (e.g. 18 for 18% GST) — "
                "IGST is set to gst_rate, CGST and SGST are each set to gst_rate/2. "
                "effective_date sets APPLICABLEFROM for GST and HSN details."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Ledger name (e.g. 'Interstate Purchase 18%', 'Local Purchase 5%')"},
                    "effective_date": {
                        "type": "string",
                        "description": (
                            "Date from which GST details are effective (APPLICABLEFROM). "
                            "Accepted formats: DD-MM-YYYY (e.g. '01-04-2025'), DD/MM/YYYY, "
                            "YYYY-MM-DD, or YYYYMMDD."
                        ),
                    },
                    "parent": {"type": "string", "description": "Parent group", "default": "Purchase Accounts"},
                    "gst_type_of_supply": {
                        "type": "string",
                        "description": "Type of supply",
                        "enum": ["Goods", "Services"],
                        "default": "Goods",
                    },
                    "taxability": {
                        "type": "string",
                        "description": "GST taxability",
                        "enum": ["Taxable", "Exempt", "Nil Rated", "Non-GST"],
                        "default": "Taxable",
                    },
                    "gst_nature_of_transaction": {
                        "type": "string",
                        "description": "GST nature of transaction (e.g. 'Interstate Purchase - Taxable', 'Intrastate Purchase - Taxable', 'Interstate Purchase - Exempt'). Defaults to 'Interstate Purchase - Taxable'.",
                        "default": "Interstate Purchase - Taxable",
                    },
                    "hsn_sac_code": {"type": "string", "description": "HSN code (goods) or SAC code (services)", "default": ""},
                    "hsn_description": {"type": "string", "description": "Description of the HSN/SAC code (e.g. 'Steel', 'Software Services')", "default": ""},
                    "gst_rate": {
                        "type": "number",
                        "description": "Total GST rate % (e.g. 18 for 18% GST, 5 for 5%). IGST = gst_rate; CGST = SGST = gst_rate / 2.",
                        "default": 0,
                    },
                    "is_reverse_charge": {"type": "boolean", "description": "Mark as Reverse Charge Applicable (for RCM purchases)", "default": False},
                    "is_ineligible_itc": {"type": "boolean", "description": "Set True if ITC is ineligible (e.g. blocked credits under Section 17(5))", "default": False},
                    **TALLY_URL_PROP,
                },
                "required": ["name", "effective_date"],
            },
        ),
        types.Tool(
            name="create_duty_ledger",
            description=(
                "Create a GST Duty ledger (CGST, SGST/UTGST, IGST, or Cess) in TallyPrime under 'Duties & Taxes'. "
                "Use for output tax ledgers (e.g. 'CGST', 'SGST', 'IGST'), "
                "input tax ledgers (e.g. 'Input CGST', 'Input SGST', 'Input IGST'), "
                "and GST Cess ledgers. "
                "Set rate_of_tax (percentage of calculation) when a fixed rate applies — "
                "required for Cess, optional for CGST/SGST/IGST (usually left at 0 as rate "
                "is resolved dynamically from the voucher's purchase/sales ledger)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Ledger name (e.g. 'CGST', 'Output CGST', 'Input IGST', 'SGST Payable', 'GST Cess')",
                    },
                    "duty_head": {
                        "type": "string",
                        "description": "GST duty head that determines the tax type",
                        "enum": ["CGST", "SGST/UTGST", "IGST", "Cess"],
                    },
                    "parent": {
                        "type": "string",
                        "description": "Parent group (default: 'Duties & Taxes')",
                        "default": "Duties & Taxes",
                    },
                    "rate_of_tax": {
                        "type": "number",
                        "description": (
                            "Percentage of calculation — the fixed tax rate % for this ledger "
                            "(e.g. 9 for 9% CGST, 12 for 12% Cess). "
                            "Leave at 0 for CGST/SGST/IGST if rate varies by item (dynamic). "
                            "Maps to RATEOFTAXCALCULATION in Tally XML."
                        ),
                        "default": 0,
                    },
                    "cess_valuation_method": {
                        "type": "string",
                        "description": "Valuation method for Cess duty head only — 'Based on Value' or 'Based on Quantity'",
                        "enum": ["Based on Value", "Based on Quantity"],
                        "default": "Based on Value",
                    },
                    **TALLY_URL_PROP,
                },
                "required": ["name", "duty_head"],
            },
        ),
        types.Tool(
            name="create_roundoff_ledger",
            description=(
                "Create a Round-Off ledger in TallyPrime. "
                "Sets VATDEALERNATURE=Invoice Rounding, ROUNDINGMETHOD, and ROUNDINGLIMIT. "
                "No GST is applied. Parent is typically 'Indirect Incomes' or 'Indirect Expenses'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Ledger name (e.g. 'Round Off', 'Rounding Off')",
                    },
                    "parent": {
                        "type": "string",
                        "description": "Parent group — 'Indirect Incomes' (default) or 'Indirect Expenses'",
                        "default": "Indirect Incomes",
                    },
                    "rounding_method": {
                        "type": "string",
                        "description": "Rounding direction",
                        "enum": ["Normal Rounding", "Upward Rounding", "Downward Rounding"],
                        "default": "Normal Rounding",
                    },
                    "rounding_limit": {
                        "type": "number",
                        "description": "Maximum rounding amount (default: 1 — rounds to nearest rupee)",
                        "default": 1,
                    },
                    **TALLY_URL_PROP,
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="create_discount_ledger",
            description=(
                "Create a Discount ledger in TallyPrime (e.g. Discount Allowed, Discount Received, Trade Discount). "
                "No GST is stored on the ledger — GST on discounts is handled at voucher entry level in TallyPrime. "
                "Use parent='Indirect Expenses' for discount allowed/given, 'Indirect Incomes' for discount received."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Ledger name (e.g. 'Discount Allowed', 'Discount Received', 'Trade Discount')",
                    },
                    "parent": {
                        "type": "string",
                        "description": (
                            "Parent group — "
                            "'Indirect Expenses' (default, for discount allowed/given), "
                            "'Indirect Incomes' (for discount received), "
                            "'Discount' (if a Discount group exists in the company)"
                        ),
                        "default": "Indirect Expenses",
                    },
                    **TALLY_URL_PROP,
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="create_additional_ledger",
            description=(
                "Create an additional expense/income ledger in TallyPrime — "
                "handles two mutually exclusive types:\n"
                "• Transport & Freight style (include_in_assessable_value='Not Applicable'): "
                "GST is applicable; effective_date (mandatory), gst_rate, gst_nature_of_transaction, "
                "hsn_sac_code, gst_type_of_supply, is_reverse_charge, is_ineligible_itc are used.\n"
                "• Insurance style (include_in_assessable_value='GST'): "
                "GST is NOT directly applicable but is included in assessable value; "
                "appropriate_to and method_of_calculation apply instead of GST/HSN details. "
                "effective_date is NOT required in this mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Ledger name (e.g. 'Transport & Freight', 'Insurance', 'Freight Charges')",
                    },
                    "parent": {
                        "type": "string",
                        "description": "Parent group — 'Indirect Expenses' (default) or 'Indirect Incomes'",
                        "default": "Indirect Expenses",
                    },
                    "include_in_assessable_value": {
                        "type": "string",
                        "description": (
                            "Controls the ledger mode. "
                            "'Not Applicable' → GST applicable, HSN/rate details used (Transport & Freight style). "
                            "'GST' → included in assessable value, appropriate_to and method_of_calculation apply (Insurance style)."
                        ),
                        "enum": ["Not Applicable", "GST"],
                        "default": "Not Applicable",
                    },
                    # ── Mode A: Transport & Freight fields ──────────────────────
                    "effective_date": {
                        "type": "string",
                        "description": (
                            "[Mode: Not Applicable — mandatory] Date from which GST details are effective (APPLICABLEFROM). "
                            "Accepted formats: DD-MM-YYYY (e.g. '01-04-2025'), DD/MM/YYYY, "
                            "YYYY-MM-DD, or YYYYMMDD. Not used in Mode B (GST)."
                        ),
                        "default": "",
                    },
                    "gst_type_of_supply": {
                        "type": "string",
                        "description": "[Mode: Not Applicable] Type of supply — 'Services' (default) or 'Goods'",
                        "enum": ["Services", "Goods"],
                        "default": "Services",
                    },
                    "taxability": {
                        "type": "string",
                        "description": "[Mode: Not Applicable] GST taxability",
                        "enum": ["Taxable", "Exempt", "Nil Rated", "Non-GST"],
                        "default": "Taxable",
                    },
                    "gst_nature_of_transaction": {
                        "type": "string",
                        "description": (
                            "[Mode: Not Applicable] Nature of GST transaction "
                            "(e.g. 'Local Sales - Taxable', 'Interstate Sales - Taxable'). "
                            "Default: 'Local Sales - Taxable'"
                        ),
                        "default": "Local Sales - Taxable",
                    },
                    "hsn_sac_code": {
                        "type": "string",
                        "description": "[Mode: Not Applicable] HSN (goods) or SAC (services) code (e.g. '998234' for freight)",
                        "default": "",
                    },
                    "hsn_description": {
                        "type": "string",
                        "description": "[Mode: Not Applicable] Description for the HSN/SAC code (e.g. 'Freight')",
                        "default": "",
                    },
                    "gst_rate": {
                        "type": "number",
                        "description": (
                            "[Mode: Not Applicable] Total GST rate % (e.g. 18 for 18% GST). "
                            "IGST = gst_rate, CGST = SGST = gst_rate / 2"
                        ),
                        "default": 0,
                    },
                    "is_reverse_charge": {
                        "type": "boolean",
                        "description": "[Mode: Not Applicable] True → Reverse Charge Applicable",
                        "default": False,
                    },
                    "is_ineligible_itc": {
                        "type": "boolean",
                        "description": (
                            "[Mode: Not Applicable] True → ITC is NOT claimable (GSTINELIGIBLEITC=Yes). "
                            "Default True (as per Tally sample for transport/freight)"
                        ),
                        "default": True,
                    },
                    # ── Mode B: Insurance fields ─────────────────────────────────
                    "appropriate_to": {
                        "type": "string",
                        "description": "[Mode: GST] What the charge is appropriate to — 'Goods' (default) or 'Services'",
                        "enum": ["Goods", "Services"],
                        "default": "Goods",
                    },
                    "method_of_calculation": {
                        "type": "string",
                        "description": "[Mode: GST] How the assessable value addition is calculated",
                        "enum": ["Based on Value", "Based on Quantity"],
                        "default": "Based on Value",
                    },
                    **TALLY_URL_PROP,
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="get_all_groups",
            description="List all account groups (chart of accounts hierarchy) in TallyPrime.",
            inputSchema={
                "type": "object",
                "properties": {**TALLY_URL_PROP},
                "required": [],
            },
        ),

        # ── Vouchers ─────────────────────────────────────────────
        types.Tool(
            name="get_vouchers",
            description="Fetch vouchers from TallyPrime with optional filters by type, date range, or party name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "voucher_type": {
                        "type": "string",
                        "description": "Filter by voucher type (e.g. 'Sales', 'Purchase', 'Payment', 'Receipt', 'Journal')",
                        "default": "",
                    },
                    "from_date": {"type": "string", "description": "Start date in YYYYMMDD format (e.g. '20240401')", "default": ""},
                    "to_date": {"type": "string", "description": "End date in YYYYMMDD format (e.g. '20250331')", "default": ""},
                    "party_name": {"type": "string", "description": "Filter by party/ledger name", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
types.Tool(
            name="create_sales_voucher",
            description=(
                "Create a Sales (invoice) voucher in TallyPrime with a single line item. "
                "Pass item details as flat fields: stock_item_name, sales_ledger, quantity, unit, item_rate, amount. "
                "GST fields are separate: gst_rate (item-level GST %), plus voucher-level GST ledgers — "
                "cgst_ledger + cgst_amount + sgst_ledger + sgst_amount (intrastate) "
                "OR igst_ledger + igst_amount (interstate). "
                "For additional charges/deductions (Freight, Insurance, Discount, Round-off) use "
                "additional_ledgers array: ledger_name, amount, is_addition (true=charge, false=deduction). "
                "Party debit = net item amount + additions - deductions + GST. "
                "For multi-item invoices, call this tool once per line item. "
                "ALWAYS confirm details with the user before calling this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Voucher date in YYYYMMDD format"},
                    "party_ledger": {"type": "string", "description": "Customer ledger name as in TallyPrime"},
                    "voucher_type": {"type": "string", "description": "Voucher type name as configured in TallyPrime, e.g. 'Sales', 'Tax Invoice'", "default": "Sales"},
                    "voucher_number": {"type": "string", "description": "Invoice / voucher number (optional)", "default": ""},
                    "narration": {"type": "string", "description": "Narration or remarks (optional)", "default": ""},
                    # ── Single line-item fields (flat) ─────────────────────
                    "stock_item_name": {"type": "string", "description": "Product/stock item name exactly as in TallyPrime"},
                    "sales_ledger": {"type": "string", "description": "Income/sales ledger to credit for this item"},
                    "quantity": {"type": "number", "description": "Item quantity"},
                    "unit": {"type": "string", "description": "Unit of measure, e.g. 'Nos', 'Kg', 'Ltrs'"},
                    "item_rate": {"type": "number", "description": "Price per unit"},
                    "amount": {"type": "number", "description": "Net line amount (post-discount, pre-tax)"},
                    # ── GST fields (separate) ──────────────────────────────
                    "gst_rate": {"type": "number", "description": "GST percentage for this item, e.g. 5, 12, 18, 28 (0 if exempt)", "default": 0},
                    "cgst_ledger": {"type": "string", "description": "CGST output tax ledger name (intrastate)", "default": ""},
                    "cgst_amount": {"type": "number", "description": "Total CGST tax amount for the invoice", "default": 0},
                    "sgst_ledger": {"type": "string", "description": "SGST/UTGST output tax ledger name (intrastate)", "default": ""},
                    "sgst_amount": {"type": "number", "description": "Total SGST/UTGST tax amount for the invoice", "default": 0},
                    "igst_ledger": {"type": "string", "description": "IGST output tax ledger name (interstate)", "default": ""},
                    "igst_amount": {"type": "number", "description": "Total IGST tax amount for the invoice", "default": 0},
                    # ── Additional ledgers ─────────────────────────────────
                    "additional_ledgers": {
                        "description": (
                            "JSON array of voucher-level ledger entries added after inventory lines. "
                            "Each object must have: "
                            "ledger_name (string, exact name in TallyPrime e.g. 'Freight Charges', 'Trade Discount'), "
                            "amount (number, always positive), "
                            "is_addition (boolean: true = charge added to bill e.g. Freight/Insurance; "
                            "false = deduction from bill e.g. Discount/Round-off). "
                            "Omit this field or pass an empty array if there are no additional ledgers."
                        ),
                        "default": [],
                    },
                    # ── GST header fields ──────────────────────────────────
                    "gst_registration_type": {"type": "string", "description": "Party GST registration type, e.g. 'Regular', 'Unregistered'", "default": ""},
                    "party_gstin": {"type": "string", "description": "Party GSTIN number (optional)", "default": ""},
                    "place_of_supply": {"type": "string", "description": "Place of supply state name (optional)", "default": ""},
                    "state_name": {"type": "string", "description": "Party state name (optional)", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": ["date", "party_ledger", "voucher_type", "stock_item_name", "sales_ledger", "quantity", "unit", "item_rate", "amount"],
            },
        ),
        types.Tool(
            name="create_purchase_voucher",
            description=(
                "Create a Purchase (invoice) voucher in TallyPrime using Item Invoice mode. "
                "Supports multiple inventory lines, each with its own purchase/expense ledger. "
                "Always ask for: date, party_ledger, voucher_type, and line_items array. "
                "Each entry in line_items needs: stock_item_name, purchase_ledger, amount (net amount, always positive), "
                "rate, quantity, unit, gst_rate (per-line GST %, e.g. 5, 12). "
                "Item-level discount: discount_percent (%) OR discount_amount (fixed, always positive). "
                "GST ledgers are voucher-level (input tax credit): cgst_ledger + cgst_amount + sgst_ledger + sgst_amount (intrastate) "
                "OR igst_ledger + igst_amount (interstate). "
                "For additional charges/deductions (Freight, Discount Received) use additional_ledgers array. "
                "reference: supplier's invoice number — creates a bill reference in TallyPrime for payables tracking."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Voucher date in YYYYMMDD format"},
                    "party_ledger": {"type": "string", "description": "Supplier/vendor ledger name as in TallyPrime"},
                    "voucher_type": {"type": "string", "description": "Voucher type name as configured in TallyPrime, e.g. 'Purchase'", "default": "Purchase"},
                    "voucher_number": {"type": "string", "description": "Internal voucher/bill number (optional)", "default": ""},
                    "reference": {"type": "string", "description": "Supplier's invoice/bill number — used for payables bill reference in TallyPrime (optional)", "default": ""},
                    "narration": {"type": "string", "description": "Narration or remarks (optional)", "default": ""},
                    "line_items": {
                        "description": (
                            "JSON array of inventory line objects (pass as a real array, not a string). "
                            "All values are mapped directly to the Tally voucher XML — no recomputation. "
                            "Each object must have: "
                            "stock_item_name (string, exact name in TallyPrime), "
                            "purchase_ledger (string, purchase/expense ledger to debit for this line), "
                            "amount (number, NET line amount after any discount — always positive, code negates for XML), "
                            "rate (number, rate per unit), "
                            "quantity (number), "
                            "unit (string, e.g. 'Nos', 'Bag', 'Kg'), "
                            "gst_rate (number, GST % for this line e.g. 5, 12, 18, 28). "
                            "Optional discount fields (always pass as positive): "
                            "discount_percent → <DISCOUNT>, discount_amount → <DISCOUNTAMOUNT> (negated in XML)."
                        ),
                        "default": [],
                    },
                    "additional_ledgers": {
                        "description": (
                            "JSON array of voucher-level ledger entries added after inventory lines. "
                            "Each object must have: "
                            "ledger_name (string, exact name in TallyPrime e.g. 'Freight Inward', 'Discount Received'), "
                            "amount (number, always positive), "
                            "is_addition (boolean: true = charge added to bill e.g. Freight; "
                            "false = deduction from bill e.g. Discount Received). "
                            "Omit this field or pass an empty array if there are no additional ledgers."
                        ),
                        "default": [],
                    },
                    "cgst_ledger": {"type": "string", "description": "CGST input tax ledger name (intrastate)", "default": ""},
                    "cgst_amount": {"type": "number", "description": "Total CGST input tax amount (always positive)", "default": 0},
                    "sgst_ledger": {"type": "string", "description": "SGST/UTGST input tax ledger name (intrastate)", "default": ""},
                    "sgst_amount": {"type": "number", "description": "Total SGST/UTGST input tax amount (always positive)", "default": 0},
                    "igst_ledger": {"type": "string", "description": "IGST input tax ledger name (interstate)", "default": ""},
                    "igst_amount": {"type": "number", "description": "Total IGST input tax amount (always positive)", "default": 0},
                    "gst_registration_type": {"type": "string", "description": "Supplier GST registration type, e.g. 'Regular', 'Unregistered'", "default": ""},
                    "party_gstin": {"type": "string", "description": "Supplier GSTIN number (optional)", "default": ""},
                    "place_of_supply": {"type": "string", "description": "Place of supply state name (optional)", "default": ""},
                    "state_name": {"type": "string", "description": "Supplier state name (optional)", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": ["date", "party_ledger", "voucher_type", "line_items"],
            },
        ),
        types.Tool(
            name="create_payment_voucher",
            description="Create a Payment voucher in TallyPrime. Debits the party and credits the bank/cash ledger.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Voucher date in YYYYMMDD format"},
                    "party_ledger": {"type": "string", "description": "Payee ledger name"},
                    "bank_or_cash_ledger": {"type": "string", "description": "Bank or Cash ledger (e.g. 'State Bank of India', 'Cash')"},
                    "amount": {"type": "number", "description": "Payment amount"},
                    "voucher_number": {"type": "string", "description": "Voucher number (optional)", "default": ""},
                    "narration": {"type": "string", "description": "Narration/remarks", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": ["date", "party_ledger", "bank_or_cash_ledger", "amount"],
            },
        ),
        types.Tool(
            name="create_receipt_voucher",
            description="Create a Receipt voucher in TallyPrime. Debits the bank/cash and credits the party (payer).",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Voucher date in YYYYMMDD format"},
                    "party_ledger": {"type": "string", "description": "Payer ledger name"},
                    "bank_or_cash_ledger": {"type": "string", "description": "Bank or Cash ledger receiving the amount"},
                    "amount": {"type": "number", "description": "Receipt amount"},
                    "voucher_number": {"type": "string", "description": "Voucher number (optional)", "default": ""},
                    "narration": {"type": "string", "description": "Narration/remarks", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": ["date", "party_ledger", "bank_or_cash_ledger", "amount"],
            },
        ),
        types.Tool(
            name="create_journal_voucher",
            description="Create a Journal voucher with custom debit/credit entries. Useful for adjustments, provisions, depreciation, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Voucher date in YYYYMMDD format"},
                    "entries": {
                        "description": (
                            "JSON array of ledger entry objects. Total debits must equal total credits. "
                            "Each object must have: ledger (string, ledger name), "
                            "amount (number, always positive), "
                            "is_debit (boolean: true = Debit, false = Credit)."
                        ),
                    },
                    "voucher_number": {"type": "string", "description": "Voucher number (optional)", "default": ""},
                    "narration": {"type": "string", "description": "Narration/remarks", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": ["date", "entries"],
            },
        ),

        # ── Debug ─────────────────────────────────────────────────
        types.Tool(
            name="debug_raw_xml",
            description=(
                "Send any raw XML request to TallyPrime and get the raw XML response back. "
                "Use this to diagnose empty fields, inspect TallyPrime's response structure, "
                "or test custom TDL queries. Returns the full unprocessed XML string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "request_xml": {
                        "type": "string",
                        "description": "The complete XML envelope to send to TallyPrime Gateway",
                    },
                    **TALLY_URL_PROP,
                },
                "required": ["request_xml"],
            },
        ),

        # ── Reports ───────────────────────────────────────────────
        types.Tool(
            name="get_trial_balance",
            description="Fetch the Trial Balance from TallyPrime using Tally's built-in report engine. Returns an ordered list of account/group entries with closing_dr, closing_cr and optionally opening_dr, opening_cr. Supports date range filtering via from_date/to_date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date in YYYYMMDD format", "default": ""},
                    "to_date": {"type": "string", "description": "End date in YYYYMMDD format", "default": ""},
                    "include_opening": {
                        "type": "boolean",
                        "description": "Include opening balance columns (opening_dr, opening_cr). Default true. Set false for closing-only view.",
                        "default": True,
                    },
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_balance_sheet",
            description="Fetch the Balance Sheet from TallyPrime using Tally's built-in report engine. Returns an ordered flat list of entries (groups and ledgers) with name, sub_amount (individual ledger), and main_amount (group total), matching Tally's on-screen Balance Sheet sequence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date in YYYYMMDD format", "default": ""},
                    "to_date": {"type": "string", "description": "End date in YYYYMMDD format", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_profit_loss",
            description="Fetch the Profit & Loss statement from TallyPrime using Tally's built-in report engine. Returns an ordered flat list of entries (groups and ledgers) with name, sub_amount (individual ledger), and main_amount (group total), matching Tally's on-screen P&L sequence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date in YYYYMMDD format (e.g. '20240401')", "default": ""},
                    "to_date": {"type": "string", "description": "End date in YYYYMMDD format (e.g. '20250331')", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_stock_summary",
            description="Fetch the Stock Summary from TallyPrime showing all stock items with quantities and values.",
            inputSchema={
                "type": "object",
                "properties": {**TALLY_URL_PROP},
                "required": [],
            },
        ),
        types.Tool(
            name="get_daybook",
            description="Fetch the Daybook (all vouchers in chronological order) from TallyPrime for a given date range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date in YYYYMMDD format", "default": ""},
                    "to_date": {"type": "string", "description": "End date in YYYYMMDD format", "default": ""},
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_outstanding_receivables",
            description=(
                "Fetch the Ledger-wise Bill-wise Outstanding Receivables report from TallyPrime. "
                "Mirrors the Bills Receivable / Bills Outstanding report as exported from Tally UI to Excel. "
                "Returns all pending bills grouped by party (ledger-wise), matching the Excel export structure. "
                "Each bill includes: bill_ref, bill_date, due_date, opening (original bill amount), "
                "outstanding (pending amount after partial payments), and days_overdue. "
                "Each party entry also shows its opening and outstanding totals, and bill_count. "
                "Top-level response includes: total_opening (grand total of original bill amounts), "
                "total_outstanding (grand total pending), party_summary (sorted highest outstanding first), "
                "aging breakdown (current/not-due, 1-30, 31-60, 61-90, 90+ days overdue), and party/bill counts. "
                "party_name is optional — omit to get all parties. "
                "ledger_group defaults to 'Sundry Debtors' — override if your debtors are under a different Tally group."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": (
                            "Optional start date of the reporting period. "
                            "Accepted formats: DD-MM-YYYY (e.g. '01-04-2025'), DD/MM/YYYY, "
                            "YYYY-MM-DD, or YYYYMMDD. "
                            "Defaults to the company's financial-year start if omitted."
                        ),
                        "default": "",
                    },
                    "as_of_date": {
                        "type": "string",
                        "description": (
                            "End / as-of date for the report. "
                            "Accepted formats: DD-MM-YYYY (e.g. '16-03-2026'), DD/MM/YYYY, "
                            "YYYY-MM-DD, or YYYYMMDD. Defaults to today if omitted."
                        ),
                        "default": "",
                    },
                    "party_name": {
                        "type": "string",
                        "description": (
                            "Optional: filter results to parties whose name contains this string "
                            "(case-insensitive). Leave empty (or omit) to get all parties."
                        ),
                        "default": "",
                    },
                    "ledger_group": {
                        "type": "string",
                        "description": (
                            "Tally ledger group that contains your debtors. "
                            "Defaults to 'Sundry Debtors'. Override if your company uses a "
                            "different group name (e.g. 'Trade Receivables')."
                        ),
                        "default": "Sundry Debtors",
                    },
                    **TALLY_URL_PROP,
                },
                "required": [],
            },
        ),
    ]


# ═══════════════════════════════════════════════════════════════════
# TOOL DISPATCH
# ═══════════════════════════════════════════════════════════════════

def _ok(data: Any) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))]


def _err(message: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps({"error": message}))]


def _parse_array_arg(val: Any) -> list:
    """
    Normalise an array tool argument.
    Some MCP clients send arrays as JSON strings (sometimes with a leading 'key:' prefix).
    This function accepts either a real list or such a string and always returns a list.
    """
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        s = val.strip()
        bracket = s.find("[")   # skip any leading '"key":' prefix
        if bracket >= 0:
            s = s[bracket:]
        return json.loads(s) if s else []
    return val or []


def _url(arguments: dict[str, Any]) -> str | None:
    """Extract optional tally_url from tool arguments."""
    return arguments.get("tally_url") or None


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    logger.info("Tool called: %s  args=%s", name, {k: v for k, v in arguments.items() if k != "tally_url"})
    try:
        match name:

            # ── Company ────────────────────────────────────────
            case "get_active_company":
                return _ok(tc.get_active_company(tally_url=_url(arguments)))

            # ── Ledgers ────────────────────────────────────────
            case "get_all_ledgers":
                return _ok(tc.fetch_all_ledgers(tally_url=_url(arguments)))

            case "get_ledger":
                return _ok(tc.fetch_ledger(arguments["name"], tally_url=_url(arguments)))

            case "create_party_ledger":
                return _ok(tc.create_party_ledger(
                    name=arguments["name"],
                    parent=arguments["parent"],
                    opening_balance=float(arguments.get("opening_balance", 0)),
                    gstin=arguments.get("gstin", ""),
                    gst_registration_type=arguments.get("gst_registration_type", "Regular"),
                    address=arguments.get("address", ""),
                    state=arguments.get("state", ""),
                    country=arguments.get("country", "India"),
                    pincode=arguments.get("pincode", ""),
                    phone=arguments.get("phone", ""),
                    email=arguments.get("email", ""),
                    credit_period=arguments.get("credit_period", ""),
                    credit_limit=float(arguments.get("credit_limit", 0)),
                    tally_url=_url(arguments),
                ))

            case "create_sales_ledger":
                return _ok(tc.create_sales_ledger(
                    name=arguments["name"],
                    effective_date=arguments["effective_date"],
                    parent=arguments.get("parent", "Sales Accounts"),
                    gst_type_of_supply=arguments.get("gst_type_of_supply", "Goods"),
                    taxability=arguments.get("taxability", "Taxable"),
                    gst_nature_of_transaction=arguments.get("gst_nature_of_transaction", ""),
                    hsn_sac_code=arguments.get("hsn_sac_code", ""),
                    hsn_description=arguments.get("hsn_description", ""),
                    gst_rate=float(arguments.get("gst_rate", 0)),
                    is_reverse_charge=bool(arguments.get("is_reverse_charge", False)),
                    tally_url=_url(arguments),
                ))

            case "create_purchase_ledger":
                return _ok(tc.create_purchase_ledger(
                    name=arguments["name"],
                    effective_date=arguments["effective_date"],
                    parent=arguments.get("parent", "Purchase Accounts"),
                    gst_type_of_supply=arguments.get("gst_type_of_supply", "Goods"),
                    taxability=arguments.get("taxability", "Taxable"),
                    gst_nature_of_transaction=arguments.get("gst_nature_of_transaction", "Interstate Purchase - Taxable"),
                    hsn_sac_code=arguments.get("hsn_sac_code", ""),
                    hsn_description=arguments.get("hsn_description", ""),
                    gst_rate=float(arguments.get("gst_rate", 0)),
                    is_reverse_charge=bool(arguments.get("is_reverse_charge", False)),
                    is_ineligible_itc=bool(arguments.get("is_ineligible_itc", False)),
                    tally_url=_url(arguments),
                ))

            case "create_duty_ledger":
                return _ok(tc.create_duty_ledger(
                    name=arguments["name"],
                    duty_head=arguments["duty_head"],
                    parent=arguments.get("parent", "Duties & Taxes"),
                    rate_of_tax=float(arguments.get("rate_of_tax", 0)),
                    cess_valuation_method=arguments.get("cess_valuation_method", "Based on Value"),
                    tally_url=_url(arguments),
                ))

            case "create_roundoff_ledger":
                return _ok(tc.create_roundoff_ledger(
                    name=arguments["name"],
                    parent=arguments.get("parent", "Indirect Incomes"),
                    rounding_method=arguments.get("rounding_method", "Normal Rounding"),
                    rounding_limit=float(arguments.get("rounding_limit", 1.0)),
                    tally_url=_url(arguments),
                ))

            case "create_discount_ledger":
                return _ok(tc.create_discount_ledger(
                    name=arguments["name"],
                    parent=arguments.get("parent", "Indirect Expenses"),
                    tally_url=_url(arguments),
                ))

            case "create_additional_ledger":
                return _ok(tc.create_additional_ledger(
                    name=arguments["name"],
                    parent=arguments.get("parent", "Indirect Expenses"),
                    include_in_assessable_value=arguments.get("include_in_assessable_value", "Not Applicable"),
                    effective_date=arguments.get("effective_date", ""),
                    gst_type_of_supply=arguments.get("gst_type_of_supply", "Services"),
                    taxability=arguments.get("taxability", "Taxable"),
                    gst_nature_of_transaction=arguments.get("gst_nature_of_transaction", "Local Sales - Taxable"),
                    hsn_sac_code=arguments.get("hsn_sac_code", ""),
                    hsn_description=arguments.get("hsn_description", ""),
                    gst_rate=float(arguments.get("gst_rate", 0)),
                    is_reverse_charge=bool(arguments.get("is_reverse_charge", False)),
                    is_ineligible_itc=bool(arguments.get("is_ineligible_itc", True)),
                    appropriate_to=arguments.get("appropriate_to", "Goods"),
                    method_of_calculation=arguments.get("method_of_calculation", "Based on Value"),
                    tally_url=_url(arguments),
                ))

            case "get_all_groups":
                return _ok(tc.fetch_all_groups(tally_url=_url(arguments)))

            # ── Vouchers ───────────────────────────────────────
            case "get_vouchers":
                return _ok(tc.fetch_vouchers(
                    voucher_type=arguments.get("voucher_type", ""),
                    from_date=arguments.get("from_date", ""),
                    to_date=arguments.get("to_date", ""),
                    party_name=arguments.get("party_name", ""),
                    tally_url=_url(arguments),
                ))

           case "create_sales_voucher":
                raw_extra = _parse_array_arg(arguments.get("additional_ledgers", []))
                return _ok(tc.create_sales_voucher(
                    date=arguments["date"],
                    party_ledger=arguments["party_ledger"],
                    stock_item_name=arguments.get("stock_item_name", ""),
                    sales_ledger=arguments.get("sales_ledger", ""),
                    quantity=float(arguments.get("quantity", 0)),
                    unit=arguments.get("unit", ""),
                    item_rate=float(arguments.get("item_rate", 0)),
                    amount=float(arguments.get("amount", 0)),
                    gst_rate=float(arguments.get("gst_rate", 0)),
                    cgst_ledger=arguments.get("cgst_ledger", ""),
                    cgst_amount=float(arguments.get("cgst_amount", 0)),
                    sgst_ledger=arguments.get("sgst_ledger", ""),
                    sgst_amount=float(arguments.get("sgst_amount", 0)),
                    igst_ledger=arguments.get("igst_ledger", ""),
                    igst_amount=float(arguments.get("igst_amount", 0)),
                    voucher_type=arguments.get("voucher_type", "Sales"),
                    voucher_number=arguments.get("voucher_number", ""),
                    narration=arguments.get("narration", ""),
                    additional_ledgers=raw_extra or None,
                    gst_registration_type=arguments.get("gst_registration_type", ""),
                    party_gstin=arguments.get("party_gstin", ""),
                    place_of_supply=arguments.get("place_of_supply", ""),
                    state_name=arguments.get("state_name", ""),
                    tally_url=_url(arguments),
                ))

            case "create_purchase_voucher":
                # Normalise line_items / additional_ledgers: same as sales — some MCP
                # clients serialise arrays as JSON strings (sometimes with a key prefix).
                raw_items = _parse_array_arg(arguments.get("line_items", []))
                raw_extra = _parse_array_arg(arguments.get("additional_ledgers", []))
                return _ok(tc.create_purchase_voucher(
                    date=arguments["date"],
                    party_ledger=arguments["party_ledger"],
                    voucher_type=arguments.get("voucher_type", "Purchase"),
                    voucher_number=arguments.get("voucher_number", ""),
                    reference=arguments.get("reference", ""),
                    narration=arguments.get("narration", ""),
                    items=raw_items,
                    additional_ledgers=raw_extra or None,
                    cgst_ledger=arguments.get("cgst_ledger", ""),
                    cgst_amount=float(arguments.get("cgst_amount", 0)),
                    sgst_ledger=arguments.get("sgst_ledger", ""),
                    sgst_amount=float(arguments.get("sgst_amount", 0)),
                    igst_ledger=arguments.get("igst_ledger", ""),
                    igst_amount=float(arguments.get("igst_amount", 0)),
                    gst_registration_type=arguments.get("gst_registration_type", ""),
                    party_gstin=arguments.get("party_gstin", ""),
                    place_of_supply=arguments.get("place_of_supply", ""),
                    state_name=arguments.get("state_name", ""),
                    tally_url=_url(arguments),
                ))

            case "create_payment_voucher":
                return _ok(tc.create_payment_voucher(
                    date=arguments["date"],
                    party_ledger=arguments["party_ledger"],
                    bank_or_cash_ledger=arguments["bank_or_cash_ledger"],
                    amount=float(arguments["amount"]),
                    voucher_number=arguments.get("voucher_number", ""),
                    narration=arguments.get("narration", ""),
                    tally_url=_url(arguments),
                ))

            case "create_receipt_voucher":
                return _ok(tc.create_receipt_voucher(
                    date=arguments["date"],
                    party_ledger=arguments["party_ledger"],
                    bank_or_cash_ledger=arguments["bank_or_cash_ledger"],
                    amount=float(arguments["amount"]),
                    voucher_number=arguments.get("voucher_number", ""),
                    narration=arguments.get("narration", ""),
                    tally_url=_url(arguments),
                ))

            case "create_journal_voucher":
                raw_entries = _parse_array_arg(arguments.get("entries", []))
                return _ok(tc.create_journal_voucher(
                    date=arguments["date"],
                    entries=raw_entries,
                    voucher_number=arguments.get("voucher_number", ""),
                    narration=arguments.get("narration", ""),
                    tally_url=_url(arguments),
                ))

            # ── Debug ──────────────────────────────────────────
            case "debug_raw_xml":
                return _ok(tc.debug_raw_xml(
                    request_xml=arguments["request_xml"],
                    tally_url=_url(arguments),
                ))

            # ── Reports ────────────────────────────────────────
            case "get_trial_balance":
                return _ok(tc.fetch_trial_balance(
                    from_date=arguments.get("from_date", ""),
                    to_date=arguments.get("to_date", ""),
                    include_opening=bool(arguments.get("include_opening", True)),
                    tally_url=_url(arguments),
                ))

            case "get_balance_sheet":
                return _ok(tc.fetch_balance_sheet(
                    from_date=arguments.get("from_date", ""),
                    to_date=arguments.get("to_date", ""),
                    tally_url=_url(arguments),
                ))

            case "get_profit_loss":
                return _ok(tc.fetch_profit_loss(
                    from_date=arguments.get("from_date", ""),
                    to_date=arguments.get("to_date", ""),
                    tally_url=_url(arguments),
                ))

            case "get_stock_summary":
                return _ok(tc.fetch_stock_summary(tally_url=_url(arguments)))

            case "get_daybook":
                return _ok(tc.fetch_daybook(
                    from_date=arguments.get("from_date", ""),
                    to_date=arguments.get("to_date", ""),
                    tally_url=_url(arguments),
                ))

            case "get_outstanding_receivables":
                return _ok(tc.fetch_outstanding_receivables(
                    from_date=arguments.get("from_date", ""),
                    as_of_date=arguments.get("as_of_date", ""),
                    party_name=arguments.get("party_name", ""),
                    ledger_group=arguments.get("ledger_group", "Sundry Debtors"),
                    tally_url=_url(arguments),
                ))

            case _:
                return _err(f"Unknown tool: {name}")

    except Exception as exc:
        logger.exception("Tool %s raised an exception", name)
        return _err(str(exc))


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
