import asyncio
from types import SimpleNamespace

import bot


def test_truncate_below_limit():
    assert bot.truncate("hello", 100) == "hello"


def test_truncate_at_limit():
    text = "a" * 4000
    assert bot.truncate(text, 4000) == text


def test_truncate_over_limit_appends_ellipsis():
    text = "a" * 4001
    result = bot.truncate(text, 4000)
    assert len(result) == 4000
    assert result.endswith("…")
    assert result == "a" * 3999 + "…"


def _att(content_type=None, filename="x", url="http://example.com/x"):
    return SimpleNamespace(content_type=content_type, filename=filename, url=url)


def test_pick_image_by_content_type():
    a = _att(content_type="image/png", filename="x.bin")
    assert bot._pick_image_attachment([a]) is a


def test_pick_image_by_extension_when_content_type_missing():
    a = _att(content_type=None, filename="photo.JPG")
    assert bot._pick_image_attachment([a]) is a


def test_pick_image_skips_non_image():
    a = _att(content_type=None, filename="doc.txt")
    assert bot._pick_image_attachment([a]) is None


def test_pick_image_returns_first_match():
    txt = _att(content_type=None, filename="doc.txt", url="http://t")
    img1 = _att(content_type="image/png", filename="a.png", url="http://1")
    img2 = _att(content_type="image/jpeg", filename="b.jpg", url="http://2")
    assert bot._pick_image_attachment([txt, img1, img2]) is img1


def test_pick_image_empty():
    assert bot._pick_image_attachment([]) is None


def test_build_user_stats_url_requires_config(monkeypatch):
    monkeypatch.setattr(bot, "USER_STATS_SITE_BASE_URL", "")
    monkeypatch.setattr(bot, "USER_STATS_SITE_GUILD_ID", "42")

    assert bot.build_user_stats_url(42, 100) is None


def test_build_user_stats_url_requires_matching_guild(monkeypatch):
    monkeypatch.setattr(bot, "USER_STATS_SITE_BASE_URL", "https://stats.example.com")
    monkeypatch.setattr(bot, "USER_STATS_SITE_GUILD_ID", "42")

    assert bot.build_user_stats_url(43, 100) is None


def test_build_user_stats_url_adds_user_and_days(monkeypatch):
    monkeypatch.setattr(bot, "USER_STATS_SITE_BASE_URL", "https://stats.example.com")
    monkeypatch.setattr(bot, "USER_STATS_SITE_GUILD_ID", "42")

    assert bot.build_user_stats_url(42, 100) == "https://stats.example.com/u/100/level?days=30"


def test_build_user_stats_url_does_not_duplicate_u_path(monkeypatch):
    monkeypatch.setattr(bot, "USER_STATS_SITE_BASE_URL", "https://stats.example.com/u")
    monkeypatch.setattr(bot, "USER_STATS_SITE_GUILD_ID", "42")

    assert bot.build_user_stats_url(42, 100) == "https://stats.example.com/u/100/level?days=30"


def test_build_user_stats_view_none_without_url():
    assert bot.build_user_stats_view(None) is None


def test_build_user_stats_view_contains_link_button():
    async def build_view():
        return bot.build_user_stats_view("https://stats.example.com/u/100/level?days=30")

    view = asyncio.run(build_view())

    assert view is not None
    assert len(view.children) == 1
    button = view.children[0]
    assert button.label == "ユーザー統計を開く"
    assert button.url == "https://stats.example.com/u/100/level?days=30"


def _row(
    guild_id=1,
    intro_channel_id=None,
    cooldown_seconds=60,
    excluded_vc_ids=None,
    nudge_exempt_role_ids=None,
):
    return {
        "guild_id": guild_id,
        "intro_channel_id": intro_channel_id,
        "cooldown_seconds": cooldown_seconds,
        "excluded_vc_ids": excluded_vc_ids,
        "nudge_exempt_role_ids": nudge_exempt_role_ids,
    }


def test_row_to_config_handles_null_excluded():
    cfg = bot._row_to_config(_row(excluded_vc_ids=None))
    assert cfg.excluded_vc_ids == frozenset()
    assert isinstance(cfg.excluded_vc_ids, frozenset)


def test_row_to_config_handles_empty_excluded():
    cfg = bot._row_to_config(_row(excluded_vc_ids=[]))
    assert cfg.excluded_vc_ids == frozenset()


def test_row_to_config_populates_excluded():
    cfg = bot._row_to_config(_row(excluded_vc_ids=[10, 20, 10]))
    assert cfg.excluded_vc_ids == frozenset({10, 20})


def test_row_to_config_handles_null_nudge_exempt_roles():
    cfg = bot._row_to_config(_row(nudge_exempt_role_ids=None))
    assert cfg.nudge_exempt_role_ids == frozenset()
    assert isinstance(cfg.nudge_exempt_role_ids, frozenset)


def test_row_to_config_populates_nudge_exempt_roles():
    cfg = bot._row_to_config(_row(nudge_exempt_role_ids=[100, 200, 100]))
    assert cfg.nudge_exempt_role_ids == frozenset({100, 200})


def test_row_to_config_passes_through_other_fields():
    cfg = bot._row_to_config(_row(guild_id=42, intro_channel_id=777, cooldown_seconds=300))
    assert cfg.guild_id == 42
    assert cfg.intro_channel_id == 777
    assert cfg.cooldown_seconds == 300
