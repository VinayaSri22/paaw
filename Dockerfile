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

# Install runtime dependencies + Docker CLI (so the job executor can spawn
# MCP servers via `docker run` against the host's mounted docker socket).
# Only the CLI is installed (docker-ce-cli), not the daemon. The executor stops
# each MCP container (run with --rm) after the job so they don't linger.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
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
