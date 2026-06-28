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

# Runtime dependencies:
#   curl            - used by the HEALTHCHECK below
#   ca-certificates - outbound TLS for native tools (web_url_read, etc.)
# NOTE: the Docker CLI is intentionally NOT installed. Search + WhatsApp are now
# native in-process tools, so the executor no longer spawns MCP containers via
# `docker run`. Re-add docker-ce-cli (and the socket mount in compose) only if
# you enable a docker-based MCP server in mcp/servers.json.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
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

# Create necessary directories
RUN mkdir -p /app/logs && chown -R paaw:paaw /app

# Switch to non-root user
USER paaw

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose port
EXPOSE 8080

# Run PAAW
CMD ["python", "-m", "paaw.main"]
