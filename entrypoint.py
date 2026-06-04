"""
Wrapper around dbt-mcp's main entrypoint.

Three things this adds on top of dbt-mcp:

1.  FastMCP's __init__ hardcodes host=127.0.0.1 / port=8000. dbt-mcp
    calls it bare, so the server is unreachable from Railway's proxy
    unless we override. We read FASTMCP_HOST / FASTMCP_PORT from env
    and inject them.

2.  Per-user bearer-token auth via vault-data's mcp_user_tokens table
    (the same table Bob's /mcp endpoint uses). Tokens issued here are
    valid against both this server AND vault-data /mcp — single source
    of truth for "who's a user." Set VAULT_DATA_DATABASE_URL to the
    Postgres URL of the vault-data database.

3.  A web admin app at /admin/ for creating, regenerating, and
    revoking user tokens. Gated by MCP_ADMIN_KEY. Talks directly to
    Postgres (no Railway redeploy needed — changes are live instantly).

When VAULT_DATA_DATABASE_URL is unset, auth is disabled and the server
is open (warning logged). When MCP_ADMIN_KEY is unset, the admin
endpoint returns 404.
"""

from __future__ import annotations

import logging
import os

import psycopg2
from mcp.server.auth.provider import AccessToken, TokenVerifier  # type: ignore
from mcp.server.auth.settings import AuthSettings  # type: ignore
from mcp.server.fastmcp import FastMCP  # type: ignore
from psycopg2.extras import RealDictCursor
from pydantic import AnyHttpUrl

log = logging.getLogger("dbt-mcp-railway")


# ─── Postgres-backed TokenVerifier ───────────────────────────────────────────

class PostgresTokenVerifier(TokenVerifier):
    """Validates bearer tokens against vault-data's mcp_user_tokens table.

    Schema (created by vault-data's Data Model/_tbl_mcp_user_tokens.sql):
        token        TEXT        PRIMARY KEY
        email        TEXT        NOT NULL
        display_name TEXT
        role_label   TEXT        NOT NULL DEFAULT 'exec'
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        last_used_at TIMESTAMPTZ
        active       BOOLEAN     NOT NULL DEFAULT TRUE
        notes        TEXT

    On every successful auth:
      - UPDATE last_used_at = now()  (durable audit trail — survives restart)
      - Return AccessToken whose client_id is the user's email

    On failure (unknown token, revoked, DB error):
      - Return None → FastMCP rejects with 401
    """

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url
        # Probe the connection + table at startup so a misconfigured DB URL
        # crashes the container immediately instead of silently 401-ing
        # every request.
        try:
            with psycopg2.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_user_tokens WHERE active"
                    )
                    n_active = cur.fetchone()[0]
            log.info(
                "PostgresTokenVerifier connected to vault-data DB; "
                "%d active user tokens",
                n_active,
            )
        except Exception as e:
            log.error(
                "PostgresTokenVerifier startup probe failed: %s "
                "(check VAULT_DATA_DATABASE_URL + that mcp_user_tokens exists)",
                e,
            )
            raise

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate a bearer token against mcp_user_tokens.

        Three gates must all be true for the token to authenticate:
          - active            (account not revoked)
          - dbt_mcp_enabled   (per-MCP toggle for THIS server)

        An admin can disable a user's access here independently of
        vault-data /mcp — the same token may still work on vault-data
        if vault_data_enabled is true.
        """
        if not token:
            return None
        try:
            with psycopg2.connect(self._dsn) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        UPDATE mcp_user_tokens
                        SET last_used_at = now()
                        WHERE token = %s AND active AND dbt_mcp_enabled
                        RETURNING email, display_name, role_label
                        """,
                        (token,),
                    )
                    row = cur.fetchone()
        except Exception as e:
            log.error("Token verification DB error: %s", e)
            return None
        if not row:
            log.warning(
                "Rejected request: unknown token, revoked account, or "
                "dbt-mcp access disabled for this user"
            )
            return None
        return AccessToken(
            token=token,
            client_id=row["email"],
            scopes=["mcp:tools"],
            expires_at=None,
        )


# ─── FastMCP construction patch ──────────────────────────────────────────────

_orig_init = FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    # 1. Bind host / port from env (Railway needs 0.0.0.0:$PORT).
    kwargs.setdefault("host", os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    port_env = os.environ.get("FASTMCP_PORT")
    if port_env:
        kwargs.setdefault("port", int(port_env))

    # 2. Wire Postgres-backed bearer-token auth when VAULT_DATA_DATABASE_URL is set.
    database_url = os.environ.get("VAULT_DATA_DATABASE_URL")
    if database_url:
        public_url = os.environ.get("MCP_PUBLIC_URL")
        if not public_url:
            railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            public_url = (
                f"https://{railway_domain}"
                if railway_domain
                else "http://localhost:8000"
            )

        verifier = PostgresTokenVerifier(database_url)
        kwargs.setdefault("token_verifier", verifier)
        kwargs.setdefault(
            "auth",
            AuthSettings(
                issuer_url=AnyHttpUrl(public_url),
                resource_server_url=AnyHttpUrl(public_url),
                required_scopes=["mcp:tools"],
            ),
        )
        log.info(
            "Bearer-token auth enabled — validating against vault-data "
            "mcp_user_tokens (issuer=%s)",
            public_url,
        )
    else:
        log.warning(
            "VAULT_DATA_DATABASE_URL is not set — server is OPEN. "
            "Anyone with the URL can call the Semantic Layer."
        )

    _orig_init(self, *args, **kwargs)

    # 3. Register the admin web app + API routes on this FastMCP instance.
    from admin import register_admin_routes  # local import: admin.py sits next to entrypoint.py
    register_admin_routes(self)


FastMCP.__init__ = _patched_init  # type: ignore[assignment]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from dbt_mcp.main import main as _dbt_mcp_main

    _dbt_mcp_main()


if __name__ == "__main__":
    main()
