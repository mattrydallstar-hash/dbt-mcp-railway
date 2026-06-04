"""
Admin web app for managing vault-data's mcp_user_tokens table.

Tokens issued here are valid against BOTH:
  - this server (the standalone dbt-mcp Semantic Layer endpoint)
  - vault-data's /mcp endpoint (raw SQL with PII guardrails)

Because both servers validate against the same Postgres table, you only
need to issue (and revoke) tokens in one place.

Exposes:
  GET    /admin/                              — HTML UI
  GET    /admin/api/users                     — list all users (active + revoked)
  POST   /admin/api/users                     — create user
                                                body: {email, display_name?, role_label?, notes?}
                                                returns token ONCE
  POST   /admin/api/users/{email}/regenerate  — rotate user's token
                                                returns new token ONCE
  POST   /admin/api/users/{email}/revoke      — set active=false (soft delete)
  POST   /admin/api/users/{email}/reactivate  — set active=true AND regenerate token
                                                (old token assumed compromised)
  PATCH  /admin/api/users/{email}             — update display_name, role_label, notes
                                                body: {display_name?, role_label?, notes?}
  DELETE /admin/api/users/{email}             — hard delete (use revoke for normal offboarding)

All /admin/api/* routes require Authorization: Bearer <MCP_ADMIN_KEY>.

Required env vars (in addition to what entrypoint.py needs):
  VAULT_DATA_DATABASE_URL — Postgres URL (same one entrypoint uses)
  MCP_ADMIN_KEY           — gates the admin endpoints. Long random string.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path

import psycopg2
from mcp.server.fastmcp import FastMCP  # type: ignore
from psycopg2.extras import RealDictCursor
from starlette.requests import Request  # type: ignore
from starlette.responses import HTMLResponse, JSONResponse  # type: ignore

log = logging.getLogger("dbt-mcp-railway.admin")

# RFC 5322 simplified — enough to catch obvious typos.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ─── OAuth resource-server metadata ──────────────────────────────────────────
# These endpoints let claude.ai's Custom Connector discover that we're a
# protected resource and that vault-data is our authorization server.
# RFC 9728 (Protected Resource Metadata) + RFC 8414 (Authorization Server
# Metadata). We're the resource server; vault-data has the full OAuth
# 2.0 + PKCE + DCR flow. Tokens land in shared mcp_user_tokens.

def _public_url() -> str:
    """Discover the dbt-mcp's own URL — where claude.ai sends MCP requests."""
    url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
    if url:
        return url
    rd = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").rstrip("/")
    return f"https://{rd}" if rd else "http://localhost:8000"


def _auth_server_url() -> str:
    """Discover the OAuth authorization server URL (vault-data)."""
    return os.environ.get(
        "AUTH_SERVER_URL",
        "https://vault-data-production.up.railway.app",
    ).rstrip("/")

# Roles we want to allow. Keep this aligned with whatever vault-data uses.
ALLOWED_ROLES = {"exec", "analyst", "admin", "csr", "ops", "owner"}


# ─── Postgres token CRUD ─────────────────────────────────────────────────────

