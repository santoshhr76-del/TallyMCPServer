"""
TallyPrime MCP Server — HTTP/SSE Transport
For cloud deployment (Railway, Render, AWS, GCP, etc.)

Exposes these endpoints:
  GET  /sse          — MCP Server-Sent Events stream (clients connect here)
  POST /messages     — MCP message inbox
  GET  /health       — Health check (no auth)
  GET  /app          — TallyPrime PWA chat interface (no auth)
  POST /chat         — AI chat endpoint: Claude + TallyPrime tools (Bearer auth)

Environment variables:
  TALLY_URL          URL of TallyPrime Gateway Server (default: http://localhost:9000)
  TALLY_TIMEOUT      HTTP timeout in seconds (default: 30)
  MCP_HOST           Server bind host (default: 0.0.0.0)
  MCP_PORT           Server bind port (default: 8000)
  MCP_API_KEY        Optional bearer token for request authentication
  ANTHROPIC_API_KEY  Anthropic API key for the /chat endpoint
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

# Load .env from the repo root (or any parent directory) before reading
# os.environ values below. python-dotenv is optional; the server still runs
# if it's missing, just without auto-loading .env.
try:
    from dotenv import load_dotenv
    # Look for .env starting from this file's directory and walking up.
    _dotenv_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path)
    else:
        load_dotenv()  # default search
except ImportError:
    pass

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .server import app as mcp_app  # re-use the same MCP server with all tools

logger = logging.getLogger(__name__)

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8000")))
API_KEY = os.environ.get("MCP_API_KEY", "")


# ─────────────────────────────────────────────────────────────────
# Optional API-key auth middleware
# ─────────────────────────────────────────────────────────────────

class ApiKeyMiddleware:
    """Reject requests that lack the correct Bearer token (when API_KEY is set).

    Implemented as a pure ASGI middleware (NOT BaseHTTPMiddleware) so that
    streaming responses such as SSE are never buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not API_KEY:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Public paths — no auth required
        if path in ("/health", "/", "/app", "/install", "/install-qr.png", "/install-qr.svg"):
            await self.app(scope, receive, send)
            return

        # Check Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {API_KEY}":
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# ─────────────────────────────────────────────────────────────────
# SSE transport wiring
# ─────────────────────────────────────────────────────────────────

sse_transport = SseServerTransport("/messages")


async def handle_sse(scope: Scope, receive: Receive, send: Send) -> None:
    """SSE endpoint — raw ASGI handler so Starlette never wraps the response.

    Using Mount + raw ASGI avoids both:
      • TypeError  ('NoneType' not callable) from returning None to Route, and
      • RuntimeError (double http.response.start) from returning Response() after
        connect_sse has already sent SSE headers via `send`.
    """
    async with sse_transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
        await mcp_app.run(
            read_stream,
            write_stream,
            mcp_app.create_initialization_options(),
        )


# ─────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────

async def health(request: Request):
    from . import tally_client as tc
    return JSONResponse({
        "status": "ok",
        "tally_url": tc.DEFAULT_TALLY_URL,
        "version": "0.1.0",
    })


# ─────────────────────────────────────────────────────────────────
# Chat endpoint — Claude + TallyPrime tools
# ─────────────────────────────────────────────────────────────────

