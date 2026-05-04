import logging
import os
import time
from dataclasses import dataclass

import asyncpg
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
ENV_INTRO_CHANNEL_ID = int(os.environ["INTRO_CHANNEL_ID"]) if os.environ.get("INTRO_CHANNEL_ID") else None
ENV_COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "3600"))
INTRO_HISTORY_MAX_SCAN = int(os.environ.get("INTRO_HISTORY_MAX_SCAN", "5000"))

EMBED_DESCRIPTION_LIMIT = 4000
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intro-bot")

GUILD_OBJECT = discord.Object(id=GUILD_ID)


@dataclass
class RuntimeConfig:
    intro_channel_id: int
    cooldown_seconds: int


runtime_config: RuntimeConfig
db_pool: asyncpg.Pool
last_posted_at: dict[int, float] = {}
intro_message_id: dict[int, int] = {}


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as con:
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                intro_channel_id BIGINT      NOT NULL,
                cooldown_seconds INTEGER     NOT NULL CHECK (cooldown_seconds BETWEEN 1 AND 86400),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


async def load_or_bootstrap_config(pool: asyncpg.Pool) -> RuntimeConfig:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT intro_channel_id, cooldown_seconds FROM bot_config WHERE id = 1")
        if row is not None:
            return RuntimeConfig(
                intro_channel_id=row["intro_channel_id"],
                cooldown_seconds=row["cooldown_seconds"],
            )
        if ENV_INTRO_CHANNEL_ID is None:
            raise RuntimeError(
                "bot_config is empty and INTRO_CHANNEL_ID env var is not set; "
                "set INTRO_CHANNEL_ID for first-time bootstrap"
            )
        await con.execute(
            "INSERT INTO bot_config (id, intro_channel_id, cooldown_seconds) "
            "VALUES (1, $1, $2) ON CONFLICT (id) DO NOTHING",
            ENV_INTRO_CHANNEL_ID,
            ENV_COOLDOWN_SECONDS,
        )
        row = await con.fetchrow("SELECT intro_channel_id, cooldown_seconds FROM bot_config WHERE id = 1")
        return RuntimeConfig(
            intro_channel_id=row["intro_channel_id"],
            cooldown_seconds=row["cooldown_seconds"],
        )


async def update_intro_channel(pool: asyncpg.Pool, channel_id: int) -> None:
    global runtime_config
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE bot_config SET intro_channel_id = $1, updated_at = NOW() WHERE id = 1",
            channel_id,
        )
    runtime_config = RuntimeConfig(
        intro_channel_id=channel_id,
        cooldown_seconds=runtime_config.cooldown_seconds,
    )
    intro_message_id.clear()


async def update_cooldown(pool: asyncpg.Pool, seconds: int) -> None:
    global runtime_config
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE bot_config SET cooldown_seconds = $1, updated_at = NOW() WHERE id = 1",
            seconds,
        )
    runtime_config = RuntimeConfig(
        intro_channel_id=runtime_config.intro_channel_id,
        cooldown_seconds=seconds,
    )


async def find_intro_message(intro_channel: discord.TextChannel, user_id: int) -> discord.Message | None:
    cached = intro_message_id.get(user_id)
    if cached is not None:
        try:
            return await intro_channel.fetch_message(cached)
        except discord.NotFound:
            intro_message_id.pop(user_id, None)
        except discord.Forbidden:
            raise
        except discord.HTTPException as e:
            log.warning("fetch_message failed for %s: %s; skipping history scan", cached, e)
            return None

    async for msg in intro_channel.history(limit=INTRO_HISTORY_MAX_SCAN, oldest_first=False):
        if msg.author.id == user_id and msg.content.strip():
            intro_message_id[user_id] = msg.id
            return msg
    return None


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


intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
intents.members = True
intents.guilds = True


class MeishiBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        global db_pool, runtime_config
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
        await init_schema(db_pool)
        runtime_config = await load_or_bootstrap_config(db_pool)
        log.info(
            "loaded config: intro_channel_id=%s cooldown_seconds=%s",
            runtime_config.intro_channel_id,
            runtime_config.cooldown_seconds,
        )
        register_commands(self.tree)
        await self.tree.sync(guild=GUILD_OBJECT)


