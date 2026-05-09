import asyncio
import logging
import os
import time
from dataclasses import dataclass

import asyncpg
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

_TOKENS_RAW = os.environ.get("DISCORD_TOKENS") or os.environ.get("DISCORD_TOKEN")
if not _TOKENS_RAW:
    raise RuntimeError("set DISCORD_TOKEN (single) or DISCORD_TOKENS (comma-separated)")
TOKENS = [t.strip() for t in _TOKENS_RAW.split(",") if t.strip()]
DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))
INTRO_HISTORY_MAX_SCAN = int(os.environ.get("INTRO_HISTORY_MAX_SCAN", "5000"))

EMBED_DESCRIPTION_LIMIT = 4000
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


async def load_all_configs(pool: asyncpg.Pool) -> dict[int, GuildConfig]:
    async with pool.acquire() as con:
        rows = await con.fetch(f"SELECT {_SELECT_COLUMNS} FROM bot_config")
    return {r["guild_id"]: _row_to_config(r) for r in rows}


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


def _pick_image_attachment(attachments):
    for att in attachments:
        if (att.content_type or "").startswith("image/") or att.filename.lower().endswith(IMAGE_EXTENSIONS):
            return att
    return None


def build_embed(member: discord.Member, intro: discord.Message) -> discord.Embed:
    embed = discord.Embed(
        description=truncate(intro.content, EMBED_DESCRIPTION_LIMIT),
        timestamp=intro.created_at,
        color=discord.Color.blurple(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.add_field(name="自己紹介", value=f"[ジャンプ]({intro.jump_url})", inline=True)
    img = _pick_image_attachment(intro.attachments)
    if img is not None:
        embed.set_image(url=img.url)
    return embed


def _make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.voice_states = True
    intents.message_content = True
    intents.members = True
    intents.guilds = True
    return intents


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
        register_commands(self.tree, self)

    async def setup_hook(self) -> None:
        self.configs = await load_all_configs(self.pool)
        log.info("loaded %d guild configs", len(self.configs))
        await self.tree.sync()

    def resolve_intro_channel(self, guild_id: int) -> discord.TextChannel | None:
        cfg = self.configs.get(guild_id)
        if cfg is None or cfg.intro_channel_id is None:
            return None
        ch = self.get_channel(cfg.intro_channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

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
                return await intro_channel.fetch_message(cached)
            except discord.NotFound:
                self._cache_pop(guild_id, user_id)
            except discord.Forbidden:
                raise
            except discord.HTTPException as e:
                log.warning("fetch_message failed for %s: %s; falling back to history scan", cached, e)

        async for msg in intro_channel.history(limit=INTRO_HISTORY_MAX_SCAN, oldest_first=False):
            if msg.author.id == user_id and msg.content.strip():
                self._cache_set(guild_id, user_id, msg.id)
                return msg
        return None

    async def on_ready(self) -> None:
        log.info(
            "[%s] ready (id=%s, guilds=%d)",
            self.user,
            getattr(self.user, "id", None),
            len(self.guilds),
        )

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

            try:
                await target.send(
                    content=f"{member.mention} が参加しました",
                    embed=build_embed(member, intro),
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
            in_flight.discard(member.id)


def register_commands(tree: app_commands.CommandTree, bot: "IntroBot") -> None:
    @tree.command(name="intros", description="この VC にいる全員の自己紹介を表示")
    @app_commands.guild_only()
    async def intros_cmd(interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "このコマンドは VC のテキストチャットで実行してください。",
                ephemeral=True,
            )
            return
        members = [m for m in channel.members if not m.bot]
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
        embeds: list[discord.Embed] = []
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
                embeds.append(build_embed(m, intro))

        if not embeds:
            await interaction.followup.send("VC のメンバーに自己紹介はまだ投稿されていません。")
            return
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i : i + 10])
        if missing:
            names = ", ".join(m.display_name for m in missing)
            await interaction.followup.send(f"自己紹介未登録: {names}", ephemeral=True)

    @tree.command(name="intro", description="指定したメンバーの自己紹介を表示")
    @app_commands.describe(user="自己紹介を表示するメンバー")
    @app_commands.guild_only()
    async def intro_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
        if user.bot:
            await interaction.response.send_message("Bot の自己紹介はありません。", ephemeral=True)
            return
        intro_channel = bot.resolve_intro_channel(interaction.guild_id)
        if intro_channel is None:
            await interaction.response.send_message("自己紹介チャンネルが設定されていません。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            intro_msg = await bot.find_intro_message(interaction.guild_id, intro_channel, user.id)
        except discord.Forbidden:
            await interaction.followup.send("自己紹介チャンネルの読み取り権限がありません。", ephemeral=True)
            return
        if intro_msg is None:
            await interaction.followup.send(
                f"{user.display_name} さんの自己紹介はまだ投稿されていません。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=build_embed(user, intro_msg))

    config_group = app_commands.Group(
        name="intro-config",
        description="intro-bot の設定を管理(管理者限定)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @config_group.command(name="show", description="現在のギルド設定を表示")
    async def show(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        cfg = bot.configs.get(interaction.guild_id)
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
        await interaction.response.send_message(
            f"自己紹介チャンネルを {channel.mention} に設定しました。",
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
        existing = bot.configs.get(interaction.guild_id)
        if existing is not None and channel.id in existing.excluded_vc_ids:
            await interaction.response.send_message(
                f"{channel.mention} は既に除外されています。",
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
            f"{channel.mention} を自動投稿の対象から外しました。",
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
        existing = bot.configs.get(interaction.guild_id)
        if existing is None or channel.id not in existing.excluded_vc_ids:
            await interaction.response.send_message(
                f"{channel.mention} は除外リストにありません。",
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
            f"{channel.mention} の除外を解除しました。",
            ephemeral=True,
        )

    @exclude_group.command(name="list", description="自動投稿しない VC の一覧を表示")
    async def exclude_list(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        cfg = bot.configs.get(interaction.guild_id)
        if cfg is None or not cfg.excluded_vc_ids:
            await interaction.response.send_message("除外 VC はありません。", ephemeral=True)
            return
        text = "除外 VC:\n" + "\n".join(f"- <#{cid}>" for cid in cfg.excluded_vc_ids)
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

    tree.add_command(config_group)


def _ensure_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)


async def main() -> None:
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
