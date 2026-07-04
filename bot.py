import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiohttp
import asyncpg
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

_TOKENS_RAW = os.environ.get("DISCORD_TOKENS") or os.environ.get("DISCORD_TOKEN")
TOKENS = [t.strip() for t in (_TOKENS_RAW or "").split(",") if t.strip()]
DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))
INTRO_HISTORY_MAX_SCAN = int(os.environ.get("INTRO_HISTORY_MAX_SCAN", "5000"))
LEVEL_API_BASE = (os.environ.get("LEVEL_API_BASE") or "").rstrip("/")
LEVEL_API_TIMEOUT_SECONDS = float(os.environ.get("LEVEL_API_TIMEOUT_SECONDS", "3"))
LEVEL_CACHE_TTL_SECONDS = float(os.environ.get("LEVEL_CACHE_TTL_SECONDS", "60"))
EXTERNAL_API_KEY = (os.environ.get("EXTERNAL_API_KEY") or "").strip()
LEVEL_CHILL_API_KEY = (os.environ.get("LEVEL_CHILL_API_KEY") or EXTERNAL_API_KEY).strip()
USER_STATS_SITE_GUILD_ID = (os.environ.get("USER_STATS_SITE_GUILD_ID") or "").strip()
USER_STATS_SITE_BASE_URL = (os.environ.get("USER_STATS_SITE_BASE_URL") or "").strip().rstrip("/")

EMBED_DESCRIPTION_LIMIT = 4000
DISCORD_MESSAGE_LIMIT = 2000
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intro-bot")


@dataclass
class GuildConfig:
    guild_id: int
    intro_channel_id: int | None
    cooldown_seconds: int
    excluded_vc_ids: frozenset[int]
    nudge_exempt_role_ids: frozenset[int]


@dataclass(frozen=True)
class ChillPlace:
    required_level: int
    name: str
    emoji: str | None = None
    tags: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True)
class ChillPlaceOverride:
    name: str
    emoji: str | None = None


@dataclass(frozen=True)
class ChillDisplay:
    current: ChillPlace | None
    next_place: ChillPlace | None
    selected_locked: bool = False


@dataclass(frozen=True)
class IntroRecord:
    guild_id: int
    user_id: int
    message_id: int
    channel_id: int
    content: str
    jump_url: str
    image_url: str | None
    author_display_name: str
    author_avatar_url: str
    created_at: datetime


DEFAULT_CHILL_PLACES: tuple[ChillPlace, ...] = (
    ChillPlace(1, "入口のベンチ", "🪑", ("はじめまして", "気軽"), "まずはここで、ゆっくり空気を眺める席。"),
    ChillPlace(2, "ロビーソファ", "🛋️", ("雑談", "のんびり"), "通りすがりの会話に混ざりやすい、やわらかい場所。"),
    ChillPlace(3, "窓際スツール", "🪟", ("ひと休み", "明るい"), "外の気配を感じながら、少しだけ腰を下ろす席。"),
    ChillPlace(4, "小さな丸テーブル", "☕", ("少人数", "気軽"), "近くの人と軽く話すのにちょうどいいテーブル。"),
    ChillPlace(5, "カフェカウンター", "🥤", ("雑談", "作業前"), "飲み物を片手に、その日の調子を整える場所。"),
    ChillPlace(6, "本棚のそば", "📚", ("静か", "読書"), "会話も作業も、少し落ち着いた声になる一角。"),
    ChillPlace(7, "観葉植物の横", "🪴", ("すみっこ", "安心"), "ほどよく人の気配がある、静かなすみっこ。"),
    ChillPlace(8, "ふかふかチェア", "💤", ("まったり", "休憩"), "ちょっと疲れた日に沈み込む席。"),
    ChillPlace(9, "充電席", "🔌", ("回復", "作業"), "端末も気持ちも、じわっと充電していく場所。"),
    ChillPlace(10, "いつものカフェ席", "☕", ("定位置", "雑談"), "顔なじみの会話が自然に始まる席。"),
    ChillPlace(12, "静かな作業机", "📝", ("集中", "静か"), "少し集中したい日に向いた、整った机。"),
    ChillPlace(14, "本棚奥の席", "📖", ("読書", "隠れ家"), "本棚の奥で、話しかけられすぎずに過ごせる場所。"),
    ChillPlace(16, "夜更かしテーブル", "🌙", ("夜", "作業"), "遅い時間のゆるい作業と雑談が似合うテーブル。"),
    ChillPlace(18, "半個室ソファ", "🕯️", ("少人数", "落ち着く"), "少しこもって、近い人たちと過ごせるソファ。"),
    ChillPlace(20, "チルラウンジ", "🍵", ("節目", "まったり"), "ここまで来た人のための、広めでゆるいラウンジ。"),
    ChillPlace(25, "窓辺の作業部屋", "🌤️", ("集中", "景色"), "景色を横目に、ゆっくり手を動かす部屋。"),
    ChillPlace(30, "深夜の作業部屋", "🌃", ("深夜", "集中"), "静かな夜に、ぽつぽつ人が集まる作業部屋。"),
    ChillPlace(40, "中庭ベンチ", "🌿", ("外気", "休憩"), "少し外に出た気分で、肩の力を抜けるベンチ。"),
    ChillPlace(50, "暖炉前", "🔥", ("常連", "ぬくもり"), "長くいる人たちの会話がゆっくり続く場所。"),
    ChillPlace(75, "屋上テラス", "🌌", ("夜風", "特別"), "夜風にあたりながら、静かに話せる特別席。"),
    ChillPlace(100, "常連席", "🏆", ("記念", "定位置"), "ここまで過ごしてきた人だけの、ちょっと誇らしい席。"),
)


_SELECT_COLUMNS = "guild_id, intro_channel_id, cooldown_seconds, excluded_vc_ids, nudge_exempt_role_ids"


def _row_to_config(row) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        intro_channel_id=row["intro_channel_id"],
        cooldown_seconds=row["cooldown_seconds"],
        excluded_vc_ids=frozenset(row["excluded_vc_ids"] or ()),
        nudge_exempt_role_ids=frozenset(row["nudge_exempt_role_ids"] or ()),
    )


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as con:
        await con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS bot_config (
                guild_id         BIGINT PRIMARY KEY,
                intro_channel_id BIGINT,
                cooldown_seconds INTEGER NOT NULL DEFAULT {DEFAULT_COOLDOWN_SECONDS}
                                 CHECK (cooldown_seconds BETWEEN 1 AND 86400),
                excluded_vc_ids  BIGINT[] NOT NULL DEFAULT '{{}}',
                nudge_exempt_role_ids BIGINT[] NOT NULL DEFAULT '{{}}',
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await con.execute(
            "ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS excluded_vc_ids BIGINT[] NOT NULL DEFAULT '{}'"
        )
        await con.execute(
            "ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS nudge_exempt_role_ids BIGINT[] NOT NULL DEFAULT '{}'"
        )
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_chill_places (
                guild_id       BIGINT NOT NULL,
                required_level INTEGER NOT NULL CHECK (required_level >= 1),
                name           TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
                emoji          TEXT CHECK (emoji IS NULL OR char_length(emoji) BETWEEN 1 AND 40),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, required_level)
            )
            """
        )
        await con.execute(
            "ALTER TABLE guild_chill_places ADD COLUMN IF NOT EXISTS emoji TEXT "
            "CHECK (emoji IS NULL OR char_length(emoji) BETWEEN 1 AND 40)"
        )
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_chill_places (
                guild_id       BIGINT NOT NULL,
                user_id        BIGINT NOT NULL,
                required_level INTEGER NOT NULL CHECK (required_level >= 1),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS intro_messages (
                guild_id            BIGINT NOT NULL,
                user_id             BIGINT NOT NULL,
                message_id          BIGINT NOT NULL,
                channel_id          BIGINT NOT NULL,
                content             TEXT NOT NULL,
                jump_url            TEXT NOT NULL,
                image_url           TEXT,
                author_display_name TEXT NOT NULL,
                author_avatar_url   TEXT NOT NULL,
                created_at          TIMESTAMPTZ NOT NULL,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )


async def load_all_configs(pool: asyncpg.Pool) -> dict[int, GuildConfig]:
    async with pool.acquire() as con:
        rows = await con.fetch(f"SELECT {_SELECT_COLUMNS} FROM bot_config")
    return {r["guild_id"]: _row_to_config(r) for r in rows}


async def load_all_chill_place_overrides(pool: asyncpg.Pool) -> dict[int, dict[int, ChillPlaceOverride]]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT guild_id, required_level, name, emoji FROM guild_chill_places")
    overrides: dict[int, dict[int, ChillPlaceOverride]] = {}
    for row in rows:
        overrides.setdefault(row["guild_id"], {})[row["required_level"]] = ChillPlaceOverride(
            name=row["name"],
            emoji=row["emoji"],
        )
    return overrides


async def load_all_user_chill_levels(pool: asyncpg.Pool) -> dict[int, dict[int, int]]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT guild_id, user_id, required_level FROM user_chill_places")
    selections: dict[int, dict[int, int]] = {}
    for row in rows:
        selections.setdefault(row["guild_id"], {})[row["user_id"]] = row["required_level"]
    return selections


async def load_chill_place_overrides(pool: asyncpg.Pool, guild_id: int) -> dict[int, ChillPlaceOverride]:
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT required_level, name, emoji FROM guild_chill_places WHERE guild_id = $1",
            guild_id,
        )
    return {
        row["required_level"]: ChillPlaceOverride(
            name=row["name"],
            emoji=row["emoji"],
        )
        for row in rows
    }


async def load_user_chill_level(pool: asyncpg.Pool, guild_id: int, user_id: int) -> int | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT required_level FROM user_chill_places WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
    return row["required_level"] if row is not None else None


async def upsert_chill_place(
    pool: asyncpg.Pool,
    guild_id: int,
    required_level: int,
    name: str,
    emoji: str | None = None,
) -> None:
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO guild_chill_places (guild_id, required_level, name, emoji)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, required_level) DO UPDATE
            SET name = EXCLUDED.name, emoji = EXCLUDED.emoji, updated_at = NOW()
            """,
            guild_id,
            required_level,
            name,
            emoji,
        )


