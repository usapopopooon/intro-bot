import asyncio
import inspect
from types import SimpleNamespace

import api
import bot


def _component_label(component):
    return getattr(component, "label", None) or getattr(getattr(component, "item", None), "label", None)


class _FakeCommandTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(callback):
            self.commands[name] = SimpleNamespace(description=description, callback=callback)
            return callback

        return decorator

    def add_command(self, command):
        return None


def test_intro_command_user_option_is_optional():
    tree = _FakeCommandTree()

    bot.register_commands(tree, SimpleNamespace())

    command = tree.commands["intro"]
    user_parameter = inspect.signature(command.callback).parameters["user"]
    assert user_parameter.default is None
    assert "省略時は自分" in command.description


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


def test_build_intro_view_contains_chill_button_without_stats_url():
    async def build_view():
        return bot.build_intro_view(SimpleNamespace(), 42, 100, None)

    view = asyncio.run(build_view())

    assert len(view.children) == 1
    button = view.children[0]
    assert _component_label(button) == "チル場所を設定"


def test_build_intro_view_contains_chill_and_stats_buttons():
    async def build_view():
        return bot.build_intro_view(SimpleNamespace(), 42, 100, "https://stats.example.com/u/100/level?days=30")

    view = asyncio.run(build_view())

    assert len(view.children) == 2
    labels = [_component_label(child) for child in view.children]
    urls = [getattr(child, "url", None) for child in view.children]
    assert "チル場所を設定" in labels
    assert "ユーザー統計を開く" in labels
    assert "https://stats.example.com/u/100/level?days=30" in urls


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


def test_split_available_excluded_vc_ids_keeps_accessible_channels():
    kept, removed = bot.split_available_excluded_vc_ids(
        frozenset({10, 20, 30}),
        frozenset({10, 30, 40}),
    )

    assert kept == frozenset({10, 30})
    assert removed == frozenset({20})


def test_split_available_excluded_vc_ids_noop_without_removed_channels():
    kept, removed = bot.split_available_excluded_vc_ids(
        frozenset({10, 20}),
        frozenset({10, 20, 30}),
    )

    assert kept == frozenset({10, 20})
    assert removed == frozenset()


def test_prune_unavailable_excluded_vcs_uses_db_current_config(monkeypatch):
    pool = object()
    memory_cfg = bot.GuildConfig(
        guild_id=1,
        intro_channel_id=100,
        cooldown_seconds=60,
        excluded_vc_ids=frozenset({10, 20}),
        nudge_exempt_role_ids=frozenset(),
    )
    db_cfg = bot.GuildConfig(
        guild_id=1,
        intro_channel_id=100,
        cooldown_seconds=60,
        excluded_vc_ids=frozenset({10, 30}),
        nudge_exempt_role_ids=frozenset(),
    )
    calls = {}

    async def fake_prune_in_db(received_pool, guild_id, available_channel_ids):
        calls["args"] = (received_pool, guild_id, available_channel_ids)
        return db_cfg, frozenset({20})

    monkeypatch.setattr(bot, "prune_unavailable_excluded_vcs_in_db", fake_prune_in_db)
    client = SimpleNamespace(
        pool=pool,
        configs={1: memory_cfg},
        available_voice_channel_ids=lambda _guild: frozenset({10, 30}),
    )

    cfg, removed = asyncio.run(bot.IntroBot.prune_unavailable_excluded_vcs(client, SimpleNamespace(id=1)))

    assert cfg == db_cfg
    assert removed == frozenset({20})
    assert client.configs[1] == db_cfg
    assert calls["args"] == (pool, 1, frozenset({10, 30}))


def test_build_chill_places_uses_defaults():
    places = bot.build_chill_places()

    assert places[0].required_level == 1
    assert places[0].name == "入口のベンチ"
    assert places[0].emoji == "🪑"
    assert places[0].tags == ("はじめまして", "気軽")
    assert next(p for p in places if p.required_level == 20).name == "チルラウンジ"


