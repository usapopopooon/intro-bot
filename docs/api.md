# intro-bot API

intro-bot の自己紹介表示内容を外部サービスから取得し、信頼済みサービスから
チル場所選択を更新するための API。

API は Bot とは別プロセスで `./scripts/start-api.sh` として起動する。Discord Gateway には接続せず、Bot が同期した Postgres の `intro_messages` テーブルと、必要に応じて level-bot API を参照する。

## Base URL

Railway で API サービスに付与した公開ドメインを使う。

```
https://<intro-api-service-domain>
```

ローカル Docker Compose ではデフォルト:

```
http://localhost:8000
```

## Authentication

すべての `/api/v1/*` エンドポイントは Bearer トークンが必要。

```
Authorization: Bearer <INTRO_API_KEY>
```

`INTRO_API_KEY` が未設定の場合は `EXTERNAL_API_KEY` を流用する。どちらも空の場合、API は常に `401 Unauthorized` を返す。

## CORS

CORS はデフォルト無効。ブラウザから別オリジンで直接叩く場合のみ `INTRO_API_CORS_ORIGINS` を設定する。

```env
INTRO_API_CORS_ORIGINS=https://example.com,https://admin.example.com
```

公開フロントエンドに `INTRO_API_KEY` を置くと利用者から見えるため、通常はサーバー側 proxy 経由を推奨する。

## Health Check

### `GET /healthz`

認証不要。API プロセスの起動確認用。

#### Response

```json
{
  "ok": true
}
```

## Get Intro

### `GET /api/v1/guilds/{guild_id}/users/{user_id}/intro`

指定ユーザーの自己紹介表示内容を取得する。

### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `guild_id` | integer | Discord guild ID |
| `user_id` | integer | Discord user ID |

### Request

```bash
curl -H "Authorization: Bearer $INTRO_API_KEY" \
  "https://<intro-api-service-domain>/api/v1/guilds/123/users/456/intro"
```

### Response `200`

```json
{
  "guild_id": 123,
  "user_id": 456,
  "member": {
    "id": 456,
    "display_name": "うさぽ",
    "avatar_url": "https://cdn.discordapp.com/avatars/..."
  },
  "intro": {
    "content": "こんにちは。作業と雑談が好きです。",
    "content_truncated": "こんにちは。作業と雑談が好きです。",
    "jump_url": "https://discord.com/channels/123/789/101112",
    "message_id": 101112,
    "channel_id": 789,
    "created_at": "2026-05-23T06:00:00+00:00",
    "image_url": null
  },
  "level": {
    "level": 8,
    "progress": 0.42,
    "progress_percent": 42,
    "footer_text": "Lv. 8 (42%)"
  },
  "chill_place": {
    "current": {
      "required_level": 8,
      "name": "ふかふかチェア",
      "emoji": "💤",
      "display_name": "💤 ふかふかチェア",
      "tags": ["まったり", "休憩"],
      "description": "ちょっと疲れた日に沈み込む席。"
    },
    "next": {
      "required_level": 9,
      "name": "充電席",
      "emoji": "🔌",
      "display_name": "🔌 充電席",
      "tags": ["回復", "作業"],
      "description": "端末も気持ちも、じわっと充電していく場所。"
    },
    "selected_locked": false,
    "display_text": "💤 ふかふかチェア (Lv.8)\nまったり / 休憩\nちょっと疲れた日に沈み込む席。\n次の解放: 🔌 充電席 Lv.9"
  },
  "stats_url": "https://stats.example.com/u/456/level?days=30",
  "display": {
    "author_name": "うさぽ",
    "author_icon_url": "https://cdn.discordapp.com/avatars/...",
    "intro_link_label": "ジャンプ",
    "stats_link_label": "30日間の統計を見る"
  }
}
```

### Nullable Fields

| Field | When null |
|-------|-----------|
| `intro.image_url` | 自己紹介投稿に画像添付がない |
| `level` | `LEVEL_API_BASE` 未設定、level-bot 応答失敗、対象ユーザーのレベル情報なし |
| `chill_place` | `level` が取得できない |
| `chill_place.current` | 現在レベルで解放済みの場所がない |
| `chill_place.next` | 次に解放される場所がない |
| `stats_url` | 統計サイトリンク設定が無効、または対象ギルドではない |
| `display.stats_link_label` | `stats_url` が null |

## Chill Places

