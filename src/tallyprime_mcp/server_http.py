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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from .server import app as mcp_app  # re-use the same MCP server with all tools

logger = logging.getLogger(__name__)

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8000")))
API_KEY = os.environ.get("MCP_API_KEY", "")


# ─────────────────────────────────────────────────────────────────
# Optional API-key auth middleware
# ─────────────────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack the correct Bearer token (when API_KEY is set)."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)

        # Public paths — no auth required
        if request.url.path in ("/health", "/", "/app"):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_KEY}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ─────────────────────────────────────────────────────────────────
# SSE transport wiring
# ─────────────────────────────────────────────────────────────────

sse_transport = SseServerTransport("/messages")


async def handle_sse(request: Request):
    """SSE endpoint — MCP clients connect here to receive events."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send  # type: ignore[attr-defined]
    ) as (read_stream, write_stream):
        await mcp_app.run(
            read_stream,
            write_stream,
            mcp_app.create_initialization_options(),
        )


async def handle_messages(request: Request):
    """POST endpoint — MCP clients send tool call requests here."""
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send  # type: ignore[attr-defined]
    )


# ─────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────

async def health(request: Request):
    from . import tally_client as tc
    return JSONResponse({
        "status": "ok",
        "tally_url": tc.TALLY_URL,
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
            "Create a Sales invoice in TallyPrime. "
            "line_items array: each item needs stock_item_name, sales_ledger, amount (net), rate, quantity, unit, gst_rate. "
            "GST at voucher level: cgst_ledger+cgst_amount+sgst_ledger+sgst_amount (intrastate) OR igst_ledger+igst_amount (interstate). "
            "ALWAYS confirm details with the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":          {"type": "string", "description": "Invoice date YYYYMMDD"},
                "party_ledger":  {"type": "string", "description": "Customer ledger name"},
                "voucher_type":  {"type": "string", "description": "Voucher type e.g. Sales, Tax Invoice", "default": "Sales"},
                "voucher_number":{"type": "string", "description": "Invoice number (optional)", "default": ""},
                "narration":     {"type": "string", "default": ""},
                "line_items":    {"type": "array",  "items": {"type": "object"}, "description": "Array of line item objects"},
                "additional_ledgers": {"type": "array", "items": {"type": "object"}, "default": []},
                "cgst_ledger":   {"type": "string", "default": ""},
                "cgst_amount":   {"type": "number", "default": 0},
                "sgst_ledger":   {"type": "string", "default": ""},
                "sgst_amount":   {"type": "number", "default": 0},
                "igst_ledger":   {"type": "string", "default": ""},
                "igst_amount":   {"type": "number", "default": 0},
            },
            "required": ["date", "party_ledger", "voucher_type", "line_items"],
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
                voucher_type=args.get("voucher_type", "Sales"),
                voucher_number=args.get("voucher_number", ""),
                narration=args.get("narration", ""),
                line_items=args.get("line_items", []),
                additional_ledgers=args.get("additional_ledgers", []),
                cgst_ledger=args.get("cgst_ledger", ""),
                cgst_amount=args.get("cgst_amount", 0),
                sgst_ledger=args.get("sgst_ledger", ""),
                sgst_amount=args.get("sgst_amount", 0),
                igst_ledger=args.get("igst_ledger", ""),
                igst_amount=args.get("igst_amount", 0),
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
        Route("/sse",      handle_sse,      methods=["GET"]),
        Mount("/messages", app=sse_transport.handle_post_message),
    ],
    middleware=[Middleware(ApiKeyMiddleware)],
)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("Starting TallyPrime MCP HTTP server on %s:%s", HOST, PORT)
    logger.info("Tally Gateway URL: %s", os.environ.get("TALLY_URL", "http://localhost:9000"))
    uvicorn.run(starlette_app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
