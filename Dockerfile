# =============================================================================
# Stage 1: Build
# =============================================================================
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package installation
RUN pip install uv

# Copy project files
COPY pyproject.toml .
COPY README.md .

# Create virtual environment and install dependencies
RUN uv venv /app/.venv
RUN . /app/.venv/bin/activate && uv pip install -e .

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.12-slim as runtime

WORKDIR /app

# Create non-root user
RUN groupadd -r paaw && useradd -r -g paaw paaw

# Runtime dependencies + Node.js RUNTIME only (no MCP servers baked in).
#   curl / ca-certificates : healthcheck + outbound TLS
#   nodejs (+ npm)         : runs the MCP servers as LOCAL subprocesses.
# The MCP servers themselves are NOT installed here - they are auto-installed on
# first use at runtime into /app/mcp/node_modules (see paaw/tools/mcp_client.py).
# That means adding a new MCP = editing mcp/servers.json, with no image rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY paaw/ /app/paaw/
COPY configs/ /app/configs/
COPY jobs/ /app/jobs/
COPY skills/ /app/skills/
COPY mcp/ /app/mcp/

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Where MCP servers are auto-installed at runtime (npm --prefix). node_modules
# lands at /app/mcp/node_modules, which is an ancestor of the local WhatsApp MCP
# (mcp/whatsapp-baileys/index.js) so Node's parent-directory module lookup can
# resolve its deps. It's also where the searxng/discord bins resolve their deps.
ENV MCP_PACKAGES_DIR=/app/mcp

# Create runtime dirs and hand the whole app tree to the non-root paaw user.
# We pre-create /app/mcp/node_modules so that when the named volume is mounted
# there it initialises owned by paaw (named volumes inherit the image path's
# ownership) - otherwise it would be root-owned and runtime npm installs fail.
# chmod u+rwX ensures paaw can read all files and write where needed; the
# container then runs as paaw (not root) so a compromise can't touch the host.
RUN mkdir -p /app/logs /app/mcp/node_modules \
    && chown -R paaw:paaw /app \
    && chmod -R u+rwX /app

# Switch to non-root user
USER paaw

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose port
EXPOSE 8080

# Run PAAW
CMD ["python", "-m", "paaw.main"]