def test_build_chill_places_overrides_and_sorts():
    places = bot.build_chill_places(
        {
            2: bot.ChillPlaceOverride(name="秘密のロビー", emoji="✨"),
            11: bot.ChillPlaceOverride(name="昼寝席"),
        }
    )

    assert [p.required_level for p in places] == sorted(p.required_level for p in places)
    level_2 = next(p for p in places if p.required_level == 2)
    level_11 = next(p for p in places if p.required_level == 11)
    assert level_2.name == "秘密のロビー"
    assert level_2.emoji == "✨"
    assert level_11.name == "昼寝席"
    assert level_11.emoji is None


def test_build_chill_places_keeps_default_vibe_when_overridden():
    place = next(
        p for p in bot.build_chill_places({2: bot.ChillPlaceOverride(name="秘密のロビー")}) if p.required_level == 2
    )

    assert place.name == "秘密のロビー"
    assert place.emoji == "🛋️"
    assert place.tags == ("雑談", "のんびり")
    assert place.description == "通りすがりの会話に混ざりやすい、やわらかい場所。"


def test_resolve_chill_display_defaults_to_highest_unlocked():
    display = bot.resolve_chill_display(bot.build_chill_places(), (7, 0.5))

    assert display is not None
    assert display.current is not None
    assert display.current.required_level == 7
    assert display.current.name == "観葉植物の横"
    assert display.next_place is not None
    assert display.next_place.required_level == 8
    assert display.next_place.name == "ふかふかチェア"
    assert display.selected_locked is False


def test_resolve_chill_display_uses_selected_unlocked_place():
    display = bot.resolve_chill_display(bot.build_chill_places(), (7, 0.5), selected_level=5)

    assert display is not None
    assert display.current is not None
    assert display.current.required_level == 5
    assert display.current.name == "カフェカウンター"
    assert display.next_place is not None
    assert display.next_place.required_level == 8
    assert display.next_place.name == "ふかふかチェア"


def test_resolve_chill_display_ignores_locked_selected_place():
    display = bot.resolve_chill_display(bot.build_chill_places(), (7, 0.5), selected_level=20)

    assert display is not None
    assert display.current is not None
    assert display.current.required_level == 7
    assert display.current.name == "観葉植物の横"
    assert display.selected_locked is True


def test_resolve_chill_display_none_without_level():
    assert bot.resolve_chill_display(bot.build_chill_places(), None) is None


def test_format_chill_display_includes_vibe():
    display = bot.resolve_chill_display(bot.build_chill_places(), (8, 0.1))

    assert display is not None
    text = bot.format_chill_display(display)
    assert "💤 ふかふかチェア (Lv.8)" in text
    assert "まったり / 休憩" in text
    assert "ちょっと疲れた日に沈み込む席。" in text
    assert "次の解放: 🔌 充電席 Lv.9" in text


def test_format_compact_chill_display_omits_vibe():
    display = bot.resolve_chill_display(bot.build_chill_places(), (8, 0.1))

    assert display is not None
    text = bot.format_compact_chill_display(display)
    assert text == "💤 ふかふかチェア (Lv.8) / 次: 🔌 充電席 Lv.9"
    assert "まったり" not in text
    assert "ちょっと疲れた日に沈み込む席。" not in text


def test_resolve_chill_place_selection_accepts_choice_value_and_name():
    places = bot.build_chill_places()

    assert bot.resolve_chill_place_selection(places, "8").name == "ふかふかチェア"
    assert bot.resolve_chill_place_selection(places, "ふかふかチェア").required_level == 8
    assert bot.resolve_chill_place_selection(places, "💤 ふかふかチェア").required_level == 8
    assert bot.resolve_chill_place_selection(places, "💤 ふかふかチェア (Lv.8)").required_level == 8
    assert bot.resolve_chill_place_selection(places, "ない場所") is None