TALLY_TOOLS = [
    {
        "name": "get_active_company",
        "description": "Get the currently active TallyPrime company: name, financial year, GSTIN, address, currency.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_all_ledgers",
        "description": "List all ledgers in TallyPrime with parent group, opening balance, and closing balance.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_ledger",
        "description": "Get full details of a specific ledger: GSTIN, PAN, contact person name, phone, email, address, credit terms, bill-wise settings, opening/closing balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact ledger name as it appears in TallyPrime"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_vouchers",
        "description": "Fetch vouchers (Sales, Purchase, Payment, Receipt, Journal) with optional date range and party filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date in YYYYMMDD format"},
                "to_date":   {"type": "string", "description": "End date in YYYYMMDD format"},
                "voucher_type": {"type": "string", "description": "Filter: Sales, Purchase, Payment, Receipt, Journal. Empty = all."},
                "party_name":   {"type": "string", "description": "Filter by party/ledger name"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_trial_balance",
        "description": "Get trial balance for a date range showing debit/credit totals for every ledger.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date YYYYMMDD"},
                "to_date":   {"type": "string", "description": "End date YYYYMMDD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_balance_sheet",
        "description": "Get balance sheet as of a specific date showing assets and liabilities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {"type": "string", "description": "Date in YYYYMMDD format"},
            },
            "required": ["as_of_date"],
        },
    },
    {
        "name": "get_profit_loss",
        "description": "Get profit and loss statement for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date YYYYMMDD"},
                "to_date":   {"type": "string", "description": "End date YYYYMMDD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_daybook",
        "description": "Get day book: all voucher entries posted within a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date YYYYMMDD"},
                "to_date":   {"type": "string", "description": "End date YYYYMMDD"},
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_outstanding_receivables",
        "description": "Get outstanding receivables (money owed to the company) with bill-wise aging.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ledger_group": {"type": "string", "description": "Ledger group to scan (default: Sundry Debtors)"},
                "as_of_date":   {"type": "string", "description": "Aging as-of date YYYYMMDD (default: today)"},
                "party_name":   {"type": "string", "description": "Filter to a specific party"},
            },
            "required": [],
        },
    },
    {
        "name": "create_simple_unit",
        "description": (
            "Create a new simple Unit of Measure in TallyPrime (e.g. Box, Kg, Nos, Ltrs). "
            "Provide the short symbol and optionally the full formal name. "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unit symbol / short name (e.g. 'Box', 'Kg', 'Nos', 'Pcs')"},
                "original_name": {"type": "string", "description": "Full formal name (e.g. 'Boxes', 'Kilograms', 'Numbers'). Defaults to name if empty.", "default": ""},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_unit",
        "description": "Get details of a specific unit of measure by name. Useful to check if a unit exists before creating stock items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact unit name as it appears in TallyPrime (e.g. 'Nos', 'Kg', 'Box')"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_all_units",
        "description": "List all simple (non-compound) units of measure in the active TallyPrime company with their name and formal name.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_compound_unit",
        "description": (
            "Create a new compound Unit of Measure in TallyPrime that relates two simple units "
            "via a conversion factor (e.g. 1 Kg = 1000 gm, 1 Dozen = 12 Nos). "
            "Both base and additional simple units must already exist in TallyPrime. "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Compound unit name (e.g. 'Kg of 1000 gm', 'Dozen of 12 Nos')"},
                "base_units": {"type": "string", "description": "Primary/base unit symbol (e.g. 'Kg', 'Dozen')"},
                "additional_units": {"type": "string", "description": "Secondary unit symbol (e.g. 'gm', 'Nos')"},
                "conversion": {"type": "integer", "description": "How many additional_units make 1 base_unit (e.g. 1000, 12)"},
            },
            "required": ["name", "base_units", "additional_units", "conversion"],
        },
    },
    {
        "name": "get_all_stock_groups",
        "description": "List all stock groups in the active TallyPrime company with their name and parent group.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_stock_group",
        "description": "Get full details of a specific stock group by name: parent group, opening balance, and closing balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact stock group name as it appears in TallyPrime"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_stock_group",
        "description": (
            "Create a new Stock Group in TallyPrime. "
            "Optionally specify a parent group for nesting (e.g. create 'Green Tea' under 'Tea Products'). "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Stock group name (e.g. 'Tea Products', 'Electronics')"},
                "parent": {"type": "string", "description": "Parent stock group for nesting (leave empty for top-level)", "default": ""},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_stock_items_of_group",
        "description": (
            "List all stock items belonging to a specific stock group in TallyPrime. "
            "Returns each item's name, parent group, base unit, closing balance, and closing value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_name": {"type": "string", "description": "Exact stock group name as it appears in TallyPrime (e.g. 'Gadgets', 'Raw Materials')"},
            },
            "required": ["group_name"],
        },
    },
    {
        "name": "get_all_stock_items",
        "description": "List all stock items in the active TallyPrime company with their name and parent stock group.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_stock_item",
        "description": "Get full details of a specific stock item by name: parent group, base unit, and closing balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact stock item name as it appears in TallyPrime"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_stock_item",
        "description": (
            "Create a new Stock Item in TallyPrime. "
            "Specify the item name, stock group (parent), and base unit of measure. "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Stock item name (e.g. 'Tea Powder', 'Sugar 1kg')"},
                "parent": {"type": "string", "description": "Stock group / parent group (default: Primary)", "default": "Primary"},
                "base_units": {"type": "string", "description": "Unit of measure (e.g. 'nos', 'kg', 'pcs', 'ltrs')", "default": "nos"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_sales_voucher",
        "description": (
            "Create a Sales invoice in TallyPrime with multiple inventory line items. "
            "Pass items as a JSON array in line_items: each needs stock_item_name, sales_ledger, "
            "quantity, unit, rate, amount (net, post-discount, pre-tax), gst_rate (per-line GST %). "
            "Optional per-item: hsn, godown, discount_percent, discount_amount. "
            "GST tax ledgers (voucher-level): cgst_ledger+cgst_amount+sgst_ledger+sgst_amount (intrastate) "
            "OR igst_ledger+igst_amount (interstate). "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":            {"type": "string",  "description": "Invoice date YYYYMMDD"},
                "party_ledger":    {"type": "string",  "description": "Customer ledger name"},
                "voucher_type":    {"type": "string",  "description": "Voucher type e.g. Sales, Tax Invoice", "default": "Sales"},
                "voucher_number":  {"type": "string",  "description": "Invoice number (optional)", "default": ""},
                "narration":       {"type": "string",  "default": ""},
                "line_items":      {"type": "array",   "items": {"type": "object"}, "description": "Array of inventory line items", "default": []},
                "cgst_ledger":     {"type": "string",  "default": ""},
                "cgst_amount":     {"type": "number",  "default": 0},
                "sgst_ledger":     {"type": "string",  "default": ""},
                "sgst_amount":     {"type": "number",  "default": 0},
                "igst_ledger":     {"type": "string",  "default": ""},
                "igst_amount":     {"type": "number",  "default": 0},
                "additional_ledgers": {"type": "array", "items": {"type": "object"}, "default": []},
                "gst_registration_type": {"type": "string", "default": ""},
                "party_gstin":     {"type": "string",  "default": ""},
                "place_of_supply": {"type": "string",  "default": ""},
                "state_name":      {"type": "string",  "default": ""},
                "cmp_gstin":       {"type": "string",  "default": ""},
                "bill_name":       {"type": "string",  "description": "Bill reference for bill-wise tracking", "default": ""},
                "bill_type":       {"type": "string",  "description": "'New Ref', 'Agst Ref', 'Advance'", "default": "New Ref"},
            },
            "required": ["date", "party_ledger", "line_items"],
        },
    },
    {
        "name": "create_purchase_voucher",
        "description": (
            "Create a Purchase invoice in TallyPrime with multiple inventory line items. "
            "Pass items as a JSON array in line_items: each needs stock_item_name, purchase_ledger, "
            "quantity, unit, rate, amount (net, always positive — code negates for XML), gst_rate. "
            "Optional per-item: hsn, godown, discount_percent, discount_amount. "
            "GST input tax ledgers (voucher-level): cgst_ledger+cgst_amount+sgst_ledger+sgst_amount (intrastate) "
            "OR igst_ledger+igst_amount (interstate). "
            "reference: supplier's invoice number for payables bill tracking. "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":            {"type": "string",  "description": "Invoice date YYYYMMDD"},
                "party_ledger":    {"type": "string",  "description": "Supplier ledger name"},
                "voucher_type":    {"type": "string",  "description": "Voucher type e.g. Purchase", "default": "Purchase"},
                "voucher_number":  {"type": "string",  "description": "Internal voucher number (optional)", "default": ""},
                "reference":       {"type": "string",  "description": "Supplier invoice/bill number for payables tracking", "default": ""},
                "narration":       {"type": "string",  "default": ""},
                "line_items":      {"type": "array",   "items": {"type": "object"}, "description": "Array of inventory line items", "default": []},
                "cgst_ledger":     {"type": "string",  "default": ""},
                "cgst_amount":     {"type": "number",  "default": 0},
                "sgst_ledger":     {"type": "string",  "default": ""},
                "sgst_amount":     {"type": "number",  "default": 0},
                "igst_ledger":     {"type": "string",  "default": ""},
                "igst_amount":     {"type": "number",  "default": 0},
                "additional_ledgers": {"type": "array", "items": {"type": "object"}, "default": []},
                "gst_registration_type": {"type": "string", "default": ""},
                "party_gstin":     {"type": "string",  "default": ""},
                "place_of_supply": {"type": "string",  "default": ""},
                "state_name":      {"type": "string",  "default": ""},
                "cmp_gstin":       {"type": "string",  "default": ""},
            },
            "required": ["date", "party_ledger", "line_items"],
        },
    },
    {
        "name": "create_payment_voucher",
        "description": (
            "Create a Payment voucher. Debits the party, credits the bank/cash ledger. "
            "Optionally include bill allocation and bank transfer details (NEFT/RTGS/IMPS). "
            "ALWAYS confirm with user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":               {"type": "string", "description": "Payment date YYYYMMDD"},
                "party_ledger":       {"type": "string", "description": "Supplier/party being paid"},
                "bank_ledger":        {"type": "string", "description": "Bank or cash ledger name"},
                "amount":             {"type": "number", "description": "Payment amount"},
                "narration":          {"type": "string", "default": ""},
                "bill_name":          {"type": "string", "description": "Bill reference for bill-wise tracking", "default": ""},
                "bill_type":          {"type": "string", "description": "'New Ref', 'Agst Ref', 'Advance'", "default": "New Ref"},
                "transaction_type":   {"type": "string", "description": "e.g. 'Inter Bank Transfer'", "default": ""},
                "transfer_mode":      {"type": "string", "description": "e.g. 'NEFT', 'RTGS', 'IMPS', 'UPI'", "default": ""},
                "ifsc_code":          {"type": "string", "description": "Bank IFSC code", "default": ""},
                "bank_name":          {"type": "string", "description": "Beneficiary bank name", "default": ""},
                "account_number":     {"type": "string", "description": "Beneficiary account number", "default": ""},
                "instrument_number":  {"type": "string", "description": "Transaction reference number", "default": ""},
                "payment_favouring":  {"type": "string", "description": "Beneficiary name", "default": ""},
            },
            "required": ["date", "party_ledger", "bank_ledger", "amount"],
        },
    },
    {
        "name": "create_receipt_voucher",
        "description": (
            "Create a Receipt voucher. Credits the party, debits the bank/cash ledger. "
            "Optionally include cheque/DD/transfer details. "
            "ALWAYS confirm with user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":               {"type": "string", "description": "Receipt date YYYYMMDD"},
                "party_ledger":       {"type": "string", "description": "Customer/party who paid"},
                "bank_ledger":        {"type": "string", "description": "Bank or cash ledger name"},
                "amount":             {"type": "number", "description": "Receipt amount"},
                "narration":          {"type": "string", "default": ""},
                "transaction_type":   {"type": "string", "description": "e.g. 'Cheque/DD', 'Inter Bank Transfer'", "default": ""},
                "bank_name":          {"type": "string", "description": "Payer bank name", "default": ""},
                "payment_favouring":  {"type": "string", "description": "Payment in favour of", "default": ""},
                "instrument_number":  {"type": "string", "description": "Cheque/transaction reference number", "default": ""},
            },
            "required": ["date", "party_ledger", "bank_ledger", "amount"],
        },
    },
]