class TokenDB:
    """All persistence for mcp_user_tokens. One method per admin action."""

    def __init__(self) -> None:
        self._dsn = os.environ.get("VAULT_DATA_DATABASE_URL")
        if not self._dsn:
            log.warning(
                "VAULT_DATA_DATABASE_URL is not set — admin endpoints will 500."
            )

    def _connect(self):
        if not self._dsn:
            raise RuntimeError(
                "VAULT_DATA_DATABASE_URL is not set on this Railway service"
            )
        return psycopg2.connect(self._dsn)

    def list_users(self) -> list[dict]:
        """Return all users (active + revoked), newest first."""
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT email,
                           display_name,
                           role_label,
                           active,
                           vault_data_enabled,
                           dbt_mcp_enabled,
                           created_at,
                           last_used_at,
                           notes,
                           -- Don't return the raw token; only the prefix for visual ID.
                           LEFT(token, 8) || '…' AS token_prefix
                    FROM mcp_user_tokens
                    ORDER BY active DESC, created_at DESC
                    """
                )
                return [dict(r) for r in cur.fetchall()]

    def find_active(self, email: str) -> dict | None:
        """Return the active row for email, or None."""
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT token, email, display_name, role_label, active,
                           created_at, last_used_at, notes
                    FROM mcp_user_tokens
                    WHERE email = %s AND active
                    LIMIT 1
                    """,
                    (email,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def find_any(self, email: str) -> dict | None:
        """Return any row for email (active or revoked)."""
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT token, email, display_name, role_label, active,
                           created_at, last_used_at, notes
                    FROM mcp_user_tokens
                    WHERE email = %s
                    ORDER BY active DESC, created_at DESC
                    LIMIT 1
                    """,
                    (email,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def create_user(
        self,
        email: str,
        display_name: str | None,
        role_label: str,
        notes: str | None,
        vault_data_enabled: bool = True,
        dbt_mcp_enabled: bool = True,
    ) -> str:
        """Insert a new user. Returns the freshly generated token."""
        if self.find_active(email):
            raise ValueError(f"user '{email}' already exists (active)")
        token = _new_token()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_user_tokens
                        (token, email, display_name, role_label, notes, active,
                         vault_data_enabled, dbt_mcp_enabled)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
                    """,
                    (
                        token, email, display_name, role_label, notes,
                        vault_data_enabled, dbt_mcp_enabled,
                    ),
                )
        return token

    def regenerate(self, email: str) -> str:
        """Rotate the active user's token. Old token immediately invalid.

        Implementation: UPDATE the existing row in place. The PK constraint
        on token is fine since we generate a fresh random value.
        """
        new_token = _new_token()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mcp_user_tokens
                    SET token = %s,
                        last_used_at = NULL
                    WHERE email = %s AND active
                    """,
                    (new_token, email),
                )
                if cur.rowcount == 0:
                    raise ValueError(
                        f"no active user '{email}' to regenerate "
                        "(check email or reactivate first)"
                    )
        return new_token

    def revoke(self, email: str) -> None:
        """Soft delete: set active=false. Token row preserved for audit."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mcp_user_tokens
                    SET active = FALSE
                    WHERE email = %s AND active
                    """,
                    (email,),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"no active user '{email}' to revoke")

    def reactivate(self, email: str) -> str:
        """Reactivate a revoked user AND regenerate their token.

        The old token is assumed compromised (that's typically why an
        account was revoked). New token returned ONCE.
        """
        new_token = _new_token()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mcp_user_tokens
                    SET active = TRUE,
                        token = %s,
                        last_used_at = NULL
                    WHERE email = %s AND NOT active
                    """,
                    (new_token, email),
                )
                if cur.rowcount == 0:
                    raise ValueError(
                        f"no revoked user '{email}' to reactivate "
                        "(maybe already active?)"
                    )
        return new_token

    def update_metadata(
        self,
        email: str,
        display_name: str | None = None,
        role_label: str | None = None,
        notes: str | None = None,
        vault_data_enabled: bool | None = None,
        dbt_mcp_enabled: bool | None = None,
    ) -> None:
        """Patch user fields. None = leave unchanged.

        Settable: display_name, role_label, notes, vault_data_enabled,
        dbt_mcp_enabled. The two enabled flags can be flipped on revoked
        users too (active flag controls account-wide, the two enabled
        flags control per-MCP).
        """
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name = %s")
            params.append(display_name)
        if role_label is not None:
            sets.append("role_label = %s")
            params.append(role_label)
        if notes is not None:
            sets.append("notes = %s")
            params.append(notes)
        if vault_data_enabled is not None:
            sets.append("vault_data_enabled = %s")
            params.append(vault_data_enabled)
        if dbt_mcp_enabled is not None:
            sets.append("dbt_mcp_enabled = %s")
            params.append(dbt_mcp_enabled)
        if not sets:
            return
        params.append(email)
        # Note: we deliberately don't filter on `active` here. An admin
        # may want to pre-stage flags on a revoked user before
        # reactivating, or flip toggles on an active user.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE mcp_user_tokens
                    SET {", ".join(sets)}
                    WHERE email = %s
                    """,
                    tuple(params),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"no user '{email}' to update")

    def hard_delete(self, email: str) -> None:
        """Permanently remove the row. Loses the audit history — use revoke
        for normal offboarding."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mcp_user_tokens WHERE email = %s",
                    (email,),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"no user '{email}' to delete")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _new_token() -> str:
    """Generate a 48-char URL-safe token. ~256 bits of entropy."""
    return secrets.token_urlsafe(36)