def test_build_chill_place_choices_filters_by_name_and_level():
    places = bot.build_chill_places()

    choices = bot.build_chill_place_choices(places, "ソファ", current_level=20)

    assert [choice.name for choice in choices] == ["🛋️ ロビーソファ (Lv.2)", "🕯️ 半個室ソファ (Lv.18)"]
    assert [choice.value for choice in choices] == ["2", "18"]


def test_build_chill_place_choices_hides_locked_places():
    places = bot.build_chill_places()

    choices = bot.build_chill_place_choices(places, "", current_level=2)

    assert [choice.name for choice in choices] == ["🪑 入口のベンチ (Lv.1)", "🛋️ ロビーソファ (Lv.2)"]


def test_format_chill_list_includes_emoji():
    text = bot.format_chill_list(bot.build_chill_places(), level=2)

    assert "✓ Lv.1 🪑 入口のベンチ" in text
    assert "✓ Lv.2 🛋️ ロビーソファ" in text
    assert "□ Lv.3 🪟 窓際スツール" in text


def test_serialize_chill_display():
    display = bot.resolve_chill_display(bot.build_chill_places(), (8, 0.1))

    payload = bot.serialize_chill_display(display)

    assert payload is not None
    assert payload["current"]["required_level"] == 8
    assert payload["current"]["name"] == "ふかふかチェア"
    assert payload["current"]["emoji"] == "💤"
    assert payload["current"]["display_name"] == "💤 ふかふかチェア"
    assert payload["current"]["tags"] == ["まったり", "休憩"]
    assert payload["next"]["required_level"] == 9
    assert payload["next"]["display_name"] == "🔌 充電席"
    assert payload["selected_locked"] is False
    assert "💤 ふかふかチェア (Lv.8)" in payload["display_text"]


def test_parse_bearer_token():
    class Request:
        headers = {"Authorization": "Bearer secret"}

    assert api.parse_bearer_token(Request()) == "secret"


def test_parse_bearer_token_rejects_missing_or_wrong_scheme():
    class Missing:
        headers = {}

    class Basic:
        headers = {"Authorization": "Basic abc"}

    assert api.parse_bearer_token(Missing()) is None
    assert api.parse_bearer_token(Basic()) is None


def test_intro_api_auth_failure_rate_limit(monkeypatch):
    monkeypatch.setattr(api, "INTRO_API_AUTH_FAILURE_LIMIT", 2)
    monkeypatch.setattr(api, "INTRO_API_AUTH_FAILURE_WINDOW_SECONDS", 60)
    app = {"auth_failures": {}}

    class Request:
        headers = {}
        transport = None

    request = Request()

    assert api.is_auth_limited(app, request) is False
    api.record_auth_failure(app, request)
    assert api.is_auth_limited(app, request) is False
    api.record_auth_failure(app, request)
    assert api.is_auth_limited(app, request) is True


def test_build_cors_headers_disabled_without_config(monkeypatch):
    monkeypatch.setattr(api, "INTRO_API_CORS_ORIGINS", frozenset())

    class Request:
        headers = {"Origin": "https://example.com"}

    assert api.build_cors_headers(Request()) == {}


def test_build_cors_headers_allows_configured_origin(monkeypatch):
    monkeypatch.setattr(api, "INTRO_API_CORS_ORIGINS", frozenset({"https://example.com"}))

    class Request:
        headers = {"Origin": "https://example.com"}

    headers = api.build_cors_headers(Request())

    assert headers["Access-Control-Allow-Origin"] == "https://example.com"
    assert headers["Access-Control-Allow-Methods"] == "GET, PUT, OPTIONS"
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert headers["Vary"] == "Origin"


def test_build_cors_headers_rejects_unconfigured_origin(monkeypatch):
    monkeypatch.setattr(api, "INTRO_API_CORS_ORIGINS", frozenset({"https://example.com"}))

    class Request:
        headers = {"Origin": "https://evil.example"}

    assert api.build_cors_headers(Request()) == {}