SYSTEM_PROMPT = """You are a TallyPrime accounting assistant embedded in a mobile chat app.
You help users query their accounting data and create vouchers through natural conversation.

Today's date for reference: use the current date when the user says "today", "this month", "this year" etc.
Financial year in India typically runs April 1 – March 31. Dates must be passed in YYYYMMDD format to tools.

Guidelines:
- Format monetary values with ₹ symbol and Indian number system (thousands/lakhs/crores)
- Show Dr/Cr clearly for ledger balances (Dr = asset/expense, Cr = liability/income)
- Keep responses concise and mobile-friendly — avoid very long tables; summarise where possible
- For reports (trial balance, balance sheet etc.), highlight key figures: total assets, net profit, top debtors
- For outstanding receivables, highlight overdue amounts and top parties

Before creating any voucher (sales, payment, receipt):
1. Summarise exactly what will be posted: date, party, amount, ledgers
2. Ask "Shall I create this in TallyPrime?" and wait for explicit YES
3. Only call the create tool after the user confirms

Never guess ledger names — if unsure, use get_all_ledgers to find the exact name first."""


def execute_tally_tool(name: str, args: dict[str, Any]) -> Any:
    """Execute a TallyPrime tool call and return the result dict."""
    from . import tally_client as tc

    tally_url = args.get("tally_url") or None

    try:
        if name == "get_active_company":
            return tc.get_active_company(tally_url=tally_url)

        elif name == "get_all_ledgers":
            return tc.fetch_all_ledgers(tally_url=tally_url)

        elif name == "get_ledger":
            return tc.fetch_ledger(args["name"], tally_url=tally_url)

        elif name == "get_vouchers":
            return tc.fetch_vouchers(
                voucher_type=args.get("voucher_type", ""),
                from_date=args.get("from_date", ""),
                to_date=args.get("to_date", ""),
                party_name=args.get("party_name", ""),
                tally_url=tally_url,
            )

        elif name == "get_trial_balance":
            return tc.fetch_trial_balance(
                from_date=args.get("from_date", ""),
                to_date=args.get("to_date", ""),
                tally_url=tally_url,
            )

        elif name == "get_balance_sheet":
            return tc.fetch_balance_sheet(
                from_date=args.get("from_date", ""),
                to_date=args.get("as_of_date", args.get("to_date", "")),
                tally_url=tally_url,
            )

        elif name == "get_profit_loss":
            return tc.fetch_profit_loss(
                from_date=args.get("from_date", ""),
                to_date=args.get("to_date", ""),
                tally_url=tally_url,
            )

        elif name == "get_daybook":
            return tc.fetch_daybook(
                from_date=args.get("from_date", ""),
                to_date=args.get("to_date", ""),
                tally_url=tally_url,
            )

        elif name == "get_outstanding_receivables":
            return tc.fetch_outstanding_receivables(
                from_date=args.get("from_date", ""),
                as_of_date=args.get("as_of_date", ""),
                party_name=args.get("party_name", ""),
                ledger_group=args.get("ledger_group", "Sundry Debtors"),
                tally_url=tally_url,
            )

        elif name == "get_unit":
            return tc.get_unit(
                name=args["name"],
                tally_url=tally_url,
            )

        elif name == "get_all_units":
            return tc.get_all_units(tally_url=tally_url)

        elif name == "create_compound_unit":
            return tc.create_compound_unit(
                name=args["name"],
                base_units=args["base_units"],
                additional_units=args["additional_units"],
                conversion=int(args["conversion"]),
                tally_url=tally_url,
            )

        elif name == "create_simple_unit":
            return tc.create_simple_unit(
                name=args["name"],
                original_name=args.get("original_name", ""),
                tally_url=tally_url,
            )

        elif name == "get_all_stock_groups":
            return tc.get_all_stock_groups(tally_url=tally_url)

        elif name == "get_stock_group":
            return tc.get_stock_group(
                name=args["name"],
                tally_url=tally_url,
            )

        elif name == "create_stock_group":
            return tc.create_stock_group(
                name=args["name"],
                parent=args.get("parent", ""),
                tally_url=tally_url,
            )

        elif name == "get_stock_items_of_group":
            return tc.get_stock_items_of_group(
                group_name=args["group_name"],
                tally_url=tally_url,
            )

        elif name == "get_all_stock_items":
            return tc.get_all_stock_items(tally_url=tally_url)

        elif name == "get_stock_item":
            return tc.get_stock_item(
                name=args["name"],
                tally_url=tally_url,
            )

        elif name == "create_stock_item":
            return tc.create_stock_item(
                name=args["name"],
                parent=args.get("parent", "Primary"),
                base_units=args.get("base_units", "nos"),
                tally_url=tally_url,
            )

        elif name == "create_sales_voucher":
            return tc.create_sales_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                items=args.get("line_items", []),
                cgst_ledger=args.get("cgst_ledger", ""),
                cgst_amount=float(args.get("cgst_amount", 0)),
                sgst_ledger=args.get("sgst_ledger", ""),
                sgst_amount=float(args.get("sgst_amount", 0)),
                igst_ledger=args.get("igst_ledger", ""),
                igst_amount=float(args.get("igst_amount", 0)),
                voucher_type=args.get("voucher_type", "Sales"),
                voucher_number=args.get("voucher_number", ""),
                narration=args.get("narration", ""),
                additional_ledgers=args.get("additional_ledgers", []),
                gst_registration_type=args.get("gst_registration_type", ""),
                party_gstin=args.get("party_gstin", ""),
                place_of_supply=args.get("place_of_supply", ""),
                state_name=args.get("state_name", ""),
                cmp_gstin=args.get("cmp_gstin", ""),
                bill_name=args.get("bill_name", ""),
                bill_type=args.get("bill_type", "New Ref"),
                tally_url=tally_url,
            )

        elif name == "create_purchase_voucher":
            return tc.create_purchase_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                items=args.get("line_items", []),
                voucher_type=args.get("voucher_type", "Purchase"),
                voucher_number=args.get("voucher_number", ""),
                reference=args.get("reference", ""),
                narration=args.get("narration", ""),
                cgst_ledger=args.get("cgst_ledger", ""),
                cgst_amount=float(args.get("cgst_amount", 0)),
                sgst_ledger=args.get("sgst_ledger", ""),
                sgst_amount=float(args.get("sgst_amount", 0)),
                igst_ledger=args.get("igst_ledger", ""),
                igst_amount=float(args.get("igst_amount", 0)),
                additional_ledgers=args.get("additional_ledgers", []),
                gst_registration_type=args.get("gst_registration_type", ""),
                party_gstin=args.get("party_gstin", ""),
                place_of_supply=args.get("place_of_supply", ""),
                state_name=args.get("state_name", ""),
                cmp_gstin=args.get("cmp_gstin", ""),
                tally_url=tally_url,
            )

        elif name == "create_payment_voucher":
            return tc.create_payment_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                bank_or_cash_ledger=args["bank_ledger"],
                amount=float(args["amount"]),
                narration=args.get("narration", ""),
                bill_name=args.get("bill_name", ""),
                bill_type=args.get("bill_type", "New Ref"),
                transaction_type=args.get("transaction_type", ""),
                transfer_mode=args.get("transfer_mode", ""),
                ifsc_code=args.get("ifsc_code", ""),
                bank_name=args.get("bank_name", ""),
                account_number=args.get("account_number", ""),
                instrument_number=args.get("instrument_number", ""),
                payment_favouring=args.get("payment_favouring", ""),
                tally_url=tally_url,
            )

        elif name == "create_receipt_voucher":
            return tc.create_receipt_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                bank_or_cash_ledger=args["bank_ledger"],
                amount=float(args["amount"]),
                narration=args.get("narration", ""),
                transaction_type=args.get("transaction_type", ""),
                bank_name=args.get("bank_name", ""),
                payment_favouring=args.get("payment_favouring", ""),
                instrument_number=args.get("instrument_number", ""),
                tally_url=tally_url,
            )

        else:
            return {"error": f"Tool '{name}' not available in chat handler"}

    except Exception as e:
        logger.error("Tool execution error [%s]: %s", name, e, exc_info=True)
        return {"error": str(e)}