bot = MeishiBot()


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, getattr(bot.user, "id", None))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None or message.guild.id != GUILD_ID:
        return
    if message.channel.id != runtime_config.intro_channel_id:
        return
    if not message.content.strip():
        return
    intro_message_id[message.author.id] = message.id


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.guild is None or message.guild.id != GUILD_ID:
        return
    if message.channel.id != runtime_config.intro_channel_id:
        return
    if intro_message_id.get(message.author.id) == message.id:
        intro_message_id.pop(message.author.id, None)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot or member.guild.id != GUILD_ID:
        return
    if after.channel is None or before.channel == after.channel:
        return
    if isinstance(after.channel, discord.StageChannel):
        return

    now = time.time()
    if now - last_posted_at.get(member.id, 0.0) < runtime_config.cooldown_seconds:
        return

    intro_channel = _resolve_intro_channel()
    if intro_channel is None:
        log.warning("intro channel not found or wrong type: %s", runtime_config.intro_channel_id)
        return

    try:
        intro = await find_intro_message(intro_channel, member.id)
    except discord.Forbidden:
        log.error("Bot lacks permission to read %s", intro_channel)
        return

    if intro is None:
        return

    try:
        await after.channel.send(
            content=f"{member.mention} が参加しました",
            embed=build_embed(member, intro),
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
    except discord.Forbidden:
        log.error("Bot lacks permission to send to %s", after.channel)
        return
    except discord.HTTPException as e:
        log.error("failed to post to %s: %s", after.channel, e)
        return

    last_posted_at[member.id] = now


def register_commands(tree: app_commands.CommandTree) -> None:
    group = app_commands.Group(
        name="intro-config",
        description="intro-bot の設定を管理(管理者限定)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @group.command(name="show", description="現在の設定を表示")
    async def show(interaction: discord.Interaction) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        await interaction.response.send_message(
            f"intro_channel: <#{runtime_config.intro_channel_id}>\ncooldown_seconds: {runtime_config.cooldown_seconds}",
            ephemeral=True,
        )

    @group.command(name="intro-channel", description="自己紹介チャンネルを変更")
    @app_commands.describe(channel="新しい自己紹介チャンネル")
    async def set_intro_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        try:
            await update_intro_channel(db_pool, channel.id)
        except Exception as e:
            log.error("update_intro_channel failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"自己紹介チャンネルを {channel.mention} に更新しました。",
            ephemeral=True,
        )

    @group.command(name="cooldown", description="クールダウン秒数を変更")
    @app_commands.describe(seconds="1〜86400 の整数(秒)")
    async def set_cooldown_cmd(
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 1, 86400],
    ) -> None:
        if not _ensure_admin(interaction):
            return await _deny(interaction)
        try:
            await update_cooldown(db_pool, seconds)
        except Exception as e:
            log.error("update_cooldown failed: %s", e)
            await interaction.response.send_message("更新に失敗しました。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"クールダウンを {seconds} 秒に更新しました。",
            ephemeral=True,
        )

    tree.add_command(group, guild=GUILD_OBJECT)

    @tree.command(
        name="intros",
        description="この VC にいる全員の自己紹介を表示",
        guild=GUILD_OBJECT,
    )
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
        intro_channel = _resolve_intro_channel()
        if intro_channel is None:
            await interaction.response.send_message("自己紹介チャンネルが見つかりません。", ephemeral=True)
            return

        await interaction.response.defer()
        embeds: list[discord.Embed] = []
        missing: list[discord.Member] = []
        for member in members:
            try:
                intro = await find_intro_message(intro_channel, member.id)
            except discord.Forbidden:
                await interaction.followup.send("自己紹介チャンネルの読み取り権限がありません。", ephemeral=True)
                return
            if intro is None:
                missing.append(member)
            else:
                embeds.append(build_embed(member, intro))

        if not embeds:
            await interaction.followup.send("VC のメンバーに自己紹介はまだ投稿されていません。")
            return
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i : i + 10])
        if missing:
            names = ", ".join(m.display_name for m in missing)
            await interaction.followup.send(f"自己紹介未登録: {names}", ephemeral=True)

    @tree.command(
        name="intro",
        description="指定したメンバーの自己紹介を表示",
        guild=GUILD_OBJECT,
    )
    @app_commands.describe(user="自己紹介を表示するメンバー")
    async def intro_cmd(interaction: discord.Interaction, user: discord.Member) -> None:
        if user.bot:
            await interaction.response.send_message("Bot の自己紹介はありません。", ephemeral=True)
            return
        intro_channel = _resolve_intro_channel()
        if intro_channel is None:
            await interaction.response.send_message("自己紹介チャンネルが見つかりません。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            intro_msg = await find_intro_message(intro_channel, user.id)
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


def _resolve_intro_channel() -> discord.TextChannel | None:
    ch = bot.get_channel(runtime_config.intro_channel_id)
    return ch if isinstance(ch, discord.TextChannel) else None


def _ensure_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
