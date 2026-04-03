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
        if path in ("/health", "/", "/app"):
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
        "description": "Get full details of a specific ledger: GSTIN, PAN, phone, address, credit terms, bill-wise settings, opening/closing balance.",
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
            return tc.fetch_company_info(tally_url=tally_url)

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


# ─────────────────────────────────────────────────────────────────
# Starlette app
# ─────────────────────────────────────────────────────────────────

starlette_app = Starlette(
    routes=[
        Route("/health",   health,          methods=["GET"]),
        Route("/app",      handle_app,      methods=["GET"]),
        Route("/chat",     handle_chat,     methods=["POST"]),
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


if __name__ == "__main__":
    run()