def _serialize_content(content: list) -> list:
    """Convert anthropic content blocks to JSON-serialisable dicts."""
    result = []
    for block in content:
        if hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif hasattr(block, "__dict__"):
            result.append(block.__dict__)
        else:
            result.append(block)
    return result


async def handle_chat(request: Request):
    """AI chat endpoint — runs the Claude + TallyPrime agentic loop."""
    try:
        body = await request.json()
        user_message = body.get("message", "").strip()
        history = body.get("history", [])   # [{role, content}] — text only, no tool blocks

        if not user_message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return JSONResponse({"error": "ANTHROPIC_API_KEY not set on server"}, status_code=500)

        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=anthropic_key)

        # Build messages: persisted text history + new user turn
        messages: list[dict] = list(history) + [{"role": "user", "content": user_message}]

        tools_used: list[str] = []

        # Agentic loop — max 10 tool-call rounds
        for _ in range(10):
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TALLY_TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                # Extract final text
                text = next(
                    (b.text for b in response.content if hasattr(b, "text")),
                    ""
                )
                # Update history with text-only turns (keep context lean)
                new_history = list(history) + [
                    {"role": "user",      "content": user_message},
                    {"role": "assistant", "content": text},
                ]
                return JSONResponse({
                    "response":   text,
                    "tools_used": tools_used,
                    "history":    new_history,
                })

            # Process tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tools_used.append(tool_name)
                    logger.info("Executing tool: %s  args: %s", tool_name, block.input)

                    result = await asyncio.get_event_loop().run_in_executor(
                        None, execute_tally_tool, tool_name, dict(block.input)
                    )

                    # Truncate very large results to keep context manageable
                    result_str = json.dumps(result, ensure_ascii=False)
                    if len(result_str) > 8000:
                        result_str = result_str[:8000] + "\n... [result truncated for context window]"

                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_str,
                    })

            # Append assistant tool-use turn and user tool-result turn
            messages.append({"role": "assistant", "content": _serialize_content(response.content)})
            messages.append({"role": "user",      "content": tool_results})

        return JSONResponse({
            "response":   "I've reached the maximum reasoning steps. Please try a more specific query.",
            "tools_used": tools_used,
            "history":    list(history) + [{"role": "user", "content": user_message}],
        })

    except Exception as e:
        logger.error("Chat error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────────
# PWA — serve index.html
# ─────────────────────────────────────────────────────────────────

async def handle_app(request: Request):
    """Serve the TallyPrime PWA chat interface."""
    pwa_path = Path(__file__).parent.parent.parent / "pwa" / "index.html"
    if not pwa_path.exists():
        return JSONResponse({"error": "PWA not found. Deploy pwa/index.html alongside this server."}, status_code=404)
    return HTMLResponse(pwa_path.read_text(encoding="utf-8"))


async def handle_install(request: Request):
    """Serve the install / scan-to-add-to-home-screen page."""
    pwa_dir = Path(__file__).parent.parent.parent / "pwa"
    page = pwa_dir / "install.html"
    if not page.exists():
        return JSONResponse({"error": "install.html not found in pwa/."}, status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


async def handle_install_qr_png(request: Request):
    """Serve the QR PNG file directly (for sharing / printing)."""
    img = Path(__file__).parent.parent.parent / "pwa" / "install-qr.png"
    if not img.exists():
        return JSONResponse({"error": "install-qr.png not found"}, status_code=404)
    return Response(img.read_bytes(), media_type="image/png")


async def handle_install_qr_svg(request: Request):
    """Serve the QR SVG file directly."""
    img = Path(__file__).parent.parent.parent / "pwa" / "install-qr.svg"
    if not img.exists():
        return JSONResponse({"error": "install-qr.svg not found"}, status_code=404)
    return Response(img.read_bytes(), media_type="image/svg+xml")


# ─────────────────────────────────────────────────────────────────
# Speech-to-text via OpenAI Whisper
# ─────────────────────────────────────────────────────────────────

async def handle_transcribe(request: Request):
    """Forward raw audio bytes from the PWA to OpenAI Whisper.

    Body  : raw audio (Content-Type identifies the codec, e.g. audio/webm)
    Header: Authorization: Bearer <MCP_API_KEY>   (the same as /chat)
    Reply : { "text": "<transcript>" }  on success
            { "error": "..." }          on failure (4xx / 5xx)

    This indirection means the OpenAI key never leaves the server, and the
    PWA works identically on every browser / phone / installed PWA — no
    reliance on flaky browser-side SpeechRecognition.
    """
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        return JSONResponse({"error": "OPENAI_API_KEY not set on server"}, status_code=500)

    audio_bytes = await request.body()
    if not audio_bytes:
        return JSONResponse({"error": "Empty audio payload"}, status_code=400)
    if len(audio_bytes) > 25 * 1024 * 1024:
        return JSONResponse({"error": "Audio too large (max 25 MB)"}, status_code=413)

    content_type = request.headers.get("content-type", "audio/webm")
    # Pick a sensible filename extension from the content type.
    if "ogg" in content_type:
        filename = "voice.ogg"
    elif "mp4" in content_type or "m4a" in content_type:
        filename = "voice.m4a"
    elif "wav" in content_type:
        filename = "voice.wav"
    elif "mpeg" in content_type or "mp3" in content_type:
        filename = "voice.mp3"
    else:
        filename = "voice.webm"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_key}"},
                files={"file": (filename, audio_bytes, content_type)},
                data={
                    "model": "whisper-1",
                    "language": request.query_params.get("lang", "en"),
                    "response_format": "json",
                },
            )
    except httpx.RequestError as e:
        logger.error("Whisper request failed: %s", e)
        return JSONResponse({"error": f"Whisper request failed: {e}"}, status_code=502)

    if r.status_code != 200:
        logger.warning("Whisper non-200: %s %s", r.status_code, r.text[:200])
        return JSONResponse(
            {"error": f"Whisper {r.status_code}: {r.text[:300]}"},
            status_code=502,
        )

    text = (r.json().get("text") or "").strip()
    return JSONResponse({"text": text})


