# Minimal Railway-compatible container for the dbt MCP server.
# We can't use dbt-labs/dbt-mcp's published Dockerfile directly because it
# uses BuildKit cache mounts without an `id=` argument, which Railway's
# Metal builder rejects. This is a clean install from PyPI instead.

FROM python:3.12-slim

# System-level deps (curl is useful for the healthcheck below)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Pin the version we tested against. Bump as new dbt-mcp releases drop.
# httpx is for the admin endpoints to call Railway's GraphQL API.
RUN pip install --no-cache-dir 'dbt-mcp==1.19.1' 'httpx>=0.27,<1'

# Wrapper that monkey-patches FastMCP.__init__ to honor FASTMCP_HOST /
# FASTMCP_PORT env vars (the SDK currently ignores them on construction,
# so dbt-mcp would otherwise bind to 127.0.0.1 — unreachable from
# Railway's proxy). It also registers the admin web app + API.
COPY entrypoint.py /app/entrypoint.py
COPY admin.py /app/admin.py
COPY admin.html /app/admin.html

# Run as non-root for hygiene
RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["python", "/app/entrypoint.py"]
