# dbt-mcp-railway

Tiny shim for hosting [dbt-labs/dbt-mcp](https://github.com/dbt-labs/dbt-mcp)
on Railway. The upstream Dockerfile uses BuildKit cache mounts without
`id=` arguments, which Railway's builder rejects — so this repo just pulls
the published PyPI package into a clean container.

## Required env vars on the Railway service

```
MCP_TRANSPORT=streamable-http
DBT_HOST=cloud.getdbt.com
DBT_MCP_ENABLE_SEMANTIC_LAYER=true
PORT=8000
DBT_TOKEN=<dbt Cloud service token, Semantic Layer scope>
DBT_PROD_ENV_ID=<production env ID from dbt Cloud URL>
DBT_USER_ID=<your user ID from dbt Cloud profile URL>
```

## Bumping the dbt-mcp version

Edit the pinned version in `Dockerfile`, commit, push. Railway redeploys.