def _validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("email looks invalid")
    return email


def _validate_role(role: str | None) -> str:
    role = (role or "exec").strip().lower()
    if role not in ALLOWED_ROLES:
        raise ValueError(
            f"role_label '{role}' is not in {sorted(ALLOWED_ROLES)}"
        )
    return role


# ─── Route registration ──────────────────────────────────────────────────────

def register_admin_routes(mcp: FastMCP) -> None:
    """Wire admin + OAuth-metadata routes onto a FastMCP instance."""
    db = TokenDB()
    html_path = Path(__file__).parent / "admin.html"

    # ─── OAuth metadata routes ───────────────────────────────────────────
    # These advertise vault-data as the authorization server so claude.ai's
    # Custom Connector can do its OAuth dance there. FastMCP also auto-emits
    # `/.well-known/oauth-protected-resource` via AuthSettings — these
    # handlers cover the /mcp-scoped path that claude.ai sometimes probes,
    # and provide a manual /.well-known/oauth-authorization-server alias
    # that proxies vault-data's discovery doc (some clients fetch this on
    # the resource server rather than following the issuer redirect).

    def _protected_resource_doc():
        return {
            "resource": f"{_public_url()}/mcp",
            "authorization_servers": [_auth_server_url()],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        }

    def _authorization_server_doc():
        # Mirror the shape Bob emits on vault-data, but with the auth-server
        # base URL pointing at the real auth server. Lets clients that probe
        # this on the resource server still get something useful.
        base = _auth_server_url()
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": ["mcp"],
            "code_challenge_methods_supported": ["S256", "plain"],
        }

    @mcp.custom_route(
        "/mcp/.well-known/oauth-protected-resource",
        methods=["GET"],
        include_in_schema=False,
    )
    async def protected_resource_mcp_scoped(request: Request) -> JSONResponse:
        # MCP spec — protected-resource metadata MAY live under the resource path.
        # Anthropic clients have been observed to probe this path. (Bob added
        # the same alias on vault-data.)
        return JSONResponse(_protected_resource_doc())

    @mcp.custom_route(
        "/.well-known/oauth-protected-resource",
        methods=["GET"],
        include_in_schema=False,
    )
    async def protected_resource_root(request: Request) -> JSONResponse:
        # Manual root-path emit. FastMCP auto-mounts this too via AuthSettings,
        # but our hand-rolled doc is the canonical one (we want to control
        # exactly what claude.ai sees).
        return JSONResponse(_protected_resource_doc())

    @mcp.custom_route(
        "/.well-known/oauth-authorization-server",
        methods=["GET"],
        include_in_schema=False,
    )
    async def authorization_server_metadata(request: Request) -> JSONResponse:
        return JSONResponse(_authorization_server_doc())

    @mcp.custom_route(
        "/mcp/.well-known/oauth-authorization-server",
        methods=["GET"],
        include_in_schema=False,
    )
    async def authorization_server_metadata_mcp_scoped(request: Request) -> JSONResponse:
        return JSONResponse(_authorization_server_doc())

    def _require_admin(request: Request) -> JSONResponse | None:
        admin_key = os.environ.get("MCP_ADMIN_KEY")
        if not admin_key:
            return JSONResponse(
                {"error": "admin disabled (MCP_ADMIN_KEY not set)"},
                status_code=404,
            )
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing Bearer token"}, status_code=401
            )
        if auth.removeprefix("Bearer ").strip() != admin_key:
            return JSONResponse(
                {"error": "invalid admin token"}, status_code=403
            )
        return None

    @mcp.custom_route("/admin/", methods=["GET"], include_in_schema=False)
    async def admin_ui(request: Request) -> HTMLResponse:
        # No auth on the page itself — the JS prompts for the admin key
        # and uses it for every API call.
        if html_path.exists():
            return HTMLResponse(html_path.read_text())
        return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)

    @mcp.custom_route(
        "/admin/api/users", methods=["GET"], include_in_schema=False
    )
    async def list_users(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        try:
            users = db.list_users()
        except Exception as e:
            log.exception("list_users failed")
            return JSONResponse({"error": str(e), "users": []}, status_code=500)
        # JSONResponse can't serialize datetimes — convert.
        for u in users:
            for k in ("created_at", "last_used_at"):
                if u.get(k) is not None:
                    u[k] = u[k].isoformat()
        return JSONResponse({"users": users})

    @mcp.custom_route(
        "/admin/api/users", methods=["POST"], include_in_schema=False
    )
    async def create_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        try:
            email = _validate_email(payload.get("email", ""))
            role = _validate_role(payload.get("role_label"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        display_name = (payload.get("display_name") or "").strip() or None
        notes = (payload.get("notes") or "").strip() or None
        # Per-MCP toggles default to TRUE (matches schema default) but the
        # admin can explicitly disable one on creation.
        vd_enabled = bool(payload.get("vault_data_enabled", True))
        dbt_enabled = bool(payload.get("dbt_mcp_enabled", True))

        try:
            token = db.create_user(
                email, display_name, role, notes,
                vault_data_enabled=vd_enabled,
                dbt_mcp_enabled=dbt_enabled,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except Exception as e:
            log.exception("create_user failed")
            return JSONResponse({"error": str(e)}, status_code=500)

        log.info("Created user %s (role=%s)", email, role)
        return JSONResponse({
            "email": email,
            "token": token,
            "note": "Save this token now — it will not be shown again. "
                    "Valid on both this MCP and vault-data's /mcp.",
        })

    @mcp.custom_route(
        "/admin/api/users/{email}/regenerate",
        methods=["POST"],
        include_in_schema=False,
    )
    async def regenerate_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        email = request.path_params["email"]
        try:
            token = db.regenerate(email)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            log.exception("regenerate failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        log.info("Regenerated token for %s", email)
        return JSONResponse({
            "email": email,
            "token": token,
            "note": "Save this token now — old token revoked immediately.",
        })

    @mcp.custom_route(
        "/admin/api/users/{email}/revoke",
        methods=["POST"],
        include_in_schema=False,
    )
    async def revoke_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        email = request.path_params["email"]
        try:
            db.revoke(email)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            log.exception("revoke failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        log.info("Revoked user %s", email)
        return JSONResponse({"email": email, "revoked": True})

    @mcp.custom_route(
        "/admin/api/users/{email}/reactivate",
        methods=["POST"],
        include_in_schema=False,
    )
    async def reactivate_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        email = request.path_params["email"]
        try:
            token = db.reactivate(email)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            log.exception("reactivate failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        log.info("Reactivated user %s (token regenerated)", email)
        return JSONResponse({
            "email": email,
            "token": token,
            "note": "Save this token now — old token discarded.",
        })

    @mcp.custom_route(
        "/admin/api/users/{email}",
        methods=["PATCH"],
        include_in_schema=False,
    )
    async def edit_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        email = request.path_params["email"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        # Each field is optional. None = leave unchanged.
        dn = payload.get("display_name")
        role = payload.get("role_label")
        notes = payload.get("notes")
        # Per-MCP toggles. Accept either bool or omitted.
        vd_enabled = payload.get("vault_data_enabled")
        dbt_enabled = payload.get("dbt_mcp_enabled")
        if vd_enabled is not None:
            vd_enabled = bool(vd_enabled)
        if dbt_enabled is not None:
            dbt_enabled = bool(dbt_enabled)

        if role is not None:
            try:
                role = _validate_role(role)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        try:
            db.update_metadata(
                email,
                display_name=dn,
                role_label=role,
                notes=notes,
                vault_data_enabled=vd_enabled,
                dbt_mcp_enabled=dbt_enabled,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            log.exception("edit failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        log.info("Edited user %s", email)
        return JSONResponse({"email": email, "updated": True})

    @mcp.custom_route(
        "/admin/api/users/{email}",
        methods=["DELETE"],
        include_in_schema=False,
    )
    async def delete_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        email = request.path_params["email"]
        try:
            db.hard_delete(email)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            log.exception("delete failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        log.info("Hard-deleted user %s", email)
        return JSONResponse({"email": email, "deleted": True})
