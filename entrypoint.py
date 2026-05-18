"""
Wrapper around dbt-mcp's main entrypoint.

dbt-mcp invokes the MCP Python SDK's `FastMCP()` constructor with no
arguments, which hardcodes the bind host to 127.0.0.1 — fine for stdio,
but useless for a hosted streamable-http deployment where the proxy
needs to reach the server.

This wrapper monkey-patches `FastMCP.__init__` to read FASTMCP_HOST /
FASTMCP_PORT from the environment before dbt-mcp constructs its server,
then hands off to dbt_mcp.main.main(). Once mcp.server.fastmcp.FastMCP
gains native env-var honoring this wrapper can be removed.
"""

from __future__ import annotations

import os
from mcp.server.fastmcp import FastMCP  # type: ignore

_orig_init = FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("host", os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    port_env = os.environ.get("FASTMCP_PORT")
    if port_env:
        kwargs.setdefault("port", int(port_env))
    _orig_init(self, *args, **kwargs)


FastMCP.__init__ = _patched_init  # type: ignore[assignment]


def main() -> None:
    from dbt_mcp.main import main as _dbt_mcp_main

    _dbt_mcp_main()


if __name__ == "__main__":
    main()