### `GET /api/v1/guilds/{guild_id}/users/{user_id}/chill-places`

level-bot などの信頼済みサービスから、対象ユーザーが現在レベルで選択できる
チル場所一覧を取得する。`LEVEL_API_BASE` で level-bot API が設定されている必要がある。

### Response `200`

```json
{
  "guild_id": 123,
  "user_id": 456,
  "level": { "level": 8, "progress": 0.1, "progress_percent": 10 },
  "selected_required_level": 5,
  "places": [
    {
      "required_level": 8,
      "name": "ふかふかチェア",
      "emoji": "💤",
      "display_name": "💤 ふかふかチェア",
      "choice_label": "💤 ふかふかチェア (Lv.8)",
      "tags": ["リラックス"],
      "description": "ゆっくり休める席"
    }
  ]
}
```

### `PUT /api/v1/guilds/{guild_id}/users/{user_id}/chill-place`

対象ユーザーの自己紹介に表示するチル場所を設定する。指定した `required_level` が
存在し、かつ現在レベルで解放済みの場合だけ保存される。

### Request

```bash
curl -X PUT -H "Authorization: Bearer $INTRO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"required_level":8}' \
  "https://<intro-api-service-domain>/api/v1/guilds/123/users/456/chill-place"
```

### Response `200`

```json
{
  "guild_id": 123,
  "user_id": 456,
  "level": { "level": 8, "progress": 0.1, "progress_percent": 10 },
  "selected": {
    "required_level": 8,
    "name": "ふかふかチェア",
    "emoji": "💤",
    "display_name": "💤 ふかふかチェア",
    "choice_label": "💤 ふかふかチェア (Lv.8)",
    "tags": ["リラックス"],
    "description": "ゆっくり休める席"
  },
  "chill_place": {
    "current": {
      "required_level": 8,
      "name": "ふかふかチェア",
      "emoji": "💤",
      "display_name": "💤 ふかふかチェア",
      "tags": ["リラックス"],
      "description": "ゆっくり休める席"
    },
    "next": null,
    "selected_locked": false,
    "display_text": "💤 ふかふかチェア (Lv.8)\nリラックス\nゆっくり休める席"
  }
}
```

### Chill Place Errors

- `400` — ID / JSON / `required_level` が不正、または存在しないチル場所
- `403` — 指定したチル場所が現在レベルでは未解放
- `424` — level-bot API から現在レベルを取得できない

## Error Responses

### `400 Bad Request`

`guild_id` または `user_id` が整数として扱えない。

```json
{
  "detail": "guild_id and user_id must be integers"
}
```

### `401 Unauthorized`

Bearer トークンがない、間違っている、または API キーが未設定。

```json
{
  "detail": "Unauthorized"
}
```

### `404 Not Found`

対象ユーザーの自己紹介が API 用 DB に存在しない。

```json
{
  "detail": "Intro not found"
}
```

API 追加前からある既存自己紹介は、Bot が同期するまで `404` になる。管理者が Discord で以下を実行すると、自己紹介チャンネルの既存投稿を同期できる。

```
/intro-config sync-intros
```

### `429 Too Many Requests`

Bearer 認証失敗が IP 単位の制限を超えた。

```json
{
  "detail": "Too Many Requests"
}
```

制限値は以下で調整できる。

```env
INTRO_API_AUTH_FAILURE_LIMIT=10
INTRO_API_AUTH_FAILURE_WINDOW_SECONDS=60
```

## Data Freshness

Bot は以下のタイミングで `intro_messages` を更新する。

- 自己紹介チャンネルに本文あり投稿が作成されたとき
- 自己紹介投稿が編集されたとき
- 自己紹介投稿が削除されたとき
- `/intro`, `/intros`, VC 入室自動投稿などで履歴から自己紹介を見つけたとき
- `/intro-config intro-channel` 設定時
- `/intro-config sync-intros` 実行時

API 側の level-bot 取得結果は `LEVEL_CACHE_TTL_SECONDS` 秒キャッシュされる。

## Railway

API サービスは Bot サービスとは別に作る。

```text
Root Directory: 空欄
Dockerfile Path: Dockerfile
Start Command: ./scripts/start-api.sh
```

必要な環境変数:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
INTRO_API_KEY=<long-random-token>
```

Railway では `INTRO_API_PORT` を設定しない。Railway が注入する `PORT` を自動で使う。
