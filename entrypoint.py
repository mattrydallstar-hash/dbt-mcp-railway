"""
Wrapper around dbt-mcp's main entrypoint.

Two reasons this exists:

1.  FastMCP's __init__ hardcodes host=127.0.0.1 / port=8000. dbt-mcp
    calls it bare, so the server is unreachable from Railway's proxy
    unless we override. We read FASTMCP_HOST / FASTMCP_PORT from env
    and inject them.

2.  dbt-mcp ships zero incoming-auth. Anyone who knows the URL has
    full access to the Semantic Layer. We add user-aligned static-key
    auth via MCP_API_KEYS_JSON:

        MCP_API_KEYS_JSON='{"matt":"abc123...","bob":"def456..."}'

    Each user's bearer token maps to their username. Logs and
    AccessToken.client_id carry the username so downstream usage can
    be attributed per-person.

When MCP_API_KEYS_JSON is unset, auth is disabled (server is open).
The deployment defaults this to required by setting the env var in
Railway — but flipping the env var off (and redeploying) is a kill
switch if every key needs revoking at once.
"""

from __future__ import annotations

import json
import logging
import os

from mcp.server.auth.provider import AccessToken, TokenVerifier  # type: ignore
from mcp.server.auth.settings import AuthSettings  # type: ignore
from mcp.server.fastmcp import FastMCP  # type: ignore
from pydantic import AnyHttpUrl

log = logging.getLogger("dbt-mcp-railway")


# ─── Static-key TokenVerifier ────────────────────────────────────────────────

class StaticKeyVerifier(TokenVerifier):
    """Validates bearer tokens against a fixed {user → key} mapping.

    Returns an AccessToken whose `client_id` is the username — so any
    request log line or downstream tool that records `client_id` will
    surface which teammate made the call.
    """

    def __init__(self, user_to_key: dict[str, str]) -> None:
        # Reverse map: key → username for O(1) lookup.
        self._key_to_user = {key: user for user, key in user_to_key.items()}
        self._users = sorted(user_to_key.keys())
        log.info("StaticKeyVerifier loaded with %d user keys: %s",
                 len(self._key_to_user), ", ".join(self._users))

    async def verify_token(self, token: str) -> AccessToken | None:
        user = self._key_to_user.get(token)
        if user is None:
            log.warning("Rejected request: unknown bearer token")
            return None
        return AccessToken(
            token=token,
            client_id=user,
            scopes=["mcp:tools"],
            expires_at=None,
        )


# ─── FastMCP construction patch ──────────────────────────────────────────────

_orig_init = FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    # 1. Bind host / port from env (FastMCP itself ignores env on these).
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
            # Fall back to Railway's auto-populated public domain if set.
            railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            public_url = f"https://{railway_domain}" if railway_domain else "http://localhost:8000"

        kwargs.setdefault(
            "token_verifier", StaticKeyVerifier(user_to_key)
        )
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


FastMCP.__init__ = _patched_init  # type: ignore[assignment]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from dbt_mcp.main import main as _dbt_mcp_main

    _dbt_mcp_main()


if __name__ == "__main__":
    main()