# ─────────────────────────────────────────────────────────────────
# E-Invoicing — Clear (formerly ClearTax) GSP integration
# ─────────────────────────────────────────────────────────────────

from .einvoice_client import (
    get_client as get_einv_client,
    build_irn_payload,
    EInvoiceError,
    EInvoiceConfigError,
    EInvoiceAuthError,
)


async def handle_einvoice_generate(request: Request):
    """Generate an IRN. Body is either:
      * the friendly flat form dict (see einvoice_client.build_irn_payload), or
      * a NIC IRP schema-1.1 payload directly (detected by presence of
        'Version' + 'DocDtls' top-level keys).
    Also accepts a single-element array of either form.
    """
    try:
        form = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # If the user passed an array, take the first element.
    if isinstance(form, list) and form:
        form = form[0]

    # If the body is already in NIC IRP schema-1.1 format, send as-is.
    # Otherwise build from the flat form.
    if isinstance(form, dict) and "Version" in form and "DocDtls" in form:
        payload = form
    else:
        try:
            payload = build_irn_payload(form)
        except Exception as e:
            return JSONResponse({"error": f"Could not build IRN payload: {e}"}, status_code=400)

    client = get_einv_client()
    try:
        response = await client.generate_irn(payload)
    except EInvoiceConfigError as e:
        return JSONResponse(e.to_dict(), status_code=500)
    except EInvoiceAuthError as e:
        return JSONResponse(e.to_dict(), status_code=502)
    except EInvoiceError as e:
        return JSONResponse(e.to_dict(), status_code=502)

    # NIC IRP client returns {"data": <decoded payload>, "raw": <full envelope>}.
    # The payload itself can be flat or nested under another "Data"/"data" key.
    irn_data = response.get("data") or response or {}
    if isinstance(irn_data, dict) and (irn_data.get("Data") or irn_data.get("data")):
        irn_data = irn_data.get("Data") or irn_data.get("data")
    if not isinstance(irn_data, dict):
        irn_data = {}
    irn_value = irn_data.get("Irn") or irn_data.get("irn")
    signed_qr = irn_data.get("SignedQRCode") or irn_data.get("signed_qr_code")

    # Render the QR (preferring the signed JWT QR string, falling back to the IRN).
    qr_png_b64 = ""
    qr_payload = signed_qr or irn_value
    if qr_payload:
        try:
            import io, base64
            import qrcode
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=10, border=2)
            qr.add_data(qr_payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#0d1b2a", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            logger.warning("QR render failed: %s", e)

    return JSONResponse({
        "ok":      True,
        "irn":     irn_value,
        "ack_no":  irn_data.get("AckNo") or irn_data.get("ack_no"),
        "ack_dt":  irn_data.get("AckDt") or irn_data.get("ack_dt"),
        "signed_qr":      signed_qr,
        "signed_invoice": irn_data.get("SignedInvoice") or irn_data.get("signed_invoice"),
        "qr_png_b64":     qr_png_b64,
        # E-Way Bill fields — populated only when EwbDtls was supplied.
        "ewb_no":         irn_data.get("EwbNo")        or irn_data.get("ewb_no"),
        "ewb_dt":         irn_data.get("EwbDt")        or irn_data.get("ewb_dt"),
        "ewb_valid_till": irn_data.get("EwbValidTill") or irn_data.get("ewb_valid_till"),
        "raw":     response,
    })


async def handle_einvoice_cancel(request: Request):
    """Cancel an IRN. Body: {irn, reason_code, remarks}."""
    try:
        form = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    irn         = (form.get("irn") or "").strip()
    reason_code = str(form.get("reason_code") or "1")
    remarks     = (form.get("remarks") or "Cancelled via TallyPrime Assistant").strip()
    if not irn:
        return JSONResponse({"error": "irn is required"}, status_code=400)

    client = get_einv_client()
    try:
        response = await client.cancel_irn(irn, reason_code, remarks)
    except EInvoiceConfigError as e:
        return JSONResponse(e.to_dict(), status_code=500)
    except EInvoiceError as e:
        return JSONResponse(e.to_dict(), status_code=502)
    return JSONResponse({"ok": True, "raw": response})


# GSTIN state-code → state-name map. The e-invoice form stores codes like
# "29" (Karnataka), "27" (Maharashtra), etc., but TallyPrime ledger fields
# expect the full state name, so we translate before posting.
GST_STATE_CODE_TO_NAME = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "96": "Other Country",
}


def _state_name(code_or_name: str) -> str:
    """Return the state name for a GSTIN state code; pass-through if already a name."""
    s = str(code_or_name or "").strip()
    if not s:
        return ""
    # If it's already a 2-digit numeric code, look it up. Otherwise assume name.
    if s.isdigit() and len(s) <= 2:
        return GST_STATE_CODE_TO_NAME.get(s.zfill(2), s)
    return s


