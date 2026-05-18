# Minimal Railway-compatible container for the dbt MCP server.
# We can't use dbt-labs/dbt-mcp's published Dockerfile directly because it
# uses BuildKit cache mounts without an `id=` argument, which Railway's
# Metal builder rejects. This is a clean install from PyPI instead.

FROM python:3.12-slim

# System-level deps (curl is useful for the healthcheck below)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Pin the version we tested against. Bump as new dbt-mcp releases drop.
RUN pip install --no-cache-dir 'dbt-mcp==1.19.1'

# Run as non-root for hygiene
RUN useradd -m -u 1000 appuser
USER appuser

# Railway expects the service to listen on $PORT. We pass it through; the
# MCP server reads it (along with MCP_TRANSPORT=streamable-http) from env.
EXPOSE 8000

ENTRYPOINT ["dbt-mcp"]