async def remove_chill_place(pool: asyncpg.Pool, guild_id: int, required_level: int) -> bool:
    async with pool.acquire() as con:
        result = await con.execute(
            "DELETE FROM guild_chill_places WHERE guild_id = $1 AND required_level = $2",
            guild_id,
            required_level,
        )
    return result == "DELETE 1"


async def set_user_chill_level(pool: asyncpg.Pool, guild_id: int, user_id: int, required_level: int) -> None:
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO user_chill_places (guild_id, user_id, required_level)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET required_level = EXCLUDED.required_level, updated_at = NOW()
            """,
            guild_id,
            user_id,
            required_level,
        )


async def clear_user_chill_level(pool: asyncpg.Pool, guild_id: int, user_id: int) -> None:
    async with pool.acquire() as con:
        await con.execute(
            "DELETE FROM user_chill_places WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )


async def upsert_intro_record(pool: asyncpg.Pool, record: IntroRecord) -> None:
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO intro_messages (
                guild_id, user_id, message_id, channel_id, content, jump_url, image_url,
                author_display_name, author_avatar_url, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET message_id = EXCLUDED.message_id,
                channel_id = EXCLUDED.channel_id,
                content = EXCLUDED.content,
                jump_url = EXCLUDED.jump_url,
                image_url = EXCLUDED.image_url,
                author_display_name = EXCLUDED.author_display_name,
                author_avatar_url = EXCLUDED.author_avatar_url,
                created_at = EXCLUDED.created_at,
                updated_at = NOW()
            WHERE intro_messages.created_at <= EXCLUDED.created_at
            """,
            record.guild_id,
            record.user_id,
            record.message_id,
            record.channel_id,
            record.content,
            record.jump_url,
            record.image_url,
            record.author_display_name,
            record.author_avatar_url,
            record.created_at,
        )


async def delete_intro_record(pool: asyncpg.Pool, guild_id: int, user_id: int, message_id: int) -> None:
    async with pool.acquire() as con:
        await con.execute(
            "DELETE FROM intro_messages WHERE guild_id = $1 AND user_id = $2 AND message_id = $3",
            guild_id,
            user_id,
            message_id,
        )


async def upsert_intro_channel(pool: asyncpg.Pool, guild_id: int, channel_id: int) -> GuildConfig:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            INSERT INTO bot_config (guild_id, intro_channel_id, cooldown_seconds)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE
            SET intro_channel_id = EXCLUDED.intro_channel_id, updated_at = NOW()
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            channel_id,
            DEFAULT_COOLDOWN_SECONDS,
        )
    return _row_to_config(row)


async def upsert_cooldown(pool: asyncpg.Pool, guild_id: int, seconds: int) -> GuildConfig:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            INSERT INTO bot_config (guild_id, intro_channel_id, cooldown_seconds)
            VALUES ($1, NULL, $2)
            ON CONFLICT (guild_id) DO UPDATE
            SET cooldown_seconds = EXCLUDED.cooldown_seconds, updated_at = NOW()
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            seconds,
        )
    return _row_to_config(row)


async def add_excluded_vc(pool: asyncpg.Pool, guild_id: int, channel_id: int) -> GuildConfig:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            INSERT INTO bot_config (guild_id, intro_channel_id, cooldown_seconds, excluded_vc_ids)
            VALUES ($1, NULL, $2, ARRAY[$3]::BIGINT[])
            ON CONFLICT (guild_id) DO UPDATE
            SET excluded_vc_ids = (
                SELECT COALESCE(ARRAY_AGG(DISTINCT v), '{{}}'::BIGINT[])
                FROM UNNEST(bot_config.excluded_vc_ids || ARRAY[$3]::BIGINT[]) AS v
            ), updated_at = NOW()
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            DEFAULT_COOLDOWN_SECONDS,
            channel_id,
        )
    return _row_to_config(row)


async def remove_excluded_vc(pool: asyncpg.Pool, guild_id: int, channel_id: int) -> GuildConfig | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            UPDATE bot_config
            SET excluded_vc_ids = array_remove(excluded_vc_ids, $2), updated_at = NOW()
            WHERE guild_id = $1
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            channel_id,
        )
    return _row_to_config(row) if row is not None else None


async def prune_unavailable_excluded_vcs_in_db(
    pool: asyncpg.Pool,
    guild_id: int,
    available_channel_ids: frozenset[int],
) -> tuple[GuildConfig | None, frozenset[int]]:
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                f"SELECT {_SELECT_COLUMNS} FROM bot_config WHERE guild_id = $1 FOR UPDATE",
                guild_id,
            )
            if row is None:
                return None, frozenset()

            cfg = _row_to_config(row)
            kept, removed = split_available_excluded_vc_ids(cfg.excluded_vc_ids, available_channel_ids)
            if not removed:
                return cfg, frozenset()

            updated = await con.fetchrow(
                f"""
                UPDATE bot_config
                SET excluded_vc_ids = $2::BIGINT[], updated_at = NOW()
                WHERE guild_id = $1
                RETURNING {_SELECT_COLUMNS}
                """,
                guild_id,
                sorted(kept),
            )
    return (_row_to_config(updated) if updated is not None else None), removed


async def add_nudge_exempt_role(pool: asyncpg.Pool, guild_id: int, role_id: int) -> GuildConfig:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            INSERT INTO bot_config (guild_id, intro_channel_id, cooldown_seconds, nudge_exempt_role_ids)
            VALUES ($1, NULL, $2, ARRAY[$3]::BIGINT[])
            ON CONFLICT (guild_id) DO UPDATE
            SET nudge_exempt_role_ids = (
                SELECT COALESCE(ARRAY_AGG(DISTINCT v), '{{}}'::BIGINT[])
                FROM UNNEST(bot_config.nudge_exempt_role_ids || ARRAY[$3]::BIGINT[]) AS v
            ), updated_at = NOW()
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            DEFAULT_COOLDOWN_SECONDS,
            role_id,
        )
    return _row_to_config(row)


async def remove_nudge_exempt_role(pool: asyncpg.Pool, guild_id: int, role_id: int) -> GuildConfig | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            f"""
            UPDATE bot_config
            SET nudge_exempt_role_ids = array_remove(nudge_exempt_role_ids, $2), updated_at = NOW()
            WHERE guild_id = $1
            RETURNING {_SELECT_COLUMNS}
            """,
            guild_id,
            role_id,
        )
    return _row_to_config(row) if row is not None else None


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_chill_place_name(place: ChillPlace) -> str:
    return f"{place.emoji} {place.name}" if place.emoji else place.name