async def handle_voucher_from_einvoice(request: Request):
    """Post a Sales voucher to TallyPrime using the e-invoice form + IRN.

    Body:
      {
        "form":   { ...flat e-invoice form (same shape PWA submits)... },
        "irn":    "<irn>",
        "ack_no": "<ack number>",
        "ack_dt": "<ack date>",
        # Optional ledger overrides:
        "sales_ledger_template": "GST Sales {rate}%",
        "cgst_ledger": "CGST",
        "sgst_ledger": "SGST",
        "igst_ledger": "IGST",
      }

    Maps the form to TallyPrime sales-voucher fields, computes per-line
    GST split from intra/interstate detection, and adds the IRN/AckNo/AckDt
    to the narration.  No LLM in the loop.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    form      = body.get("form") or {}
    irn       = body.get("irn", "") or ""
    ack_no    = body.get("ack_no", "") or ""
    ack_dt    = body.get("ack_dt", "") or ""
    signed_qr = body.get("signed_qr", "") or ""
    ewb_no    = body.get("ewb_no", "") or ""
    ewb_dt    = body.get("ewb_dt", "") or ""
    ewb_veh_no = (body.get("ewb_veh_no") or "").strip().upper()

    # ── Convert NIC's "YYYY-MM-DD HH:MM:SS" ack date to Tally's two formats ──
    # Tally <IRNACKDATE>          wants YYYYMMDD          (8 digits)
    # Tally <IRNACKUPDATEDATETIME> wants YYYYMMDDHHMMSSsss (17 digits, 000 ms)
    irn_ack_date     = ""
    irn_ack_datetime = ""
    if ack_dt:
        try:
            from datetime import datetime
            dt = datetime.strptime(ack_dt.strip(), "%Y-%m-%d %H:%M:%S")
            irn_ack_date     = dt.strftime("%Y%m%d")
            irn_ack_datetime = dt.strftime("%Y%m%d%H%M%S") + "000"
        except ValueError:
            # If NIC ever returns just a date or a different format, pass-through.
            digits = "".join(c for c in ack_dt if c.isdigit())
            if len(digits) >= 8:
                irn_ack_date = digits[:8]
            if len(digits) >= 14:
                irn_ack_datetime = digits[:14] + "000"

    # ── Convert NIC's EwbDt ("YYYY-MM-DD HH:MM:SS" or "DD/MM/YYYY") → YYYYMMDD ──
    ewb_date_tally = ""
    if ewb_dt:
        s = ewb_dt.strip()
        try:
            from datetime import datetime
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    ewb_date_tally = dt.strftime("%Y%m%d")
                    break
                except ValueError:
                    continue
        except Exception:
            pass
        if not ewb_date_tally:
            digits = "".join(c for c in s if c.isdigit())
            if len(digits) >= 8:
                ewb_date_tally = digits[:8]

    seller = form.get("seller") or {}
    buyer  = form.get("buyer")  or {}
    items  = form.get("items")  or []

    # ── Validation ──────────────────────────────────────────────
    if not buyer.get("legal_name"):
        return JSONResponse({"error": "Buyer legal name is required"}, status_code=400)
    if not items:
        return JSONResponse({"error": "At least one line item is required"}, status_code=400)

    # ── Date conversion: DD/MM/YYYY → YYYYMMDD ──────────────────
    doc_date = (form.get("doc_date") or "").strip()
    if "/" in doc_date:
        try:
            d, m, y = doc_date.split("/")
            tally_date = f"{y}{m.zfill(2)}{d.zfill(2)}"
        except Exception:
            return JSONResponse({"error": f"Could not parse doc_date '{doc_date}' as DD/MM/YYYY"}, status_code=400)
    elif "-" in doc_date and len(doc_date) >= 10:
        # Tolerate DD-MM-YYYY or YYYY-MM-DD
        parts = doc_date.split("-")
        if len(parts[0]) == 4:
            tally_date = parts[0] + parts[1].zfill(2) + parts[2].zfill(2)
        else:
            tally_date = parts[2] + parts[1].zfill(2) + parts[0].zfill(2)
    else:
        tally_date = doc_date

    # ── Tax split: intrastate (CGST+SGST) vs interstate (IGST) ──
    intrastate = str(seller.get("stcd", "")).strip() == str(buyer.get("pos", "")).strip()

    # ── Ledger naming overrides (with sensible defaults) ────────
    sales_template = body.get("sales_ledger_template") or "GST Sales {rate}%"
    cgst_ledger    = body.get("cgst_ledger") or "CGST"
    sgst_ledger    = body.get("sgst_ledger") or "SGST"
    igst_ledger    = body.get("igst_ledger") or "IGST"

    # ── Build per-line items + roll up totals ───────────────────
    line_items   = []
    tot_cgst     = 0.0
    tot_sgst     = 0.0
    tot_igst     = 0.0
    for it in items:
        qty       = float(it.get("qty") or 0)
        rate      = float(it.get("rate") or 0)
        discount  = float(it.get("discount") or 0)
        gst_rate  = float(it.get("gst_rate") or 0)
        ass_amt   = round(qty * rate - discount, 2)
        gst_amt   = round(ass_amt * gst_rate / 100.0, 2)
        cgst_amt  = round(gst_amt / 2, 2) if intrastate else 0.0
        sgst_amt  = round(gst_amt / 2, 2) if intrastate else 0.0
        igst_amt  = 0.0 if intrastate else gst_amt

        # Format the per-line sales ledger name from the template.
        rate_str = str(int(gst_rate)) if gst_rate == int(gst_rate) else str(gst_rate)
        sales_ledger = sales_template.replace("{rate}", rate_str)

        line = {
            "stock_item_name": it.get("desc", "")[:100],
            "sales_ledger":    sales_ledger,
            "amount":          ass_amt,
            "rate":            rate,
            "quantity":        qty,
            "unit":            (it.get("unit") or "Nos"),
            "gst_rate":        gst_rate,
        }
        if it.get("hsn"):       line["hsn"] = str(it["hsn"])
        if discount > 0:        line["discount_amount"] = discount

        line_items.append(line)
        tot_cgst += cgst_amt
        tot_sgst += sgst_amt
        tot_igst += igst_amt

    # ── Narration ───────────────────────────────────────────────
    # The IRN/AckNo/AckDt now go into TallyPrime's dedicated XML tags
    # (<IRN>, <IRNACKNO>, <IRNACKDATE>, <IRNACKUPDATEDATETIME>, <IRNQRCODE>),
    # so we keep narration empty unless caller passes one explicitly.
    narration = (body.get("narration") or "").strip()

    # ── Translate GSTIN state codes to TallyPrime state names ───
    # Tally expects "Maharashtra", "Karnataka" etc. — not "27", "29".
    buyer_state_name    = _state_name(buyer.get("stcd"))
    place_of_supply_nm  = _state_name(buyer.get("pos"))

    # ── Call TallyPrime ─────────────────────────────────────────
    from . import tally_client as tc
    try:
        result = tc.create_sales_voucher(
            date=tally_date,
            party_ledger=buyer.get("legal_name", ""),
            items=line_items,
            voucher_type=body.get("voucher_type") or "Sales",
            voucher_number=str(form.get("doc_no") or ""),
            narration=narration,
            cgst_ledger=cgst_ledger if intrastate else "",
            cgst_amount=round(tot_cgst, 2),
            sgst_ledger=sgst_ledger if intrastate else "",
            sgst_amount=round(tot_sgst, 2),
            igst_ledger="" if intrastate else igst_ledger,
            igst_amount=round(tot_igst, 2),
            gst_registration_type="Regular",
            party_gstin=buyer.get("gstin", ""),
            place_of_supply=place_of_supply_nm,
            state_name=buyer_state_name,
            # NIC IRP schema-1.1 buyer is always domestic; default country India.
            # If you ever support export invoices, surface buyer.country in the form
            # and pass it through here.
            country=(buyer.get("country") or "India"),
            cmp_gstin=seller.get("gstin", ""),
            # E-Invoice fields → TallyPrime's dedicated XML tags
            irn=irn,
            irn_qr_code=signed_qr,
            irn_ack_no=str(ack_no) if ack_no else "",
            irn_ack_date=irn_ack_date,
            irn_ack_datetime=irn_ack_datetime,
            # E-Way Bill fields → <EWAYBILLDETAILS.LIST>
            ewb_no=str(ewb_no) if ewb_no else "",
            ewb_date=ewb_date_tally,
            ewb_veh_no=ewb_veh_no,
            # TransMode/VehType are hardcoded "1"/"R" matching the NIC payload.
        )
    except Exception as e:
        logger.exception("Sales voucher post failed")
        return JSONResponse(
            {"error": f"Tally voucher creation failed: {e}", "stage": "tally_call"},
            status_code=500,
        )

    # tally_client returns {"success": True, ...} on success, or
    # {"error": "..."} on Tally-side errors. Pass through.
    if isinstance(result, dict) and result.get("error"):
        return JSONResponse(
            {"error": result["error"], "stage": "tally_response", "raw": result},
            status_code=502,
        )

    return JSONResponse({
        "ok":              True,
        "voucher_number":  form.get("doc_no") or result.get("voucher_number") if isinstance(result, dict) else None,
        "narration":       narration,
        "intrastate":      intrastate,
        "totals":          {
            "cgst":  round(tot_cgst, 2),
            "sgst":  round(tot_sgst, 2),
            "igst":  round(tot_igst, 2),
        },
        "result":          result,
    })


# ─────────────────────────────────────────────────────────────────────
# Ledger creation — direct PWA → MCP → Tally (no LLM in the loop)
# ─────────────────────────────────────────────────────────────────────
async def handle_ledger_create(request: Request):
    """Create a ledger in TallyPrime.

    Body: { ledger_type: "party" | "sales" | "purchase" | "duty",
            name: "...", ...type-specific fields }

    Dispatches to the matching tally_client.create_*_ledger function so the
    PWA never has to know which XML envelope to build.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    ltype = (body.get("ledger_type") or "").strip().lower()
    name  = (body.get("name") or "").strip()

    if not ltype:
        return JSONResponse({"error": "ledger_type is required (party | sales | purchase | duty)"}, status_code=400)
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    from . import tally_client as tc
    try:
        if ltype == "party":
            result = tc.create_party_ledger(
                name=name,
                parent=body.get("parent") or "Sundry Debtors",
                opening_balance=float(body.get("opening_balance") or 0),
                gstin=(body.get("gstin") or "").upper(),
                gst_registration_type=body.get("gst_registration_type") or "Regular",
                address=body.get("address") or "",
                state=body.get("state") or "",
                country=body.get("country") or "India",
                pincode=body.get("pincode") or "",
                phone=body.get("phone") or "",
                email=body.get("email") or "",
                credit_period=body.get("credit_period") or "",
                credit_limit=float(body.get("credit_limit") or 0),
            )

        elif ltype == "sales":
            if not body.get("effective_date"):
                return JSONResponse({"error": "effective_date is required for sales ledger"}, status_code=400)
            result = tc.create_sales_ledger(
                name=name,
                effective_date=body.get("effective_date"),
                parent=body.get("parent") or "Sales Accounts",
                gst_type_of_supply=body.get("gst_type_of_supply") or "Goods",
                taxability=body.get("taxability") or "Taxable",
                gst_nature_of_transaction=body.get("gst_nature_of_transaction") or "",
                hsn_sac_code=body.get("hsn_sac_code") or "",
                hsn_description=body.get("hsn_description") or "",
                gst_rate=float(body.get("gst_rate") or 0),
                is_reverse_charge=bool(body.get("is_reverse_charge") or False),
            )

        elif ltype == "purchase":
            if not body.get("effective_date"):
                return JSONResponse({"error": "effective_date is required for purchase ledger"}, status_code=400)
            result = tc.create_purchase_ledger(
                name=name,
                effective_date=body.get("effective_date"),
                parent=body.get("parent") or "Purchase Accounts",
                gst_type_of_supply=body.get("gst_type_of_supply") or "Goods",
                taxability=body.get("taxability") or "Taxable",
                gst_nature_of_transaction=body.get("gst_nature_of_transaction") or "Interstate Purchase - Taxable",
                hsn_sac_code=body.get("hsn_sac_code") or "",
                hsn_description=body.get("hsn_description") or "",
                gst_rate=float(body.get("gst_rate") or 0),
                is_reverse_charge=bool(body.get("is_reverse_charge") or False),
                is_ineligible_itc=bool(body.get("is_ineligible_itc") or False),
            )

        elif ltype == "duty":
            duty_head = (body.get("duty_head") or "").strip()
            if not duty_head:
                return JSONResponse({"error": "duty_head is required for duty ledger (CGST | SGST/UTGST | IGST | Cess)"}, status_code=400)
            result = tc.create_duty_ledger(
                name=name,
                duty_head=duty_head,
                parent=body.get("parent") or "Duties & Taxes",
                rate_of_tax=float(body.get("rate_of_tax") or 0),
                cess_valuation_method=body.get("cess_valuation_method") or "Based on Value",
            )

        else:
            return JSONResponse(
                {"error": f"Unknown ledger_type '{ltype}'. Use: party | sales | purchase | duty"},
                status_code=400,
            )
    except Exception as e:
        logger.exception("Ledger creation failed")
        return JSONResponse(
            {"error": f"Ledger creation failed: {e}", "stage": "tally_call"},
            status_code=500,
        )

    # tally_client returns {"status": "success" | "error" | "no_change", ...}
    if isinstance(result, dict) and result.get("status") == "error":
        return JSONResponse({"ok": False, **result}, status_code=502)
    return JSONResponse({"ok": True, **(result if isinstance(result, dict) else {"raw": result})})


