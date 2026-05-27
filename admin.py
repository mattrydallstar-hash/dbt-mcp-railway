"""
Admin web app for managing dbt-mcp API keys.

Exposes:
  GET  /admin/                — HTML UI
  GET  /admin/api/users       — list users
  POST /admin/api/users       — create user (returns the new key once)
  DELETE /admin/api/users/{name}   — delete user
  POST /admin/api/users/{name}/rotate — regenerate user's key (returns new key once)

All routes require Authorization: Bearer <MCP_ADMIN_KEY>.

How it persists:
  Each mutation re-fetches MCP_API_KEYS_JSON from Railway, applies the
  change, and writes back via Railway's GraphQL API. Railway auto-
  redeploys on env-var change (~30s) and the new container reads the
  updated keys at startup. The in-memory verifier we already keep in
  entrypoint.StaticKeyVerifier is also updated in-process so the UI
  reflects changes immediately (between the write and the redeploy).

Required env vars (in addition to what entrypoint.py needs):
  RAILWAY_API_TOKEN          — project token with write scope
  RAILWAY_PROJECT_ID         — auto-injected by Railway
  RAILWAY_ENVIRONMENT_ID     — auto-injected by Railway
  RAILWAY_SERVICE_ID         — auto-injected by Railway
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP  # type: ignore
from starlette.requests import Request  # type: ignore
from starlette.responses import HTMLResponse, JSONResponse  # type: ignore

log = logging.getLogger("dbt-mcp-railway.admin")

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
KEYS_VAR = "MCP_API_KEYS_JSON"


# ─── Railway client (writes MCP_API_KEYS_JSON) ────────────────────────────────

class RailwayClient:
    def __init__(self) -> None:
        self.token = os.environ.get("RAILWAY_API_TOKEN")
        self.project_id = os.environ.get("RAILWAY_PROJECT_ID")
        self.env_id = os.environ.get("RAILWAY_ENVIRONMENT_ID")
        self.service_id = os.environ.get("RAILWAY_SERVICE_ID")
        self.enabled = all((self.token, self.project_id, self.env_id, self.service_id))
        if not self.enabled:
            log.warning(
                "Admin mutations disabled: missing one of RAILWAY_API_TOKEN, "
                "RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID."
            )

    async def get_keys(self) -> dict[str, str]:
        """Read the current MCP_API_KEYS_JSON from Railway (source of truth)."""
        if not self.enabled:
            raise RuntimeError("RailwayClient is disabled (missing env vars)")
        query = (
            "query Vars($projectId: String!, $environmentId: String!, $serviceId: String!) {"
            " variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) }"
        )
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                RAILWAY_API,
                json={"query": query, "variables": {
                    "projectId": self.project_id,
                    "environmentId": self.env_id,
                    "serviceId": self.service_id,
                }},
                headers={"Project-Access-Token": self.token},
            )
        r.raise_for_status()
        data = r.json()["data"]["variables"]
        raw = data.get(KEYS_VAR, "{}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    async def set_keys(self, keys: dict[str, str]) -> None:
        """Persist MCP_API_KEYS_JSON to Railway (triggers auto-redeploy)."""
        if not self.enabled:
            raise RuntimeError("RailwayClient is disabled (missing env vars)")
        mutation = (
            "mutation Upsert($input: VariableCollectionUpsertInput!) {"
            " variableCollectionUpsert(input: $input) }"
        )
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                RAILWAY_API,
                json={
                    "query": mutation,
                    "variables": {"input": {
                        "projectId": self.project_id,
                        "environmentId": self.env_id,
                        "serviceId": self.service_id,
                        "variables": {KEYS_VAR: json.dumps(keys)},
                        "replace": False,
                    }},
                },
                headers={"Project-Access-Token": self.token},
            )
        r.raise_for_status()
        result = r.json()
        if result.get("errors"):
            raise RuntimeError(f"Railway error: {result['errors']}")


# ─── Route registration ──────────────────────────────────────────────────────

def register_admin_routes(mcp: FastMCP, verifier_singleton_getter) -> None:
    """
    Wire admin routes onto a FastMCP instance.

    verifier_singleton_getter is a callable returning the current
    StaticKeyVerifier (or None). Lazy lookup so we don't snapshot a
    stale reference if the verifier ever gets rebuilt.
    """
    railway = RailwayClient()
    html_path = Path(__file__).parent / "admin.html"

    def _require_admin(request: Request) -> JSONResponse | None:
        admin_key = os.environ.get("MCP_ADMIN_KEY")
        if not admin_key:
            return JSONResponse(
                {"error": "admin disabled (MCP_ADMIN_KEY not set)"}, status_code=404
            )
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing Bearer token"}, status_code=401)
        if auth.removeprefix("Bearer ").strip() != admin_key:
            return JSONResponse({"error": "invalid admin token"}, status_code=403)
        return None

    @mcp.custom_route("/admin/", methods=["GET"], include_in_schema=False)
    async def admin_ui(request: Request) -> HTMLResponse:
        # No auth on the page itself — the JS prompts for the admin key
        # and uses it for every API call.
        if html_path.exists():
            return HTMLResponse(html_path.read_text())
        return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)

    @mcp.custom_route("/admin/api/users", methods=["GET"], include_in_schema=False)
    async def list_users(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        verifier = verifier_singleton_getter()
        stats = verifier.get_stats(include_keys=False) if verifier else []
        try:
            keys = await railway.get_keys()
        except Exception as e:
            return JSONResponse({"error": str(e), "users": []}, status_code=500)
        # Merge persisted keys (source of truth) with in-memory usage stats
        users = []
        stats_by_user = {row["user"]: row for row in stats}
        for user, key in sorted(keys.items()):
            s = stats_by_user.get(user, {})
            users.append({
                "user": user,
                "key_prefix": key[:8] + "…",
                "calls": s.get("calls", 0),
                "last_seen": s.get("last_seen"),
            })
        return JSONResponse({"users": users})

    @mcp.custom_route("/admin/api/users", methods=["POST"], include_in_schema=False)
    async def create_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not name.replace("_", "").replace("-", "").replace(".", "").isalnum():
            return JSONResponse(
                {"error": "name must be alphanumeric (with _ - . allowed)"},
                status_code=400,
            )
        keys = await railway.get_keys()
        if name in keys:
            return JSONResponse(
                {"error": f"user '{name}' already exists"}, status_code=409
            )
        new_key = secrets.token_urlsafe(32)
        keys[name] = new_key
        await railway.set_keys(keys)
        log.info("Created user '%s' (Railway redeploy triggered)", name)
        return JSONResponse({
            "user": name,
            "key": new_key,
            "note": "Save this key now — it will not be shown again. "
                    "Container redeploy is in progress (~30s).",
        })

    @mcp.custom_route(
        "/admin/api/users/{name}", methods=["DELETE"], include_in_schema=False
    )
    async def delete_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        name = request.path_params["name"]
        keys = await railway.get_keys()
        if name not in keys:
            return JSONResponse(
                {"error": f"user '{name}' not found"}, status_code=404
            )
        del keys[name]
        await railway.set_keys(keys)
        log.info("Deleted user '%s' (Railway redeploy triggered)", name)
        return JSONResponse({"user": name, "deleted": True})

    @mcp.custom_route(
        "/admin/api/users/{name}/rotate",
        methods=["POST"],
        include_in_schema=False,
    )
    async def rotate_user(request: Request) -> JSONResponse:
        if (err := _require_admin(request)):
            return err
        name = request.path_params["name"]
        keys = await railway.get_keys()
        if name not in keys:
            return JSONResponse(
                {"error": f"user '{name}' not found"}, status_code=404
            )
        new_key = secrets.token_urlsafe(32)
        keys[name] = new_key
        await railway.set_keys(keys)
        log.info("Rotated key for '%s' (Railway redeploy triggered)", name)
        return JSONResponse({
            "user": name,
            "key": new_key,
            "note": "Save this key now — old key revoked. "
                    "Container redeploy is in progress (~30s).",
        })