def format_chill_choice_name(place: ChillPlace) -> str:
    return f"{format_chill_place_name(place)} (Lv.{place.required_level})"


def resolve_chill_place_selection(places: tuple[ChillPlace, ...], selection: str) -> ChillPlace | None:
    text = selection.strip()
    if text.isdigit():
        level = int(text)
        return next((place for place in places if place.required_level == level), None)
    return next(
        (
            place
            for place in places
            if text in {place.name, format_chill_place_name(place), format_chill_choice_name(place)}
        ),
        None,
    )


def build_chill_place_choices(
    places: tuple[ChillPlace, ...],
    current: str,
    current_level: int | None = None,
) -> list[app_commands.Choice[str]]:
    query = current.strip().lower()
    unlocked = [place for place in places if current_level is None or place.required_level <= current_level]
    matches = [
        place
        for place in unlocked
        if not query
        or query in place.name.lower()
        or query in format_chill_place_name(place).lower()
        or query in str(place.required_level)
    ]
    return [
        app_commands.Choice(name=format_chill_choice_name(place)[:100], value=str(place.required_level))
        for place in matches[:25]
    ]


def build_chill_places(overrides: dict[int, ChillPlaceOverride] | None = None) -> tuple[ChillPlace, ...]:
    by_level = {place.required_level: place for place in DEFAULT_CHILL_PLACES}
    if overrides:
        for level, override in overrides.items():
            default = by_level.get(level)
            tags = default.tags if default is not None else ()
            description = default.description if default is not None else None
            emoji = override.emoji if override.emoji is not None else default.emoji if default is not None else None
            by_level[level] = ChillPlace(level, override.name, emoji=emoji, tags=tags, description=description)
    return tuple(by_level[level] for level in sorted(by_level))


def resolve_chill_display(
    places: tuple[ChillPlace, ...],
    level_info: tuple[int, float] | None,
    selected_level: int | None = None,
) -> ChillDisplay | None:
    if level_info is None or not places:
        return None
    level, _ = level_info
    unlocked = [place for place in places if place.required_level <= level]
    if not unlocked:
        next_place = next((place for place in places if place.required_level > level), None)
        return ChillDisplay(current=None, next_place=next_place)

    selected = next((place for place in places if place.required_level == selected_level), None)
    if selected is not None and selected.required_level <= level:
        current = selected
        selected_locked = False
    else:
        current = unlocked[-1]
        selected_locked = selected is not None
    next_place = next((place for place in places if place.required_level > level), None)
    return ChillDisplay(current=current, next_place=next_place, selected_locked=selected_locked)


def format_chill_display(display: ChillDisplay) -> str:
    lines: list[str] = []
    if display.current is not None:
        lines.append(f"{format_chill_place_name(display.current)} (Lv.{display.current.required_level})")
        if display.current.tags:
            lines.append(" / ".join(display.current.tags))
        if display.current.description:
            lines.append(display.current.description)
    else:
        lines.append("まだ解放されていません")
    if display.next_place is not None:
        lines.append(f"次の解放: {format_chill_place_name(display.next_place)} Lv.{display.next_place.required_level}")
    if display.selected_locked:
        lines.append("選択中の場所は現在レベルでは未解放です")
    return "\n".join(lines)


def format_compact_chill_display(display: ChillDisplay) -> str:
    parts: list[str] = []
    if display.current is not None:
        parts.append(f"{format_chill_place_name(display.current)} (Lv.{display.current.required_level})")
    else:
        parts.append("まだ解放されていません")
    if display.next_place is not None:
        parts.append(f"次: {format_chill_place_name(display.next_place)} Lv.{display.next_place.required_level}")
    if display.selected_locked:
        parts.append("選択中は未解放")
    return " / ".join(parts)


def format_chill_list(places: tuple[ChillPlace, ...], level: int | None = None) -> str:
    lines: list[str] = []
    for place in places:
        if level is None:
            prefix = "-"
        elif place.required_level <= level:
            prefix = "✓"
        else:
            prefix = "□"
        lines.append(f"{prefix} Lv.{place.required_level} {format_chill_place_name(place)}")
    return "\n".join(lines)


def split_available_excluded_vc_ids(
    excluded_vc_ids: frozenset[int],
    available_voice_channel_ids: frozenset[int],
) -> tuple[frozenset[int], frozenset[int]]:
    kept = excluded_vc_ids & available_voice_channel_ids
    removed = excluded_vc_ids - kept
    return kept, removed


def _pick_image_attachment(attachments):
    for att in attachments:
        if (att.content_type or "").startswith("image/") or att.filename.lower().endswith(IMAGE_EXTENSIONS):
            return att
    return None


def build_intro_record(message: discord.Message) -> IntroRecord | None:
    if message.guild is None or not message.content.strip():
        return None
    image = _pick_image_attachment(message.attachments)
    return IntroRecord(
        guild_id=message.guild.id,
        user_id=message.author.id,
        message_id=message.id,
        channel_id=message.channel.id,
        content=message.content,
        jump_url=message.jump_url,
        image_url=image.url if image is not None else None,
        author_display_name=getattr(message.author, "display_name", message.author.name),
        author_avatar_url=message.author.display_avatar.url,
        created_at=message.created_at,
    )


def build_user_stats_url(guild_id: int, user_id: int) -> str | None:
    if not USER_STATS_SITE_BASE_URL or not USER_STATS_SITE_GUILD_ID:
        return None
    if str(guild_id) != USER_STATS_SITE_GUILD_ID:
        return None
    base_url = USER_STATS_SITE_BASE_URL.removesuffix("/u")
    return f"{base_url}/u/{user_id}/level?days=30"


def build_user_stats_view(stats_url: str | None) -> discord.ui.View | None:
    if stats_url is None:
        return None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="ユーザー統計を開く", url=stats_url))
    return view