async def handle_einvoice_pdf(request: Request):
    """Render a printable HTML invoice with IRN + QR. Browser → Print → Save as PDF.

    Body: { form: {...flat form...}, irn: "...", ack_no, ack_dt, signed_qr }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    form     = body.get("form") or {}
    irn      = body.get("irn", "")
    ack_no   = body.get("ack_no", "")
    ack_dt   = body.get("ack_dt", "")
    qr_data  = body.get("signed_qr", "") or irn  # encode signed_qr into the QR if available

    seller   = form.get("seller") or {}
    buyer    = form.get("buyer")  or {}
    items    = form.get("items")  or []

    # Inline QR via the qrcode lib already installed for the install page.
    try:
        import io, base64
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(qr_data or irn or "no-irn")
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0d1b2a", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("QR render failed: %s", e)
        qr_b64 = ""

    # Build a compact, print-friendly HTML invoice.
    def _fmt_inr(n) -> str:
        try:
            n = float(n or 0)
        except (TypeError, ValueError):
            return "-"
        # Indian comma style for the integer part
        s = f"{n:,.2f}"
        parts = s.split(".")
        i, dec = parts[0].replace(",", ""), parts[1]
        if len(i) <= 3:
            grouped = i
        else:
            last3, rest = i[-3:], i[:-3]
            groups = []
            while len(rest) > 2:
                groups.insert(0, rest[-2:])
                rest = rest[:-2]
            if rest:
                groups.insert(0, rest)
            grouped = ",".join(groups + [last3])
        return f"&#8377; {grouped}.{dec}"

    rows_html = []
    for i, it in enumerate(items, 1):
        qty   = float(it.get("qty") or 0)
        rate  = float(it.get("rate") or 0)
        gstrt = float(it.get("gst_rate") or 0)
        ass   = round(qty * rate - float(it.get("discount") or 0), 2)
        gst   = round(ass * gstrt / 100, 2)
        total = round(ass + gst, 2)
        rows_html.append(f"""
            <tr>
              <td>{i}</td>
              <td>{(it.get("desc") or "")[:80]}</td>
              <td>{it.get("hsn","")}</td>
              <td class="num">{qty}</td>
              <td>{(it.get("unit","") or "").upper()}</td>
              <td class="num">{_fmt_inr(rate)}</td>
              <td class="num">{_fmt_inr(ass)}</td>
              <td class="num">{gstrt}%</td>
              <td class="num">{_fmt_inr(gst)}</td>
              <td class="num"><strong>{_fmt_inr(total)}</strong></td>
            </tr>
        """)

    grand_total = sum(
        round(float(it.get("qty") or 0) * float(it.get("rate") or 0)
              - float(it.get("discount") or 0), 2)
        * (1 + float(it.get("gst_rate") or 0) / 100)
        for it in items
    )

    qr_img_html = (
        f'<img src="data:image/png;base64,{qr_b64}" alt="IRN QR" style="width:170px;height:170px;">'
        if qr_b64 else
        '<div style="width:170px;height:170px;border:2px dashed #999;display:flex;align-items:center;justify-content:center;color:#999;font-size:12px;">QR unavailable</div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>e-Invoice {form.get('doc_no','')}</title>
  <style>
    @page {{ size: A4; margin: 14mm; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           color:#16203A; font-size:12px; padding: 0; margin: 0; }}
    h1 {{ font-size: 20px; margin: 0 0 4px; }}
    .small {{ font-size: 11px; color:#4B5670; }}
    .label {{ font-size: 10px; color:#8B95AB; text-transform: uppercase; letter-spacing: 0.5px; }}
    .head {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:2px solid #0d1b2a; padding-bottom:10px; margin-bottom:12px; }}
    .irn-box {{ background:#EEF4FB; border:1px solid #BBD4F2; border-radius:8px; padding:10px 12px; margin-bottom:12px; display:flex; gap:14px; align-items:center; }}
    .irn-box .col {{ flex:1; min-width:0; }}
    .irn-box code {{ font-family: 'SF Mono', Menlo, monospace; font-size:11px; word-break: break-all; display:block; }}
    .parties {{ display:flex; gap:16px; margin-bottom:12px; }}
    .party {{ flex:1; border:1px solid #E4E8EF; border-radius:8px; padding:10px; }}
    .party h3 {{ font-size:11px; margin:0 0 6px; color:#8B95AB; text-transform:uppercase; letter-spacing:0.5px;}}
    .party strong {{ font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; margin: 8px 0; font-size: 11px; }}
    th, td {{ padding: 6px 7px; border: 1px solid #E4E8EF; text-align:left; }}
    th {{ background:#0d1b2a; color:white; font-weight:700; font-size:10px; text-transform:uppercase; letter-spacing:0.4px;}}
    .num {{ text-align:right; }}
    .totals {{ display:flex; justify-content:flex-end; margin-top:6px; }}
    .totals .grand {{ font-weight:800; font-size:14px; color:#0d1b2a; }}
    .footer {{ margin-top:18px; font-size:10px; color:#8B95AB; }}
    .print-btn {{ position:fixed; top:14px; right:14px; padding:8px 14px; background:#1565C0; color:white; border:none; border-radius:8px; font-weight:700; cursor:pointer; }}
    @media print {{ .print-btn {{ display:none; }} }}
  </style>
</head>
<body>
  <button class="print-btn" onclick="window.print()">🖨 Print / Save PDF</button>
  <div class="head">
    <div>
      <h1>Tax Invoice</h1>
      <div class="small">e-Invoice — IRN signed by NIC IRP</div>
    </div>
    <div style="text-align:right;">
      <div class="label">Invoice No</div>
      <div><strong>{form.get('doc_no','')}</strong></div>
      <div class="label" style="margin-top:6px;">Date</div>
      <div>{form.get('doc_date','')}</div>
    </div>
  </div>

  <div class="irn-box">
    <div class="col">
      <div class="label">IRN</div>
      <code>{irn or '—'}</code>
      <div class="label" style="margin-top:6px;">Ack No / Ack Date</div>
      <div class="small">{ack_no or '—'} &nbsp;&middot;&nbsp; {ack_dt or '—'}</div>
    </div>
    <div>{qr_img_html}</div>
  </div>

  <div class="parties">
    <div class="party">
      <h3>Seller</h3>
      <strong>{seller.get('legal_name','')}</strong><br>
      <span class="small">GSTIN: {seller.get('gstin','')}</span><br>
      <span class="small">{seller.get('addr1','')} {seller.get('addr2','')}</span><br>
      <span class="small">{seller.get('loc','')} - {seller.get('pin','')} ({seller.get('stcd','')})</span>
    </div>
    <div class="party">
      <h3>Buyer</h3>
      <strong>{buyer.get('legal_name','')}</strong><br>
      <span class="small">GSTIN: {buyer.get('gstin','')}</span><br>
      <span class="small">{buyer.get('addr1','')} {buyer.get('addr2','')}</span><br>
      <span class="small">{buyer.get('loc','')} - {buyer.get('pin','')} ({buyer.get('stcd','')})</span><br>
      <span class="small">Place of Supply: {buyer.get('pos','')}</span>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>Description</th><th>HSN</th><th>Qty</th><th>Unit</th>
        <th>Rate</th><th>Taxable</th><th>GST</th><th>GST Amt</th><th>Total</th>
      </tr>
    </thead>
    <tbody>{''.join(rows_html) or '<tr><td colspan="10" style="text-align:center;color:#999;">No items</td></tr>'}</tbody>
  </table>

  <div class="totals">
    <div>
      <div class="label" style="text-align:right;">Grand Total</div>
      <div class="grand">{_fmt_inr(round(grand_total, 2))}</div>
    </div>
  </div>

  <div class="footer">
    Generated by TallyPrime Assistant. IRN issued by NIC IRP via Clear GSP. This is a computer-generated tax invoice.
  </div>
</body>
</html>"""
    return HTMLResponse(html)


