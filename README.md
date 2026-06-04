# dbt-mcp-railway

Tiny shim for hosting [dbt-labs/dbt-mcp](https://github.com/dbt-labs/dbt-mcp)
on Railway with **per-user bearer-token auth** backed by the same Postgres
table the vault-data MCP uses. One token per user, one place to manage,
valid against both MCP endpoints.

## Architecture

```
                   ┌──────────────────────────────┐
                   │ vault-data Postgres          │
                   │   mcp_user_tokens table      │  ← single source of truth
                   └──────┬───────────────────────┘
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
      ┌──────────────┐         ┌──────────────────┐
      │ vault-data   │         │ THIS SERVER      │
      │ /mcp         │         │ dbt-mcp-railway  │
      │ (raw SQL)    │         │ (semantic layer) │
      └──────────────┘         └──────────────────┘
              ▲                        ▲
              │                        │
              └────────── one token ───┘
```

Tokens are issued via the web admin at `/admin/`. They land in vault-data's
`mcp_user_tokens` table. Both this server and vault-data's `/mcp` validate
incoming bearer tokens against that same table — so one token per user
authorizes both surfaces.

## Required env vars on the Railway service

```
# dbt Cloud Semantic Layer (the MCP's actual data source)
MCP_TRANSPORT=streamable-http
DBT_HOST=cloud.getdbt.com
DBT_MCP_ENABLE_SEMANTIC_LAYER=true
PORT=8000
DBT_TOKEN=<dbt Cloud service token, Semantic Layer scope>
DBT_PROD_ENV_ID=<production env ID from dbt Cloud URL>
DBT_USER_ID=<your user ID from dbt Cloud profile URL>

# Postgres-backed per-user auth (shares mcp_user_tokens with vault-data)
VAULT_DATA_DATABASE_URL=<DATABASE_PUBLIC_URL of the vault-data Postgres>
MCP_ADMIN_KEY=<long random string — gates /admin/ UI + API>

# Required by FastMCP / Railway proxy
FASTMCP_HOST=0.0.0.0
FASTMCP_PORT=8000
```

### When env vars are missing

| Missing var | Behavior |
|---|---|
| `VAULT_DATA_DATABASE_URL` | Server runs **open** — anyone with the URL can query. Warning logged. Useful for dev only. |
| `MCP_ADMIN_KEY` | `/admin/` UI loads but every API call returns 404. Effectively disables admin. |

## Managing users

Open `https://<your-railway-domain>/admin/` in a browser. Paste the
`MCP_ADMIN_KEY` value. From there:

- **Add user**: email (required), display name, role, notes. Returns a
  freshly generated token **once** — copy it from the toast before
  closing. Token works against both this server and vault-data /mcp
  immediately, no redeploy needed.
- **Regenerate**: rotate a user's token. Old token stops working in the
  next request.
- **Revoke**: soft delete. Token stops working immediately; row preserved
  for audit history.
- **Reactivate**: undoes a revoke, but always generates a new token (the
  revoked one is assumed compromised).
- **Edit**: update display name, role, notes.
- **Hard delete**: permanently remove the row. Loses audit history — use
  revoke for normal offboarding.

All changes are direct UPDATEs on Postgres — they take effect on the
**next request**, no container redeploy involved.

## How a user connects

After you create a user and copy the token, the admin UI shows a ready-to-paste
one-liner. They run it once in their Claude Code shell:

```bash
claude mcp add --transport http dbt-sl \
  https://<your-railway-domain>/mcp \
  --header "Authorization: Bearer <token>"
```

Or, in Claude Desktop, add the URL + bearer token via the Custom Connector
flow.

## Schema reference

This server reads from / writes to:

```sql
-- Owned by vault-data — see allstar-st-api/Data Model/_tbl_mcp_user_tokens.sql
CREATE TABLE mcp_user_tokens (
    token        TEXT        PRIMARY KEY,
    email        TEXT        NOT NULL,
    display_name TEXT,
    role_label   TEXT        NOT NULL DEFAULT 'exec',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    active       BOOLEAN     NOT NULL DEFAULT TRUE,
    notes        TEXT
);
```

vault-data's `deploy_views.py` keeps this table created and idempotent.
Don't manage the schema from this service — it's downstream.

## Bumping the dbt-mcp version

Edit the pinned version in `Dockerfile`, commit, push. Railway redeploys.

## Local dev

```bash
export VAULT_DATA_DATABASE_URL="postgres://..."  # public URL from vault-data Railway
export MCP_ADMIN_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DBT_TOKEN=...
export DBT_PROD_ENV_ID=...
export DBT_USER_ID=...
export FASTMCP_HOST=127.0.0.1
export FASTMCP_PORT=8000
export MCP_TRANSPORT=streamable-http
export DBT_HOST=cloud.getdbt.com
export DBT_MCP_ENABLE_SEMANTIC_LAYER=true
python entrypoint.py
```

Then `open http://localhost:8000/admin/`.