class ChillPlaceSelect(discord.ui.Select):
    def __init__(self, bot, guild_id: int, user_id: int, current_level: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.current_level = current_level
        places = bot.get_chill_places(guild_id)
        options = [
            discord.SelectOption(
                label=format_chill_choice_name(place)[:100],
                value=str(place.required_level),
                description=truncate(place.description or "", 100) or None,
            )
            for place in places
            if place.required_level <= current_level
        ][:25]
        super().__init__(
            placeholder="自己紹介に表示するチル場所を選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_place = resolve_chill_place_selection(self.bot.get_chill_places(self.guild_id), self.values[0])
        if selected_place is None:
            await interaction.response.send_message("そのチル場所は設定されていません。", ephemeral=True)
            return
        level_info = await self.bot.get_user_level(self.guild_id, self.user_id)
        if level_info is None:
            await interaction.response.send_message("現在レベルを取得できませんでした。", ephemeral=True)
            return
        current_level, _ = level_info
        if selected_place.required_level > current_level:
            await interaction.response.send_message(
                (
                    f"{selected_place.name} は Lv.{selected_place.required_level} で解放されます。"
                    f"現在は Lv.{current_level} です。"
                ),
                ephemeral=True,
            )
            return
        try:
            await set_user_chill_level(
                self.bot.pool,
                self.guild_id,
                self.user_id,
                selected_place.required_level,
            )
        except Exception as e:
            log.error("set_user_chill_level failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        await sync_level_user_chill_place(
            self.bot.http_session,
            self.guild_id,
            self.user_id,
            selected_place.required_level,
        )
        self.bot.user_chill_levels.setdefault(self.guild_id, {})[self.user_id] = selected_place.required_level
        await interaction.response.edit_message(content="チル場所を設定しました。", view=None)
        await interaction.followup.send(f"チル場所を「{selected_place.name}」に設定しました。")


class ChillPlaceSelectView(discord.ui.View):
    def __init__(self, bot, guild_id: int, user_id: int, current_level: int) -> None:
        super().__init__(timeout=180)
        self.add_item(ChillPlaceSelect(bot, guild_id, user_id, current_level))


class DynamicChillPlaceButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"intro:chill:set:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="チル場所を設定",
                style=discord.ButtonStyle.secondary,
                custom_id=f"intro:chill:set:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        _interaction: discord.Interaction,
        _item: discord.ui.Item,
        match,
    ):
        return cls(guild_id=int(match["guild_id"]), user_id=int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("このチル場所を設定できるのは本人だけです。", ephemeral=True)
            return
        bot = interaction.client
        level_info = await bot.get_user_level(self.guild_id, self.user_id)
        if level_info is None:
            await interaction.response.send_message("現在レベルを取得できませんでした。", ephemeral=True)
            return
        current_level, _ = level_info
        if not any(place.required_level <= current_level for place in bot.get_chill_places(self.guild_id)):
            await interaction.response.send_message("選択できるチル場所がまだありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "自己紹介に表示するチル場所を選んでください。",
            view=ChillPlaceSelectView(bot, self.guild_id, self.user_id, current_level),
            ephemeral=True,
        )


class IntroActionView(discord.ui.View):
    def __init__(self, _bot, guild_id: int, user_id: int, stats_url: str | None) -> None:
        super().__init__(timeout=None)
        self.add_item(DynamicChillPlaceButton(guild_id, user_id))
        if stats_url is not None:
            self.add_item(discord.ui.Button(label="ユーザー統計を開く", url=stats_url))


def build_intro_view(bot, guild_id: int, user_id: int, stats_url: str | None) -> discord.ui.View:
    return IntroActionView(bot, guild_id, user_id, stats_url)


def serialize_chill_place(place: ChillPlace | None) -> dict | None:
    if place is None:
        return None
    return {
        "required_level": place.required_level,
        "name": place.name,
        "emoji": place.emoji,
        "display_name": format_chill_place_name(place),
        "tags": list(place.tags),
        "description": place.description,
    }


def serialize_chill_display(display: ChillDisplay | None) -> dict | None:
    if display is None:
        return None
    return {
        "current": serialize_chill_place(display.current),
        "next": serialize_chill_place(display.next_place),
        "selected_locked": display.selected_locked,
        "display_text": format_chill_display(display),
    }


def build_embed(
    member: discord.Member,
    intro: discord.Message,
    level_info: tuple[int, float] | None = None,
    chill_display: ChillDisplay | None = None,
    include_stats_link: bool = True,
) -> discord.Embed:
    embed = discord.Embed(
        description=truncate(intro.content, EMBED_DESCRIPTION_LIMIT),
        timestamp=intro.created_at,
        color=discord.Color.blurple(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.add_field(name="自己紹介", value=f"[ジャンプ]({intro.jump_url})", inline=True)
    stats_url = build_user_stats_url(member.guild.id, member.id) if include_stats_link else None
    if stats_url:
        embed.add_field(name="詳細", value=f"[30日間の統計を見る]({stats_url})", inline=True)
    img = _pick_image_attachment(intro.attachments)
    if img is not None:
        embed.set_image(url=img.url)
    if chill_display is not None:
        embed.add_field(name="チル場所", value=format_compact_chill_display(chill_display), inline=True)
    if level_info is not None:
        level, progress = level_info
        embed.set_footer(text=f"Lv. {level} ({int(progress * 100)}%)")
    return embed


async def _drain_cancelled(task: asyncio.Task) -> None:
    """完了済みなら何もしない。pending ならキャンセルして結果を回収する。"""
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def fetch_user_level(
    session: aiohttp.ClientSession | None, guild_id: int, user_id: int
) -> tuple[int, float] | None:
    """level-bot の API から total レベルと進捗を取得。失敗時は None。"""
    if session is None or not LEVEL_API_BASE:
        return None
    url = f"{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/users/{user_id}/levels"
    headers = {"Authorization": f"Bearer {EXTERNAL_API_KEY}"} if EXTERNAL_API_KEY else None
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=LEVEL_API_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError):
        return None
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(total, dict):
        return None
    level = total.get("level")
    progress = total.get("progress")
    if not isinstance(level, int) or not isinstance(progress, (int, float)):
        return None
    return level, float(progress)


def build_level_chill_api_headers() -> dict[str, str] | None:
    if not LEVEL_CHILL_API_KEY:
        return None
    return {"Authorization": f"Bearer {LEVEL_CHILL_API_KEY}"}


async def sync_level_user_chill_place(
    session: aiohttp.ClientSession | None,
    guild_id: int,
    user_id: int,
    required_level: int,
) -> bool:
    headers = build_level_chill_api_headers()
    if session is None or not LEVEL_API_BASE or headers is None:
        return False
    url = f"{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/users/{user_id}/chill-place"
    try:
        async with session.put(
            url,
            headers=headers,
            json={"required_level": required_level},
            timeout=aiohttp.ClientTimeout(total=LEVEL_API_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status < 400:
                return True
            log.warning("level chill user sync failed status=%s url=%s", resp.status, url)
            return False
    except (aiohttp.ClientError, TimeoutError):
        log.exception("level chill user sync failed url=%s", url)
        return False


async def clear_level_user_chill_place(
    session: aiohttp.ClientSession | None,
    guild_id: int,
    user_id: int,
) -> bool:
    headers = build_level_chill_api_headers()
    if session is None or not LEVEL_API_BASE or headers is None:
        return False
    url = f"{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/users/{user_id}/chill-place"
    try:
        async with session.delete(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=LEVEL_API_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status < 400:
                return True
            log.warning("level chill user clear failed status=%s url=%s", resp.status, url)
            return False
    except (aiohttp.ClientError, TimeoutError):
        log.exception("level chill user clear failed url=%s", url)
        return False


async def sync_level_guild_chill_place(
    session: aiohttp.ClientSession | None,
    guild_id: int,
    required_level: int,
    name: str,
    emoji: str | None,
) -> bool:
    headers = build_level_chill_api_headers()
    if session is None or not LEVEL_API_BASE or headers is None:
        return False
    url = f"{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/chill-places/{required_level}"
    try:
        async with session.put(
            url,
            headers=headers,
            json={"name": name, "emoji": emoji},
            timeout=aiohttp.ClientTimeout(total=LEVEL_API_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status < 400:
                return True
            log.warning("level chill guild sync failed status=%s url=%s", resp.status, url)
            return False
    except (aiohttp.ClientError, TimeoutError):
        log.exception("level chill guild sync failed url=%s", url)
        return False


async def remove_level_guild_chill_place(
    session: aiohttp.ClientSession | None,
    guild_id: int,
    required_level: int,
) -> bool:
    headers = build_level_chill_api_headers()
    if session is None or not LEVEL_API_BASE or headers is None:
        return False
    url = f"{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/chill-places/{required_level}"
    try:
        async with session.delete(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=LEVEL_API_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status < 400:
                return True
            log.warning("level chill guild remove failed status=%s url=%s", resp.status, url)
            return False
    except (aiohttp.ClientError, TimeoutError):
        log.exception("level chill guild remove failed url=%s", url)
        return False


def _make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.voice_states = True
    intents.message_content = True
    intents.members = True
    intents.guilds = True
    return intents


def _interaction_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    if isinstance(interaction.channel, discord.VoiceChannel):
        return interaction.channel
    if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None:
        return None
    channel = interaction.user.voice.channel
    return channel if isinstance(channel, discord.VoiceChannel) else None


def _voice_members(channel: discord.VoiceChannel) -> tuple[discord.Member, ...]:
    return tuple(member for member in channel.members if not member.bot)


def _next_voice_member_index(
    members: tuple[discord.Member, ...],
    cursor: int | None,
    preferred_user_id: int,
) -> int | None:
    if not members:
        return None
    if cursor is not None:
        return cursor % len(members)
    for index, member in enumerate(members):
        if member.id == preferred_user_id:
            return index
    return 0


class IntroBot(discord.Client):
    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(intents=_make_intents())
        self.tree = app_commands.CommandTree(self)
        self.pool = pool
        self.configs: dict[int, GuildConfig] = {}
        self.intro_message_id: dict[int, dict[int, int]] = {}
        # クールダウンは (guild, user, channel) 単位。VC を跨いだ移動では独立して効く
        self.last_posted_at: dict[int, dict[int, dict[int, float]]] = {}
        # 同一ユーザーの voice_state_update 並走中フラグ。クールダウンは
        # 送信成功時にのみ立てるため、await 中の二重投稿はこの集合で抑える
        self.in_flight: dict[int, set[int]] = {}
        self.http_session: aiohttp.ClientSession | None = None
        # (guild_id, user_id) -> (level_info, expiry_monotonic)
        self.level_cache: dict[tuple[int, int], tuple[tuple[int, float] | None, float]] = {}
        self.chill_place_overrides: dict[int, dict[int, ChillPlaceOverride]] = {}
        self.user_chill_levels: dict[int, dict[int, int]] = {}
        self.intro_command_cursor: dict[tuple[int, int], int] = {}
        register_commands(self.tree, self)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.add_dynamic_items(DynamicChillPlaceButton)
        self.configs = await load_all_configs(self.pool)
        self.chill_place_overrides = await load_all_chill_place_overrides(self.pool)
        self.user_chill_levels = await load_all_user_chill_levels(self.pool)
        log.info("loaded %d guild configs", len(self.configs))
        await self.tree.sync()

    async def close(self) -> None:
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def get_user_level(self, guild_id: int, user_id: int) -> tuple[int, float] | None:
        """TTL 付きキャッシュ経由でレベルを取得。連続 VC ジョインの再 fetch を抑える。"""
        now = time.monotonic()
        key = (guild_id, user_id)
        cached = self.level_cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        value = await fetch_user_level(self.http_session, guild_id, user_id)
        self.level_cache[key] = (value, now + LEVEL_CACHE_TTL_SECONDS)
        return value

    def get_chill_places(self, guild_id: int) -> tuple[ChillPlace, ...]:
        return build_chill_places(self.chill_place_overrides.get(guild_id))

    def get_user_chill_level(self, guild_id: int, user_id: int) -> int | None:
        return self.user_chill_levels.get(guild_id, {}).get(user_id)

    async def get_chill_display(
        self,
        guild_id: int,
        user_id: int,
        level_info: tuple[int, float] | None,
    ) -> ChillDisplay | None:
        places = self.get_chill_places(guild_id)
        selected_level = self.get_user_chill_level(guild_id, user_id)
        return resolve_chill_display(places, level_info, selected_level)

    def next_voice_intro_member(self, channel: discord.VoiceChannel, preferred_user_id: int) -> discord.Member | None:
        members = _voice_members(channel)
        key = (channel.guild.id, channel.id)
        index = _next_voice_member_index(members, self.intro_command_cursor.get(key), preferred_user_id)
        if index is None:
            return None
        self.intro_command_cursor[key] = (index + 1) % len(members)
        return members[index]

    def resolve_intro_channel(self, guild_id: int) -> discord.TextChannel | None:
        cfg = self.configs.get(guild_id)
        if cfg is None or cfg.intro_channel_id is None:
            return None
        ch = self.get_channel(cfg.intro_channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    def available_voice_channel_ids(self, guild: discord.Guild) -> frozenset[int]:
        member = guild.me
        ids: set[int] = set()
        for channel in guild.voice_channels:
            if member is not None and not channel.permissions_for(member).view_channel:
                continue
            ids.add(channel.id)
        return frozenset(ids)

    async def prune_unavailable_excluded_vcs(self, guild: discord.Guild) -> tuple[GuildConfig | None, frozenset[int]]:
        cfg = self.configs.get(guild.id)
        if cfg is None:
            return cfg, frozenset()

        updated, removed = await prune_unavailable_excluded_vcs_in_db(
            self.pool,
            guild.id,
            self.available_voice_channel_ids(guild),
        )
        if updated is not None:
            self.configs[guild.id] = updated
        if not removed:
            return updated, frozenset()
        log.info(
            "removed unavailable excluded VCs for guild=%s: %s",
            guild.id,
            ",".join(str(cid) for cid in sorted(removed)),
        )
        return updated, removed

    def _cache_get(self, guild_id: int, user_id: int) -> int | None:
        return self.intro_message_id.get(guild_id, {}).get(user_id)

    def _cache_set(self, guild_id: int, user_id: int, msg_id: int) -> None:
        self.intro_message_id.setdefault(guild_id, {})[user_id] = msg_id

    def _cache_pop(self, guild_id: int, user_id: int) -> None:
        if guild_id in self.intro_message_id:
            self.intro_message_id[guild_id].pop(user_id, None)

    async def find_intro_message(
        self, guild_id: int, intro_channel: discord.TextChannel, user_id: int
    ) -> discord.Message | None:
        cached = self._cache_get(guild_id, user_id)
        if cached is not None:
            try:
                msg = await intro_channel.fetch_message(cached)
                record = build_intro_record(msg)
                if record is not None:
                    await upsert_intro_record(self.pool, record)
                return msg
            except discord.NotFound:
                self._cache_pop(guild_id, user_id)
            except discord.Forbidden:
                raise
            except discord.HTTPException as e:
                log.warning("fetch_message failed for %s: %s; falling back to history scan", cached, e)

        async for msg in intro_channel.history(limit=INTRO_HISTORY_MAX_SCAN, oldest_first=False):
            if msg.author.id == user_id and msg.content.strip():
                self._cache_set(guild_id, user_id, msg.id)
                record = build_intro_record(msg)
                if record is not None:
                    await upsert_intro_record(self.pool, record)
                return msg
        return None

    async def sync_intro_channel_history(self, guild_id: int, intro_channel: discord.TextChannel) -> int:
        count = 0
        seen: set[int] = set()
        async for msg in intro_channel.history(limit=INTRO_HISTORY_MAX_SCAN, oldest_first=False):
            if msg.author.bot or not msg.content.strip() or msg.author.id in seen:
                continue
            record = build_intro_record(msg)
            if record is None:
                continue
            await upsert_intro_record(self.pool, record)
            self._cache_set(guild_id, msg.author.id, msg.id)
            seen.add(msg.author.id)
            count += 1
        return count

    async def on_ready(self) -> None:
        log.info(
            "[%s] ready (id=%s, guilds=%d)",
            self.user,
            getattr(self.user, "id", None),
            len(self.guilds),
        )
        for guild in self.guilds:
            try:
                await self.prune_unavailable_excluded_vcs(guild)
            except Exception as e:
                log.error("failed to prune excluded VCs for guild=%s: %s", guild.id, e)

    async def on_guild_channel_delete(self, channel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        try:
            await self.prune_unavailable_excluded_vcs(channel.guild)
        except Exception as e:
            log.error("failed to prune excluded VCs after channel delete guild=%s: %s", channel.guild.id, e)

    async def on_guild_channel_update(self, _before, after) -> None:
        if not isinstance(after, discord.VoiceChannel):
            return
        try:
            await self.prune_unavailable_excluded_vcs(after.guild)
        except Exception as e:
            log.error("failed to prune excluded VCs after channel update guild=%s: %s", after.guild.id, e)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        cfg = self.configs.get(message.guild.id)
        if cfg is None or cfg.intro_channel_id is None:
            return
        if message.channel.id != cfg.intro_channel_id:
            return
        if not message.content.strip():
            return
        self._cache_set(message.guild.id, message.author.id, message.id)
        record = build_intro_record(message)
        if record is not None:
            await upsert_intro_record(self.pool, record)

    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        cfg = self.configs.get(message.guild.id)
        if cfg is None or cfg.intro_channel_id is None:
            return
        if message.channel.id != cfg.intro_channel_id:
            return
        if self._cache_get(message.guild.id, message.author.id) == message.id:
            self._cache_pop(message.guild.id, message.author.id)
        await delete_intro_record(self.pool, message.guild.id, message.author.id, message.id)

    async def on_message_edit(self, _before: discord.Message, after: discord.Message) -> None:
        if after.author.bot or after.guild is None:
            return
        cfg = self.configs.get(after.guild.id)
        if cfg is None or cfg.intro_channel_id is None:
            return
        if after.channel.id != cfg.intro_channel_id:
            return
        if not after.content.strip():
            await delete_intro_record(self.pool, after.guild.id, after.author.id, after.id)
            if self._cache_get(after.guild.id, after.author.id) == after.id:
                self._cache_pop(after.guild.id, after.author.id)
            return
        record = build_intro_record(after)
        if record is not None:
            await upsert_intro_record(self.pool, record)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if after.channel is None or before.channel == after.channel:
            return
        if isinstance(after.channel, discord.StageChannel):
            return
        cfg = self.configs.get(member.guild.id)
        if cfg is None or cfg.intro_channel_id is None:
            return

        # in-flight 判定 → 登録は同期で完了させる(間に await を挟まない)。
        # これで自動部屋作成時の「ロビー参加 → 即移動」のような連続発火を
        # 二件目以降で弾ける
        in_flight = self.in_flight.setdefault(member.guild.id, set())
        if member.id in in_flight:
            return
        in_flight.add(member.id)

        # レベル取得は intro 検索と並行に走らせ、送信前に await で合流する。
        # 送信に至らない経路 (intro 未登録 / 除外 VC / クールダウン中) では
        # finally でキャンセルする。キャッシュヒット時は即完了する
        level_task = asyncio.create_task(self.get_user_level(member.guild.id, member.id))

        try:
            intro_channel = self.resolve_intro_channel(member.guild.id)
            if intro_channel is None:
                log.warning("intro channel not found for guild %s", member.guild.id)
                return

            try:
                intro = await self.find_intro_message(member.guild.id, intro_channel, member.id)
            except discord.Forbidden:
                log.error("Bot lacks permission to read %s", intro_channel)
                return

            # find_intro_message の await 中に後続の voice_state_update
            # (ロビー → 新部屋への move 通知) が処理されるので、その時点での
            # 現在地を投稿先にする。after.channel だとロビー宛になってしまう
            target = member.voice.channel if member.voice else None
            if target is None or isinstance(target, discord.StageChannel):
                return

            if target.id in cfg.excluded_vc_ids:
                return

            # クールダウンは投稿先 VC 単位で持つ。target 確定後にチェックすることで
            # ロビー → 別部屋へ移った場合も実際の投稿先に対して正しく判定できる
            now = time.time()
            last = self.last_posted_at.get(member.guild.id, {}).get(member.id, {}).get(target.id, 0.0)
            if now - last < cfg.cooldown_seconds:
                return

            if intro is None:
                # 管理者が指定したロールを持つメンバーには催促を送らない
                if cfg.nudge_exempt_role_ids and any(r.id in cfg.nudge_exempt_role_ids for r in member.roles):
                    return
                # 自己紹介がまだ書かれていないユーザーにはメンション付きで記入を促す
                try:
                    await target.send(
                        content=(
                            f"{member.mention} 自己紹介の検索に失敗しました。"
                            "自己紹介は必須となっておりますので、もしまだならご記入をお願いいたします。"
                        ),
                        allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
                    )
                except discord.Forbidden:
                    log.error("Bot lacks permission to send to %s", target)
                    return
                except discord.HTTPException as e:
                    log.error("failed to post to %s: %s", target, e)
                    return
                self.last_posted_at.setdefault(member.guild.id, {}).setdefault(member.id, {})[target.id] = now
                return

            level_info = await level_task
            chill_display = await self.get_chill_display(member.guild.id, member.id, level_info)
            try:
                stats_url = build_user_stats_url(member.guild.id, member.id)
                await target.send(
                    content=f"{member.mention} が参加しました",
                    embed=build_embed(
                        member,
                        intro,
                        level_info=level_info,
                        chill_display=chill_display,
                        include_stats_link=False,
                    ),
                    view=build_intro_view(self, member.guild.id, member.id, stats_url),
                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                )
            except discord.Forbidden:
                log.error("Bot lacks permission to send to %s", target)
                return
            except discord.HTTPException as e:
                log.error("failed to post to %s: %s", target, e)
                return

            # 送信が成功した場合のみクールダウンを確定させる
            self.last_posted_at.setdefault(member.guild.id, {}).setdefault(member.id, {})[target.id] = now
        finally:
            await _drain_cancelled(level_task)
            in_flight.discard(member.id)


def register_commands(tree: app_commands.CommandTree, bot: IntroBot) -> None:
    @tree.command(name="intro-help", description="intro-bot の使い方を表示")
    @app_commands.describe(topic="表示するヘルプの種類")
    @app_commands.choices(
        topic=[
            app_commands.Choice(name="基本", value="basic"),
            app_commands.Choice(name="チル場所", value="chill"),
            app_commands.Choice(name="管理者設定", value="config"),
        ]
    )
    @app_commands.guild_only()
    async def intro_help(
        interaction: discord.Interaction,
        topic: Literal["basic", "chill", "config"] = "basic",
    ) -> None:
        if topic == "chill":
            text = (
                "チル場所:\n"
                "- `/intro-chill list` 解放状況を見る\n"
                "- `/intro-chill set place:<場所名>` 自己紹介に出す場所を選ぶ\n"
                "- 自己紹介 embed の「チル場所を設定」ボタンからドロップダウンで選ぶ\n"
                "- `/intro-chill mine` 現在の選択を見る\n"
                "- `/intro-chill clear` 選択を解除して、現在レベルの最高解放場所を自動表示する"
            )
        elif topic == "config":
            text = (
                "管理者設定:\n"
                "- `/intro-config intro-channel` 自己紹介チャンネルを設定\n"
                "- `/intro-config cooldown` 自動投稿のクールダウンを設定\n"
                "- `/intro-config exclude-vc` 自動投稿しない VC を管理\n"
                "- `/intro-config nudge-exempt-role` 未記入催促を送らないロールを管理\n"
                "- `/intro-config chill-place` レベルごとのチル場所を管理\n"
                "- `/intro-config sync-intros` 既存自己紹介を API 用 DB に同期"
            )
        else:
            text = (
                "intro-bot:\n"
                "- `/intro` VC参加中は同じVCのメンバーを順番に見る / VC外では自分を見る\n"
                "- `/intro user:@user` 指定した人の自己紹介を見る\n"
                "- `/intros` VC にいる全員の自己紹介を見る\n"
                "- `/intro-chill list` レベルで解放されるチル場所を見る\n"
                "- `/intro-help topic:チル場所` チル場所コマンドの詳細を見る"
            )
        await interaction.response.send_message(text, ephemeral=True)

    @tree.command(name="intros", description="参加中の VC にいる全員の自己紹介を表示")
    @app_commands.guild_only()
    async def intros_cmd(interaction: discord.Interaction) -> None:
        channel = _interaction_voice_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "VC に参加している状態で実行してください。",
                ephemeral=True,
            )
            return
        members = list(_voice_members(channel))
        if not members:
            await interaction.response.send_message("VC に誰もいません。", ephemeral=True)
            return
        intro_channel = bot.resolve_intro_channel(interaction.guild_id)
        if intro_channel is None:
            await interaction.response.send_message(
                "自己紹介チャンネルが設定されていません。`/intro-config intro-channel` で設定してください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        pairs: list[tuple[discord.Member, discord.Message]] = []
        missing: list[discord.Member] = []
        for m in members:
            try:
                intro = await bot.find_intro_message(interaction.guild_id, intro_channel, m.id)
            except discord.Forbidden:
                await interaction.followup.send("自己紹介チャンネルの読み取り権限がありません。", ephemeral=True)
                return
            if intro is None:
                missing.append(m)
            else:
                pairs.append((m, intro))

        if not pairs:
            await interaction.followup.send("VC のメンバーに自己紹介はまだ投稿されていません。")
            return

        level_infos = await asyncio.gather(*(bot.get_user_level(interaction.guild_id, m.id) for m, _ in pairs))
        chill_displays = [
            await bot.get_chill_display(interaction.guild_id, m.id, lv)
            for (m, _), lv in zip(pairs, level_infos, strict=True)
        ]
        embeds = [
            build_embed(m, intro, level_info=lv, chill_display=chill)
            for ((m, intro), lv, chill) in zip(pairs, level_infos, chill_displays, strict=True)
        ]
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i : i + 10])
        if missing:
            names = ", ".join(m.display_name for m in missing)
            await interaction.followup.send(f"自己紹介未登録: {names}", ephemeral=True)

    @tree.command(name="intro", description="自己紹介を表示(省略時はVCメンバー順/自分)")
    @app_commands.describe(user="自己紹介を表示するメンバー。省略時はVCメンバー順、VC外では自分")
    @app_commands.guild_only()
    async def intro_cmd(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if user is None:
            voice_channel = _interaction_voice_channel(interaction)
            target = (
                bot.next_voice_intro_member(voice_channel, interaction.user.id)
                if voice_channel is not None
                else interaction.user
            )
            if target is None:
                await interaction.response.send_message("VC に誰もいません。", ephemeral=True)
                return
        else:
            target = user
        if target.bot:
            await interaction.response.send_message("Bot の自己紹介はありません。", ephemeral=True)
            return
        intro_channel = bot.resolve_intro_channel(interaction.guild_id)
        if intro_channel is None:
            await interaction.response.send_message("自己紹介チャンネルが設定されていません。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            intro_msg = await bot.find_intro_message(interaction.guild_id, intro_channel, target.id)
        except discord.Forbidden:
            await interaction.followup.send("自己紹介チャンネルの読み取り権限がありません。", ephemeral=True)
            return
        if intro_msg is None:
            await interaction.followup.send(
                f"{target.display_name} さんの自己紹介はまだ投稿されていません。",
                ephemeral=True,
            )
            return
        level_info = await bot.get_user_level(interaction.guild_id, target.id)
        chill_display = await bot.get_chill_display(interaction.guild_id, target.id, level_info)
        stats_url = build_user_stats_url(interaction.guild_id, target.id)
        await interaction.followup.send(
            embed=build_embed(
                target,
                intro_msg,
                level_info=level_info,
                chill_display=chill_display,
                include_stats_link=False,
            ),
            view=build_intro_view(bot, interaction.guild_id, target.id, stats_url),
        )

    chill_group = app_commands.Group(
        name="intro-chill",
        description="自己紹介に表示するチル場所を選択",
        guild_only=True,
    )

    @chill_group.command(name="list", description="レベルごとのチル場所と自分の解放状況を表示")
    async def chill_list(interaction: discord.Interaction) -> None:
        level_info = await bot.get_user_level(interaction.guild_id, interaction.user.id)
        level = level_info[0] if level_info is not None else None
        text = format_chill_list(bot.get_chill_places(interaction.guild_id), level=level)
        if level is None:
            text = "現在レベルを取得できませんでした。場所一覧のみ表示します。\n" + text
        text = truncate(text, DISCORD_MESSAGE_LIMIT)
        await interaction.response.send_message(text, ephemeral=True)

    async def chill_place_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        level_info = await bot.get_user_level(interaction.guild_id, interaction.user.id)
        current_level = level_info[0] if level_info is not None else None
        return build_chill_place_choices(bot.get_chill_places(interaction.guild_id), current, current_level)

    @chill_group.command(name="set", description="自己紹介に表示するチル場所を選択")
    @app_commands.describe(place="チル場所の名前")
    @app_commands.autocomplete(place=chill_place_autocomplete)
    async def chill_set(
        interaction: discord.Interaction,
        place: str,
    ) -> None:
        level_info = await bot.get_user_level(interaction.guild_id, interaction.user.id)
        if level_info is None:
            await interaction.response.send_message(
                "現在レベルを取得できないため、チル場所を選択できません。",
                ephemeral=True,
            )
            return
        current_level, _ = level_info
        places = bot.get_chill_places(interaction.guild_id)
        selected_place = resolve_chill_place_selection(places, place)
        if selected_place is None:
            await interaction.response.send_message(
                "そのチル場所は設定されていません。候補から場所名を選んでください。",
                ephemeral=True,
            )
            return
        if selected_place.required_level > current_level:
            await interaction.response.send_message(
                (
                    f"{selected_place.name} は Lv.{selected_place.required_level} で解放されます。"
                    f"現在は Lv.{current_level} です。"
                ),
                ephemeral=True,
            )
            return
        try:
            await set_user_chill_level(
                bot.pool,
                interaction.guild_id,
                interaction.user.id,
                selected_place.required_level,
            )
        except Exception as e:
            log.error("set_user_chill_level failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        await sync_level_user_chill_place(
            bot.http_session,
            interaction.guild_id,
            interaction.user.id,
            selected_place.required_level,
        )
        bot.user_chill_levels.setdefault(interaction.guild_id, {})[interaction.user.id] = selected_place.required_level
        await interaction.response.send_message(
            f"チル場所を「{selected_place.name}」に設定しました。",
        )

    @chill_group.command(name="clear", description="チル場所の選択を解除")
    async def chill_clear(interaction: discord.Interaction) -> None:
        try:
            await clear_user_chill_level(bot.pool, interaction.guild_id, interaction.user.id)
        except Exception as e:
            log.error("clear_user_chill_level failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        if interaction.guild_id in bot.user_chill_levels:
            bot.user_chill_levels[interaction.guild_id].pop(interaction.user.id, None)
        await clear_level_user_chill_place(
            bot.http_session,
            interaction.guild_id,
            interaction.user.id,
        )
        await interaction.response.send_message(
            "チル場所の選択を解除しました。現在レベルで解放済みの一番上の場所を自動表示します。",
        )

    @chill_group.command(name="mine", description="現在のチル場所を表示")
    async def chill_mine(interaction: discord.Interaction) -> None:
        level_info = await bot.get_user_level(interaction.guild_id, interaction.user.id)
        display = await bot.get_chill_display(interaction.guild_id, interaction.user.id, level_info)
        if display is None:
            await interaction.response.send_message("現在レベルを取得できませんでした。", ephemeral=True)
            return
        await interaction.response.send_message(format_chill_display(display))

    config_group = app_commands.Group(
        name="intro-config",
        description="intro-bot の設定を管理(管理者限定)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def _config_after_excluded_vc_pruning(
        interaction: discord.Interaction,
    ) -> tuple[GuildConfig | None, frozenset[int]] | None:
        if interaction.guild is None:
            return bot.configs.get(interaction.guild_id), frozenset()
        try:
            return await bot.prune_unavailable_excluded_vcs(interaction.guild)
        except Exception as e:
            log.error("prune_unavailable_excluded_vcs failed: %s", e)
            await interaction.response.send_message("除外 VC の自動整理に失敗しました。", ephemeral=True)
            return None

    def _format_excluded_vc_prune_note(removed: frozenset[int]) -> str:
        if not removed:
            return ""
        return f"\n\n削除済みまたはアクセス不可の除外 VC を {len(removed)} 件、自動解除しました。"

    @config_group.command(name="show", description="現在のギルド設定を表示")
    async def show(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        result = await _config_after_excluded_vc_pruning(interaction)
        if result is None:
            return
        cfg, removed_excluded_vcs = result
        if cfg is None:
            text = (
                f"intro_channel: 未設定\n"
                f"cooldown_seconds: {DEFAULT_COOLDOWN_SECONDS}(デフォルト)\n"
                "excluded_vcs: なし\n"
                "nudge_exempt_roles: なし"
            )
        else:
            channel_text = f"<#{cfg.intro_channel_id}>" if cfg.intro_channel_id else "未設定"
            excluded_text = ", ".join(f"<#{cid}>" for cid in cfg.excluded_vc_ids) if cfg.excluded_vc_ids else "なし"
            exempt_text = (
                ", ".join(f"<@&{rid}>" for rid in cfg.nudge_exempt_role_ids) if cfg.nudge_exempt_role_ids else "なし"
            )
            text = (
                f"intro_channel: {channel_text}\n"
                f"cooldown_seconds: {cfg.cooldown_seconds}\n"
                f"excluded_vcs: {excluded_text}\n"
                f"nudge_exempt_roles: {exempt_text}"
            )
        text += _format_excluded_vc_prune_note(removed_excluded_vcs)
        await interaction.response.send_message(text, ephemeral=True)

    @config_group.command(name="intro-channel", description="自己紹介チャンネルを設定")
    @app_commands.describe(channel="自己紹介チャンネル")
    async def set_intro_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        try:
            cfg = await upsert_intro_channel(bot.pool, interaction.guild_id, channel.id)
        except Exception as e:
            log.error("upsert_intro_channel failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        bot.intro_message_id.pop(interaction.guild_id, None)
        try:
            synced = await bot.sync_intro_channel_history(interaction.guild_id, channel)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"自己紹介チャンネルを {channel.mention} に設定しましたが、履歴の読み取り権限がありません。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            log.warning("initial intro sync failed for guild=%s channel=%s: %s", interaction.guild_id, channel.id, e)
            await interaction.response.send_message(
                f"自己紹介チャンネルを {channel.mention} に設定しましたが、既存自己紹介の同期に失敗しました。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"自己紹介チャンネルを {channel.mention} に設定しました。既存自己紹介を {synced} 件同期しました。",
            ephemeral=True,
        )

    @config_group.command(name="cooldown", description="クールダウン秒数を変更")
    @app_commands.describe(seconds="1〜86400 の整数(秒)")
    async def set_cooldown(
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 1, 86400],
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        try:
            cfg = await upsert_cooldown(bot.pool, interaction.guild_id, seconds)
        except Exception as e:
            log.error("upsert_cooldown failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        await interaction.response.send_message(
            f"クールダウンを {seconds} 秒に更新しました。",
            ephemeral=True,
        )

    @config_group.command(name="sync-intros", description="自己紹介チャンネルの既存投稿を API 用 DB に同期")
    async def sync_intros(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        intro_channel = bot.resolve_intro_channel(interaction.guild_id)
        if intro_channel is None:
            await interaction.response.send_message("自己紹介チャンネルが設定されていません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await bot.sync_intro_channel_history(interaction.guild_id, intro_channel)
        except discord.Forbidden:
            await interaction.followup.send("自己紹介チャンネルの読み取り権限がありません。", ephemeral=True)
            return
        except discord.HTTPException as e:
            log.warning("manual intro sync failed for guild=%s: %s", interaction.guild_id, e)
            await interaction.followup.send("同期に失敗しました。", ephemeral=True)
            return
        await interaction.followup.send(f"既存自己紹介を {synced} 件同期しました。", ephemeral=True)

    chill_place_group = app_commands.Group(
        name="chill-place",
        description="レベルごとのチル場所を管理",
        parent=config_group,
    )

    @chill_place_group.command(name="add", description="レベルごとのチル場所を追加・変更")
    @app_commands.describe(level="解放レベル", name="場所名", emoji="表示する絵文字。標準絵文字推奨")
    async def chill_place_add(
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 1000],
        name: app_commands.Range[str, 1, 80],
        emoji: app_commands.Range[str, 1, 40] | None = None,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        clean_name = name.strip()
        clean_emoji = emoji.strip() if emoji is not None else None
        if clean_emoji == "":
            clean_emoji = None
        if not clean_name:
            await interaction.response.send_message("場所名を入力してください。", ephemeral=True)
            return
        try:
            await upsert_chill_place(bot.pool, interaction.guild_id, level, clean_name, clean_emoji)
        except Exception as e:
            log.error("upsert_chill_place failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.chill_place_overrides.setdefault(interaction.guild_id, {})[level] = ChillPlaceOverride(
            name=clean_name,
            emoji=clean_emoji,
        )
        await sync_level_guild_chill_place(
            bot.http_session,
            interaction.guild_id,
            level,
            clean_name,
            clean_emoji,
        )
        place = next(p for p in bot.get_chill_places(interaction.guild_id) if p.required_level == level)
        await interaction.response.send_message(
            f"Lv.{level} のチル場所を「{format_chill_place_name(place)}」に設定しました。",
            ephemeral=True,
        )

    @chill_place_group.command(name="remove", description="追加・変更したチル場所を削除")
    @app_commands.describe(level="削除する解放レベル")
    async def chill_place_remove(
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 1000],
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        try:
            removed = await remove_chill_place(bot.pool, interaction.guild_id, level)
        except Exception as e:
            log.error("remove_chill_place failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        if interaction.guild_id in bot.chill_place_overrides:
            bot.chill_place_overrides[interaction.guild_id].pop(level, None)
        await remove_level_guild_chill_place(
            bot.http_session,
            interaction.guild_id,
            level,
        )
        if not removed:
            await interaction.response.send_message(
                "カスタム設定はありませんでした。プリセットの場所はそのまま表示されます。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Lv.{level} のカスタム設定を削除しました。",
            ephemeral=True,
        )

    @chill_place_group.command(name="list", description="レベルごとのチル場所一覧を表示")
    async def chill_place_list(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        text = truncate(format_chill_list(bot.get_chill_places(interaction.guild_id)), DISCORD_MESSAGE_LIMIT)
        await interaction.response.send_message(
            text,
            ephemeral=True,
        )

    exclude_group = app_commands.Group(
        name="exclude-vc",
        description="自動投稿しない VC を管理",
        parent=config_group,
    )

    @exclude_group.command(name="add", description="自動投稿しない VC を追加")
    @app_commands.describe(channel="自動投稿の対象から外す VC")
    async def exclude_add(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        result = await _config_after_excluded_vc_pruning(interaction)
        if result is None:
            return
        existing, removed_excluded_vcs = result
        if existing is not None and channel.id in existing.excluded_vc_ids:
            await interaction.response.send_message(
                f"{channel.mention} は既に除外されています。{_format_excluded_vc_prune_note(removed_excluded_vcs)}",
                ephemeral=True,
            )
            return
        try:
            cfg = await add_excluded_vc(bot.pool, interaction.guild_id, channel.id)
        except Exception as e:
            log.error("add_excluded_vc failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        await interaction.response.send_message(
            f"{channel.mention} を自動投稿の対象から外しました。{_format_excluded_vc_prune_note(removed_excluded_vcs)}",
            ephemeral=True,
        )

    @exclude_group.command(name="remove", description="自動投稿しない VC から除外を解除")
    @app_commands.describe(channel="除外を解除する VC")
    async def exclude_remove(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        result = await _config_after_excluded_vc_pruning(interaction)
        if result is None:
            return
        existing, removed_excluded_vcs = result
        if existing is None or channel.id not in existing.excluded_vc_ids:
            await interaction.response.send_message(
                f"{channel.mention} は除外リストにありません。{_format_excluded_vc_prune_note(removed_excluded_vcs)}",
                ephemeral=True,
            )
            return
        try:
            cfg = await remove_excluded_vc(bot.pool, interaction.guild_id, channel.id)
        except Exception as e:
            log.error("remove_excluded_vc failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        if cfg is None:
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        await interaction.response.send_message(
            f"{channel.mention} の除外を解除しました。{_format_excluded_vc_prune_note(removed_excluded_vcs)}",
            ephemeral=True,
        )

    @exclude_group.command(name="list", description="自動投稿しない VC の一覧を表示")
    async def exclude_list(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        result = await _config_after_excluded_vc_pruning(interaction)
        if result is None:
            return
        cfg, removed_excluded_vcs = result
        if cfg is None or not cfg.excluded_vc_ids:
            await interaction.response.send_message(
                "除外 VC はありません。" + _format_excluded_vc_prune_note(removed_excluded_vcs),
                ephemeral=True,
            )
            return
        text = "除外 VC:\n" + "\n".join(f"- <#{cid}>" for cid in cfg.excluded_vc_ids)
        text += _format_excluded_vc_prune_note(removed_excluded_vcs)
        await interaction.response.send_message(text, ephemeral=True)

    nudge_exempt_group = app_commands.Group(
        name="nudge-exempt-role",
        description="自己紹介未記入でも催促を送らないロールを管理",
        parent=config_group,
    )

    @nudge_exempt_group.command(name="add", description="催促を送らないロールを追加")
    @app_commands.describe(role="催促の対象から外すロール")
    async def nudge_exempt_add(
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        existing = bot.configs.get(interaction.guild_id)
        if existing is not None and role.id in existing.nudge_exempt_role_ids:
            await interaction.response.send_message(
                f"{role.mention} は既に除外されています。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            cfg = await add_nudge_exempt_role(bot.pool, interaction.guild_id, role.id)
        except Exception as e:
            log.error("add_nudge_exempt_role failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        await interaction.response.send_message(
            f"{role.mention} を催促の対象から外しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nudge_exempt_group.command(name="remove", description="催促ロール除外を解除")
    @app_commands.describe(role="除外を解除するロール")
    async def nudge_exempt_remove(
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        existing = bot.configs.get(interaction.guild_id)
        if existing is None or role.id not in existing.nudge_exempt_role_ids:
            await interaction.response.send_message(
                f"{role.mention} は除外リストにありません。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            cfg = await remove_nudge_exempt_role(bot.pool, interaction.guild_id, role.id)
        except Exception as e:
            log.error("remove_nudge_exempt_role failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        if cfg is None:
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        bot.configs[interaction.guild_id] = cfg
        await interaction.response.send_message(
            f"{role.mention} の除外を解除しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nudge_exempt_group.command(name="list", description="催促を送らないロールの一覧を表示")
    async def nudge_exempt_list(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        cfg = bot.configs.get(interaction.guild_id)
        if cfg is None or not cfg.nudge_exempt_role_ids:
            await interaction.response.send_message("除外ロールはありません。", ephemeral=True)
            return
        text = "催促を送らないロール:\n" + "\n".join(f"- <@&{rid}>" for rid in cfg.nudge_exempt_role_ids)
        await interaction.response.send_message(
            text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    tree.add_command(chill_group)
    tree.add_command(config_group)


def _ensure_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)


async def main() -> None:
    if not TOKENS:
        raise RuntimeError("set DISCORD_TOKEN (single) or DISCORD_TOKENS (comma-separated)")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=max(4, 2 * len(TOKENS)))
    await init_schema(pool)
    log.info("starting %d client(s)", len(TOKENS))
    clients = [IntroBot(pool) for _ in TOKENS]
    try:
        await asyncio.gather(*(c.start(t) for c, t in zip(clients, TOKENS, strict=True)))
    finally:
        for c in clients:
            if not c.is_closed():
                await c.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