# ─────────────────────────────────────────────────────────────────
# Starlette app
# ─────────────────────────────────────────────────────────────────

starlette_app = Starlette(
    routes=[
        Route("/health",             health,                  methods=["GET"]),
        Route("/app",                handle_app,              methods=["GET"]),
        Route("/install",            handle_install,          methods=["GET"]),
        Route("/install-qr.png",     handle_install_qr_png,   methods=["GET"]),
        Route("/install-qr.svg",     handle_install_qr_svg,   methods=["GET"]),
        Route("/chat",               handle_chat,             methods=["POST"]),
        Route("/transcribe",         handle_transcribe,       methods=["POST"]),
        Route("/einvoice/generate",        handle_einvoice_generate,     methods=["POST"]),
        Route("/einvoice/cancel",          handle_einvoice_cancel,       methods=["POST"]),
        Route("/einvoice/pdf",             handle_einvoice_pdf,          methods=["POST"]),
        Route("/voucher/sales/from-einvoice", handle_voucher_from_einvoice, methods=["POST"]),
        Route("/ledger/create",            handle_ledger_create,          methods=["POST"]),
        Mount("/sse/messages", app=sse_transport.handle_post_message),
        Mount("/sse",          app=handle_sse),
        Mount("/messages",     app=sse_transport.handle_post_message),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
)


# Wrap with pure-ASGI ApiKeyMiddleware AFTER Starlette is built so that
# it sits outside the CORS middleware and never buffers streaming responses.
asgi_app = ApiKeyMiddleware(starlette_app)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("Starting TallyPrime MCP HTTP server on %s:%s", HOST, PORT)
    logger.info("Tally Gateway URL: %s", os.environ.get("TALLY_URL", "http://localhost:9000"))
    uvicorn.run(asgi_app, host=HOST, port=PORT)


if __name__