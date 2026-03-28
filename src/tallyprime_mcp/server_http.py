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
        "name": "create_sales_voucher",
        "description": (
            "Create a Sales invoice in TallyPrime with a single line item. "
            "Pass item details as flat fields: stock_item_name, sales_ledger, quantity, unit, item_rate, amount. "
            "GST fields: gst_rate (item %), plus cgst_ledger+cgst_amount+sgst_ledger+sgst_amount (intrastate) "
            "OR igst_ledger+igst_amount (interstate). "
            "For multi-item invoices, call once per item. "
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
                "stock_item_name": {"type": "string",  "description": "Product/stock item name as in TallyPrime"},
                "sales_ledger":    {"type": "string",  "description": "Income/sales ledger to credit"},
                "quantity":        {"type": "number",  "description": "Item quantity"},
                "unit":            {"type": "string",  "description": "Unit of measure e.g. 'Nos', 'Kg'"},
                "item_rate":       {"type": "number",  "description": "Price per unit"},
                "amount":          {"type": "number",  "description": "Net line amount (post-discount, pre-tax)"},
                "gst_rate":        {"type": "number",  "description": "GST % for this item e.g. 5, 12, 18, 28", "default": 0},
                "cgst_ledger":     {"type": "string",  "default": ""},
                "cgst_amount":     {"type": "number",  "default": 0},
                "sgst_ledger":     {"type": "string",  "default": ""},
                "sgst_amount":     {"type": "number",  "default": 0},
                "igst_ledger":     {"type": "string",  "default": ""},
                "igst_amount":     {"type": "number",  "default": 0},
                "additional_ledgers": {"type": "array", "items": {"type": "object"}, "default": []},
            },
            "required": ["date", "party_ledger", "voucher_type", "stock_item_name", "sales_ledger", "quantity", "unit", "item_rate", "amount"],
        },
    },
    {
        "name": "create_payment_voucher",
        "description": "Create a Payment voucher. Debits the party, credits the bank/cash ledger. ALWAYS confirm with user before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date":         {"type": "string", "description": "Payment date YYYYMMDD"},
                "party_ledger": {"type": "string", "description": "Supplier/party being paid"},
                "bank_ledger":  {"type": "string", "description": "Bank or cash ledger name"},
                "amount":       {"type": "number", "description": "Payment amount"},
                "narration":    {"type": "string", "default": ""},
            },
            "required": ["date", "party_ledger", "bank_ledger", "amount"],
        },
    },
    {
        "name": "create_receipt_voucher",
        "description": "Create a Receipt voucher. Credits the party, debits the bank/cash ledger. ALWAYS confirm with user before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date":         {"type": "string", "description": "Receipt date YYYYMMDD"},
                "party_ledger": {"type": "string", "description": "Customer who paid"},
                "bank_ledger":  {"type": "string", "description": "Bank or cash ledger name"},
                "amount":       {"type": "number", "description": "Receipt amount"},
                "narration":    {"type": "string", "default": ""},
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

        elif name == "create_sales_voucher":
            return tc.create_sales_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                stock_item_name=args.get("stock_item_name", ""),
                sales_ledger=args.get("sales_ledger", ""),
                quantity=float(args.get("quantity", 0)),
                unit=args.get("unit", ""),
                item_rate=float(args.get("item_rate", 0)),
                amount=float(args.get("amount", 0)),
                gst_rate=float(args.get("gst_rate", 0)),
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
                tally_url=tally_url,
            )

        elif name == "create_payment_voucher":
            return tc.create_payment_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                bank_or_cash_ledger=args["bank_ledger"],
                amount=float(args["amount"]),
                narration=args.get("narration", ""),
                tally_url=tally_url,
            )

        elif name == "create_receipt_voucher":
            return tc.create_receipt_voucher(
                date=args["date"],
                party_ledger=args["party_ledger"],
                bank_or_cash_ledger=args["bank_ledger"],
                amount=float(args["amount"]),
                narration=args.get("narration", ""),
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
