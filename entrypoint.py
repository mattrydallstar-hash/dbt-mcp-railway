"""
Wrapper around dbt-mcp's main entrypoint.

Three things this adds on top of dbt-mcp:

1.  FastMCP's __init__ hardcodes host=127.0.0.1 / port=8000. dbt-mcp
    calls it bare, so the server is unreachable from Railway's proxy
    unless we override. We read FASTMCP_HOST / FASTMCP_PORT from env
    and inject them.

2.  dbt-mcp ships zero incoming-auth. We add user-aligned static-key
    auth via MCP_API_KEYS_JSON:

        MCP_API_KEYS_JSON='{"matt":"abc123...","bob":"def456..."}'

    Each user's bearer token resolves to their username on the
    AccessToken, so request logs and downstream tools can attribute
    usage per-person.

3.  An /admin/users endpoint exposes the key table + per-user usage
    stats (call count, last-seen timestamp). Gated by a separate
    MCP_ADMIN_KEY env var. Useful for "who's using this and which
    key blew up the rate limit."

When MCP_API_KEYS_JSON is unset, auth is disabled (server is open,
warning logged). When MCP_ADMIN_KEY is unset, the admin endpoint
returns 404.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from mcp.server.auth.provider import AccessToken, TokenVerifier  # type: ignore
from mcp.server.auth.settings import AuthSettings  # type: ignore
from mcp.server.fastmcp import FastMCP  # type: ignore
from pydantic import AnyHttpUrl
from starlette.requests import Request  # type: ignore
from starlette.responses import JSONResponse  # type: ignore

log = logging.getLogger("dbt-mcp-railway")


# ─── Static-key TokenVerifier (with usage stats) ─────────────────────────────

class StaticKeyVerifier(TokenVerifier):
    """Validates bearer tokens against a fixed {user → key} mapping.

    Tracks per-user request counts and last-seen timestamps in memory.
    Counts reset whenever the container restarts — fine for an internal
    diagnostic view; not durable analytics.
    """

    def __init__(self, user_to_key: dict[str, str]) -> None:
        self._key_to_user = {key: user for user, key in user_to_key.items()}
        self._stats: dict[str, dict] = {
            user: {
                "key": key,
                "key_prefix": key[:8] + "…",
                "calls": 0,
                "last_seen": None,
            }
            for user, key in user_to_key.items()
        }
        log.info(
            "StaticKeyVerifier loaded with %d user keys: %s",
            len(self._stats),
            ", ".join(sorted(self._stats.keys())),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        user = self._key_to_user.get(token)
        if user is None:
            log.warning("Rejected request: unknown bearer token")
            return None
        # In-memory usage tracking.
        s = self._stats[user]
        s["calls"] += 1
        s["last_seen"] = datetime.now(timezone.utc).isoformat()
        return AccessToken(
            token=token,
            client_id=user,
            scopes=["mcp:tools"],
            expires_at=None,
        )

    def get_stats(self, *, include_keys: bool) -> list[dict]:
        """Return the user table. include_keys=True shows full keys;
        otherwise just the first-8-char prefix for visual identification."""
        rows = []
        for user, s in sorted(self._stats.items()):
            row = {
                "user": user,
                "calls": s["calls"],
                "last_seen": s["last_seen"],
            }
            if include_keys:
                row["key"] = s["key"]
            else:
                row["key_prefix"] = s["key_prefix"]
            rows.append(row)
        return rows


# Module-level singleton — the admin route handler reaches in here.
_verifier_singleton: StaticKeyVerifier | None = None


# ─── FastMCP construction patch ──────────────────────────────────────────────

_orig_init = FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    global _verifier_singleton

    # 1. Bind host / port from env.
    kwargs.setdefault("host", os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    port_env = os.environ.get("FASTMCP_PORT")
    if port_env:
        kwargs.setdefault("port", int(port_env))

    # 2. Wire user-aligned static-key auth when MCP_API_KEYS_JSON is set.
    keys_json = os.environ.get("MCP_API_KEYS_JSON")
    if keys_json:
        try:
            user_to_key = json.loads(keys_json)
            if not isinstance(user_to_key, dict) or not user_to_key:
                raise ValueError("must be a non-empty JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"MCP_API_KEYS_JSON is set but invalid: {e}. "
                "Expected JSON like {\"matt\":\"abc...\",\"bob\":\"def...\"}"
            ) from e

        public_url = os.environ.get("MCP_PUBLIC_URL")
        if not public_url:
            railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            public_url = (
                f"https://{railway_domain}" if railway_domain else "http://localhost:8000"
            )

        # Build the verifier once and reuse for the admin endpoint.
        if _verifier_singleton is None:
            _verifier_singleton = StaticKeyVerifier(user_to_key)

        kwargs.setdefault("token_verifier", _verifier_singleton)
        kwargs.setdefault(
            "auth",
            AuthSettings(
                issuer_url=AnyHttpUrl(public_url),
                resource_server_url=AnyHttpUrl(public_url),
                required_scopes=["mcp:tools"],
            ),
        )
        log.info("Bearer-token auth enabled (issuer=%s)", public_url)
    else:
        log.warning(
            "MCP_API_KEYS_JSON is not set — server is OPEN. "
            "Anyone with the URL can call the Semantic Layer."
        )

    _orig_init(self, *args, **kwargs)

    # 3. Register the admin web app + API routes on this FastMCP instance.
    from admin import register_admin_routes  # local import: admin.py sits next to entrypoint.py
    register_admin_routes(self, lambda: _verifier_singleton)


FastMCP.__init__ = _patched_init  # type: ignore[assignment]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from dbt_mcp.main import main as _dbt_mcp_main

    _dbt_mcp_main()


if __name__ == "__main__":
    main()
