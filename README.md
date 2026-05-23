# intro-bot

Discord でユーザーが VC に入室すると、自己紹介チャンネルの最新投稿を VC のテキストチャット(text-in-voice)に埋め込み形式で自動投稿する Bot。

- 招待されたすべてのギルドで動作する(マルチギルド対応)
- 1 プロセスで複数の Bot トークンを並列起動可能
- ギルドごとの設定(自己紹介チャンネル / クールダウン)は Postgres に永続化
- 任意で [level-bot](../level-bot) 連携: 埋め込み footer に総合レベルを表示し、ユーザー統計サイトへのリンクやレベル解放式のチル場所を追加
- `Authorization: Bearer <key>` で自己紹介表示内容を取得できる読み取り専用 API
- 運用 / 管理者向けの使い方: [docs/usage.md](docs/usage.md)
- 外部 API 仕様: [docs/api.md](docs/api.md)

## 必要なもの

- Docker / Docker Compose(ローカル実行)または Railway アカウント(本番デプロイ)
- Discord Bot アカウント([Discord Developer Portal](https://discord.com/developers/applications) で作成)

### Discord Developer Portal の設定

Bot ページで以下の **Privileged Gateway Intents** を有効化する。

- `SERVER MEMBERS INTENT`
- `MESSAGE CONTENT INTENT`

## 環境変数

| キー | 必須 | デフォルト | 用途 |
|------|------|-----------|------|
| `DISCORD_TOKEN` | △ | — | Bot トークン(単一)。`DISCORD_TOKENS` が無いときに使われる |
| `DISCORD_TOKENS` | △ | — | カンマ区切りで複数トークンを並列起動。設定があればこちらが優先される |
| `DATABASE_URL` | ✅ | — | Postgres 接続文字列(Compose / Railway では自動注入) |
| `COOLDOWN_SECONDS` | — | `60` | 新規ギルドのデフォルトクールダウン秒数(以降は `/intro-config cooldown` で変更) |
| `INTRO_HISTORY_MAX_SCAN` | — | `5000` | 履歴走査の最大件数 |
| `LEVEL_API_BASE` | — | (空) | level-bot の Base URL。**空ならレベル取得は無効**(footer なし) |
| `LEVEL_API_TIMEOUT_SECONDS` | — | `3` | level-bot へのリクエストタイムアウト秒数 |
| `LEVEL_CACHE_TTL_SECONDS` | — | `60` | 取得したレベルをプロセス内でキャッシュする秒数 |
| `USER_STATS_SITE_GUILD_ID` | — | (空) | ユーザー統計サイトへのリンクを表示する対象ギルド ID |
| `USER_STATS_SITE_BASE_URL` | — | (空) | `/level` と同じユーザー統計サイトの Base URL。`/u/<user_id>/level?days=30` を付けてリンクする |
| `EXTERNAL_API_KEY` | — | (空) | level-bot 側で外部 API キー認証が有効な場合のみ設定。`Authorization: Bearer <key>` で送信される |
| `INTRO_API_KEY` | — | `EXTERNAL_API_KEY` | intro-bot 外部 API 用の Bearer キー。空なら外部 API は 401 |
| `INTRO_API_HOST` | — | `0.0.0.0` | intro-bot 外部 API の bind host |
| `INTRO_API_PORT` | — | `PORT` or `8000` | intro-bot 外部 API の bind port。Railway では未設定推奨 |
| `INTRO_API_AUTH_FAILURE_LIMIT` | — | `10` | 外部 API の Bearer 認証失敗を許容する回数 |
| `INTRO_API_AUTH_FAILURE_WINDOW_SECONDS` | — | `60` | 外部 API の Bearer 認証失敗を数える秒数 |
| `INTRO_API_CORS_ORIGINS` | — | (空) | ブラウザから外部 API を叩く場合に許可する Origin(カンマ区切り)。server-to-server なら空でよい |

`DISCORD_TOKEN` か `DISCORD_TOKENS` のどちらかが必須。両方ある場合は `DISCORD_TOKENS` が使われる。

ギルドごとの自己紹介チャンネルは **環境変数では指定しない**。Bot を招待したあと、各ギルドの管理者が `/intro-config intro-channel <#channel>` で設定する(設定するまで auto-post は無効)。

## ローカル実行(Docker Compose)

```bash
cp .env.example .env
# .env を編集して DISCORD_TOKEN または DISCORD_TOKENS を記入
# DATABASE_URL は compose.yaml で postgres サービスを参照するため記入不要
docker compose up --build
```

`.env` は任意ファイルとして読み込まれるため、`docker compose config` などの構文確認は `.env` 作成前でも実行できる。ただし Bot を実際に起動するには `DISCORD_TOKEN` または `DISCORD_TOKENS` が必要。

停止 / クリーンアップ:

```bash
docker compose down       # 停止のみ
docker compose down -v    # ボリュームごと削除(DB リセット)
```

## Railway デプロイ

1. Railway で **New Project** → **Deploy from GitHub repo** で本リポジトリを選択(`railway.json` と `Dockerfile` を自動検出)
2. 同じプロジェクト内で **+ New** → **Database** → **Add PostgreSQL**
3. Bot サービスの **Variables** タブで以下を設定:
   - `DATABASE_URL = ${{Postgres.DATABASE_URL}}`(**内部用** の参照変数を使う。egress 課金対象外)
   - `DISCORD_TOKEN` または `DISCORD_TOKENS`
   - `COOLDOWN_SECONDS`(任意)
4. 外部 API を使う場合は、同じリポジトリ/Dockerfile でもう 1 つ Railway サービスを作り、Start Command を `./scripts/start-api.sh` にする
   - API サービスにも `DATABASE_URL = ${{Postgres.DATABASE_URL}}` を設定
   - `INTRO_API_KEY` を設定
   - `INTRO_API_PORT` は未設定にして Railway の `PORT` を使う
5. デプロイ後 Logs で Bot 側の `[Bot#1234] ready (id=..., guilds=N)` と API 側の `intro API listening` を確認
6. 各ギルドで管理者として `/intro-config intro-channel <#channel>` を実行して自己紹介チャンネルを設定

## Bot を招待する

Discord Developer Portal の **OAuth2 → URL Generator** で以下を選んだ URL を使う。

**Scopes**: `bot`, `applications.commands`

**Bot Permissions**:

- `View Channels`
- `Send Messages`
- `Embed Links`
- `Read Message History`

サーバー全体に上記権限を付与しておけば、自己紹介チャンネルと各 VC の text-in-voice の両方で動作する。

## 開発

依存と開発ツール(ruff / pytest)をまとめてインストール:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

lint / format / test:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

CI(GitHub Actions)で push / PR 時に自動実行される(`.github/workflows/ci.yml`)。
