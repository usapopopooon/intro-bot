import asyncio
import hmac
import logging
import os
import time

import aiohttp
import asyncpg
from aiohttp import web
from dotenv import load_dotenv

from bot import (
    DATABASE_URL,
    EMBED_DESCRIPTION_LIMIT,
    EXTERNAL_API_KEY,
    LEVEL_API_BASE,
    LEVEL_CACHE_TTL_SECONDS,
    build_chill_places,
    build_user_stats_url,
    fetch_user_level,
    format_chill_choice_name,
    init_schema,
    load_chill_place_overrides,
    load_user_chill_level,
    resolve_chill_display,
    serialize_chill_display,
    serialize_chill_place,
    set_user_chill_level,
    truncate,
)

load_dotenv()

INTRO_API_KEY = (os.environ.get("INTRO_API_KEY") or EXTERNAL_API_KEY).strip()
INTRO_API_HOST = os.environ.get("INTRO_API_HOST", "0.0.0.0")
INTRO_API_PORT = int(os.environ.get("INTRO_API_PORT") or os.environ.get("PORT", "8000"))
INTRO_API_AUTH_FAILURE_LIMIT = int(os.environ.get("INTRO_API_AUTH_FAILURE_LIMIT", "10"))
INTRO_API_AUTH_FAILURE_WINDOW_SECONDS = float(os.environ.get("INTRO_API_AUTH_FAILURE_WINDOW_SECONDS", "60"))
INTRO_API_CORS_ORIGINS = frozenset(
    origin.strip() for origin in os.environ.get("INTRO_API_CORS_ORIGINS", "").split(",") if origin.strip()
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intro-api")


def parse_bearer_token(request: web.Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def verify_intro_api_request(request: web.Request) -> bool:
    token = parse_bearer_token(request)
    return bool(token and INTRO_API_KEY and hmac.compare_digest(token, INTRO_API_KEY))


def request_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and peername:
        return str(peername[0])
    return "unknown"


def build_cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin")
    if not origin or not INTRO_API_CORS_ORIGINS:
        return {}
    if "*" in INTRO_API_CORS_ORIGINS:
        allowed_origin = "*"
    elif origin in INTRO_API_CORS_ORIGINS:
        allowed_origin = origin
    else:
        return {}
    headers = {
        "Access-Control-Allow-Origin": allowed_origin,
        "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
    }
    if allowed_origin != "*":
        headers["Vary"] = "Origin"
    return headers


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers.update(build_cors_headers(request))
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    if request.method == "OPTIONS" or request.path == "/healthz":
        return await handler(request)
    if verify_intro_api_request(request):
        return await handler(request)

    app = request.app
    if is_auth_limited(app, request):
        return web.json_response({"detail": "Too Many Requests"}, status=429)
    record_auth_failure(app, request)
    return web.json_response({"detail": "Unauthorized"}, status=401)


def is_auth_limited(app: web.Application, request: web.Request) -> bool:
    now = time.monotonic()
    ip = request_ip(request)
    failures = [t for t in app["auth_failures"].get(ip, []) if now - t < INTRO_API_AUTH_FAILURE_WINDOW_SECONDS]
    app["auth_failures"][ip] = failures
    return len(failures) >= INTRO_API_AUTH_FAILURE_LIMIT


def record_auth_failure(app: web.Application, request: web.Request) -> None:
    now = time.monotonic()
    ip = request_ip(request)
    failures = [t for t in app["auth_failures"].get(ip, []) if now - t < INTRO_API_AUTH_FAILURE_WINDOW_SECONDS]
    failures.append(now)
    app["auth_failures"][ip] = failures


def isoformat(value) -> str:
    return value.isoformat() if value is not None else ""


def parse_guild_user_ids(request: web.Request) -> tuple[int, int] | web.Response:
    try:
        return int(request.match_info["guild_id"]), int(request.match_info["user_id"])
    except ValueError:
        return web.json_response({"detail": "guild_id and user_id must be integers"}, status=400)


def serialize_chill_place_choice(place) -> dict:
    payload = serialize_chill_place(place)
    payload["choice_label"] = format_chill_choice_name(place)
    return payload


async def fetch_intro_record(pool: asyncpg.Pool, guild_id: int, user_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow(
            """
            SELECT guild_id, user_id, message_id, channel_id, content, jump_url, image_url,
                   author_display_name, author_avatar_url, created_at
            FROM intro_messages
            WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id,
            user_id,
        )


async def get_cached_user_level(request: web.Request, guild_id: int, user_id: int) -> tuple[int, float] | None:
    now = time.monotonic()
    cache = request.app["level_cache"]
    key = (guild_id, user_id)
    cached = cache.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    value = await fetch_user_level(request.app["http_session"], guild_id, user_id)
    cache[key] = (value, now + LEVEL_CACHE_TTL_SECONDS)
    return value


async def healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def intro_api(request: web.Request) -> web.Response:
    ids = parse_guild_user_ids(request)
    if isinstance(ids, web.Response):
        return ids
    guild_id, user_id = ids

    pool: asyncpg.Pool = request.app["pool"]
    row = await fetch_intro_record(pool, guild_id, user_id)
    if row is None:
        return web.json_response({"detail": "Intro not found"}, status=404)

    level_info = await get_cached_user_level(request, guild_id, user_id)
    overrides = await load_chill_place_overrides(pool, guild_id)
    selected_level = await load_user_chill_level(pool, guild_id, user_id)
    places = build_chill_places(overrides)
    chill_display = resolve_chill_display(places, level_info, selected_level)
    stats_url = build_user_stats_url(guild_id, user_id)

    level_payload = None
    if level_info is not None:
        level, progress = level_info
        level_payload = {
            "level": level,
            "progress": progress,
            "progress_percent": int(progress * 100),
            "footer_text": f"Lv. {level} ({int(progress * 100)}%)",
        }

    return web.json_response(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "member": {
                "id": user_id,
                "display_name": row["author_display_name"],
                "avatar_url": row["author_avatar_url"],
            },
            "intro": {
                "content": row["content"],
                "content_truncated": truncate(row["content"], EMBED_DESCRIPTION_LIMIT),
                "jump_url": row["jump_url"],
                "message_id": row["message_id"],
                "channel_id": row["channel_id"],
                "created_at": isoformat(row["created_at"]),
                "image_url": row["image_url"],
            },
            "level": level_payload,
            "chill_place": serialize_chill_display(chill_display),
            "stats_url": stats_url,
            "display": {
                "author_name": row["author_display_name"],
                "author_icon_url": row["author_avatar_url"],
                "intro_link_label": "ジャンプ",
                "stats_link_label": "30日間の統計を見る" if stats_url else None,
            },
        }
    )


async def chill_places_api(request: web.Request) -> web.Response:
    ids = parse_guild_user_ids(request)
    if isinstance(ids, web.Response):
        return ids
    guild_id, user_id = ids

    pool: asyncpg.Pool = request.app["pool"]
    level_info = await get_cached_user_level(request, guild_id, user_id)
    if level_info is None:
        return web.json_response({"detail": "Current level unavailable"}, status=424)

    current_level, progress = level_info
    overrides = await load_chill_place_overrides(pool, guild_id)
    selected_level = await load_user_chill_level(pool, guild_id, user_id)
    places = build_chill_places(overrides)
    unlocked = [place for place in places if place.required_level <= current_level]

    return web.json_response(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "level": {
                "level": current_level,
                "progress": progress,
                "progress_percent": int(progress * 100),
            },
            "selected_required_level": selected_level,
            "places": [serialize_chill_place_choice(place) for place in unlocked],
        }
    )


async def set_chill_place_api(request: web.Request) -> web.Response:
    ids = parse_guild_user_ids(request)
    if isinstance(ids, web.Response):
        return ids
    guild_id, user_id = ids

    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"detail": "Invalid JSON body"}, status=400)

    required_level = body.get("required_level") if isinstance(body, dict) else None
    if not isinstance(required_level, int) or isinstance(required_level, bool):
        return web.json_response({"detail": "required_level must be an integer"}, status=400)

    pool: asyncpg.Pool = request.app["pool"]
    level_info = await get_cached_user_level(request, guild_id, user_id)
    if level_info is None:
        return web.json_response({"detail": "Current level unavailable"}, status=424)

    current_level, progress = level_info
    overrides = await load_chill_place_overrides(pool, guild_id)
    places = build_chill_places(overrides)
    selected = next((place for place in places if place.required_level == required_level), None)
    if selected is None:
        return web.json_response({"detail": "Unknown chill place"}, status=400)
    if selected.required_level > current_level:
        return web.json_response({"detail": "Chill place is locked"}, status=403)

    await set_user_chill_level(pool, guild_id, user_id, selected.required_level)
    chill_display = resolve_chill_display(places, level_info, selected.required_level)

    return web.json_response(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "level": {
                "level": current_level,
                "progress": progress,
                "progress_percent": int(progress * 100),
            },
            "selected": serialize_chill_place_choice(selected),
            "chill_place": serialize_chill_display(chill_display),
        }
    )


async def make_app() -> web.Application:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    await init_schema(pool)
    session = aiohttp.ClientSession()

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app["pool"] = pool
    app["http_session"] = session
    app["auth_failures"] = {}
    app["level_cache"] = {}
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/v1/guilds/{guild_id}/users/{user_id}/intro", intro_api)
    app.router.add_get("/api/v1/guilds/{guild_id}/users/{user_id}/chill-places", chill_places_api)
    app.router.add_put("/api/v1/guilds/{guild_id}/users/{user_id}/chill-place", set_chill_place_api)

    async def cleanup(_app: web.Application) -> None:
        await session.close()
        await pool.close()

    app.on_cleanup.append(cleanup)
    return app


async def main() -> None:
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, INTRO_API_HOST, INTRO_API_PORT)
    await site.start()
    log.info(
        "intro API listening on %s:%s (level_api=%s)",
        INTRO_API_HOST,
        INTRO_API_PORT,
        "on" if LEVEL_API_BASE else "off",
    )
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
