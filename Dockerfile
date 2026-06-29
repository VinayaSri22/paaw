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

# Runtime dependencies + Node.js.
#   curl / ca-certificates : healthcheck + outbound TLS
#   nodejs (+ npm)         : runs the MCP servers as LOCAL subprocesses, so we
#                            no longer need `docker run` or the docker socket.
# mcp-searxng is installed globally; the WhatsApp MCP's SDK is installed later
# (after the source is copied in).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g mcp-searxng \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY paaw/ /app/paaw/
COPY configs/ /app/configs/
COPY jobs/ /app/jobs/
COPY skills/ /app/skills/
COPY mcp/ /app/mcp/

# The WhatsApp MCP server (mcp/whatsapp-baileys/index.js) is a thin stdio client
# to the bridge - its ONLY runtime dependency is the MCP SDK (baileys & friends
# belong to the always-on bridge service, not here). Install just the SDK so the
# image stays small and PAAW can launch `node index.js` as a subprocess.
# Temporarily hide the bridge's package.json so npm only resolves the SDK and
# not the heavy baileys tree (which pulls a git-based dep we don't want here).
RUN cd /app/mcp/whatsapp-baileys \
    && mv package.json package.json.bridge \
    && npm install --no-save --omit=dev @modelcontextprotocol/sdk@^1.0.0 \
    && mv package.json.bridge package.json \
    && npm cache clean --force

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create runtime dirs and hand the whole app tree to the non-root paaw user.
# chmod u+rwX ensures paaw can read all files and write where needed; the
# container then runs as paaw (not root) so a compromise can't touch the host
# or root-owned files.
RUN mkdir -p /app/logs \
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
