# Coolify Deployment

This project has a dedicated Docker Compose file for Coolify:

```text
docker-compose.coolify.yml
```

## Application

Create a new Coolify application from the GitHub repository.

- Build Pack: Docker Compose
- Docker Compose Location: `/docker-compose.coolify.yml`
- Public service: `api`
- Public port: `8000`
- Health check path: `/healthz`

The `bot` service does not need a public domain. The `api` service only needs a
domain if another service or browser client calls the intro API.

## Variables

Copy `.env.coolify.example` into Coolify Variables, then set at least one of:

```text
DISCORD_TOKEN=...
# or
DISCORD_TOKENS=token1,token2
```

For production, also change:

```text
SERVICE_PASSWORD_POSTGRES=...
INTRO_API_KEY=...
```

`INTRO_API_KEY` can be left empty if the external intro API is not used. Empty
API keys make protected endpoints return `401`, while `/healthz` stays public for
health checks.

## Optional API Domain

If the intro API should be reachable from outside Coolify, assign a domain to
the `api` service.

Example:

```text
https://intro-bot.usapo.space
```

Then call:

```text
GET /api/v1/guilds/{guild_id}/users/{user_id}/intro
Authorization: Bearer <INTRO_API_KEY>
```

If a browser client calls the API, set:

```text
INTRO_API_CORS_ORIGINS=https://example.com
```

For server-to-server calls, leave `INTRO_API_CORS_ORIGINS` empty.

## Database

Coolify stores Postgres data in the named Docker volume:

```text
postgres-data
```

The app services connect to Postgres through the internal service name
`postgres`; do not expose the database publicly.

## Deploy

After pushing changes to GitHub, deploy from the Coolify UI. Avoid changing
service configuration directly on the server; keep deployment configuration in
Git so future redeploys stay reproducible.
