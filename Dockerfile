# ── TallyPrime MCP Server — Cloud Dockerfile ──────────────────────────────
# Build:  docker build -t tallyprime-mcp .
# Run:    docker run -p 8000:8000 \
#           -e TALLY_URL=https://tally.yourdomain.com \
#           -e MCP_API_KEY=your-secret-key \
#           tallyprime-mcp

FROM python:3.12-slim

# ── system deps ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── copy source FIRST, then install ──────────────────────────────
# (src/ must exist before pip install -e . so the package is registered)
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY pwa/ ./pwa/

# Install all dependencies + the tallyprime_mcp package itself
RUN pip install --no-cache-dir mcp httpx "uvicorn[standard]" starlette anthropic \
 && pip install --no-cache-dir -e .

# ── runtime config (override via -e flags or Cloud Run env vars) ─
ENV TALLY_URL=http://host.docker.internal:9000
ENV TALLY_TIMEOUT=30
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV MCP_API_KEY=""

# Cloud Run injects PORT automatically — expose the same value
EXPOSE 8000

# ── healthcheck ───────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# ── start HTTP/SSE server ─────────────────────────────────────────
CMD ["python", "-m", "tallyprime_mcp.server_http"]
