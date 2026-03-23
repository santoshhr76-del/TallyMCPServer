"""
TallyPrime MCP Server — HTTP/SSE Transport
For cloud deployment (Railway, Render, AWS, GCP, etc.)

Exposes two endpoints:
  GET  /sse          — MCP Server-Sent Events stream (clients connect here)
  POST /messages     — MCP message inbox

Environment variables:
  TALLY_URL      URL of TallyPrime Gateway Server (default: http://localhost:9000)
  TALLY_TIMEOUT  HTTP timeout in seconds (default: 30)
  MCP_HOST       Server bind host (default: 0.0.0.0)
  MCP_PORT       Server bind port (default: 8000)
  MCP_API_KEY    Optional bearer token for request authentication
"""

import asyncio
import logging
import os
from typing import Any

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .server import app as mcp_app  # re-use the same MCP server with all tools

logger = logging.getLogger(__name__)

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
# Cloud Run injects PORT automatically; fall back to MCP_PORT then 8000
PORT = int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8000")))
API_KEY = os.environ.get("MCP_API_KEY", "")  # optional — leave blank to disable auth


# ─────────────────────────────────────────────────────────────────
# Optional API-key auth middleware
# ─────────────────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack the correct Bearer token (when API_KEY is set)."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)

        # Allow health-check without auth
        if request.url.path in ("/health", "/"):
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
    tally_url = tc.TALLY_URL
    return JSONResponse({
        "status": "ok",
        "tally_url": tally_url,
        "version": "0.1.0",
    })


# ─────────────────────────────────────────────────────────────────
# Starlette app
# ─────────────────────────────────────────────────────────────────

starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/sse", handle_sse, methods=["GET"]),
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
