# intro-bot

Discord でユーザーが VC に入室すると、自己紹介チャンネルの最新投稿を VC のテキストチャット(text-in-voice)に埋め込み形式で自動投稿する Bot。

- 詳細仕様: [SPEC.md](SPEC.md)
- 運用 / 管理者向けの使い方: [docs/usage.md](docs/usage.md)

## 必要なもの

- Docker / Docker Compose(ローカル実行)または Railway アカウント(本番デプロイ)
- Discord Bot アカウント([Discord Developer Portal](https://discord.com/developers/applications) で作成)
- 対象 Discord サーバーの管理者権限

### Discord Developer Portal の設定

Bot ページで以下の **Privileged Gateway Intents** を有効化する。

- `SERVER MEMBERS INTENT`
- `MESSAGE CONTENT INTENT`

## 環境変数

| キー | 必須 | デフォルト | 用途 |
|------|------|-----------|------|
| `DISCORD_TOKEN` | ✅ | — | Bot トークン |
| `GUILD_ID` | ✅ | — | 対象サーバーID |
| `DATABASE_URL` | ✅ | — | Postgres 接続文字列(Compose / Railway では自動注入) |
| `INTRO_CHANNEL_ID` | △ | — | 自己紹介チャンネルID(**初回起動時のみ**参照) |
| `COOLDOWN_SECONDS` | — | `3600` | クールダウン秒数(初回起動時のみ) |
| `INTRO_HISTORY_MAX_SCAN` | — | `5000` | 履歴走査の最大件数 |

`INTRO_CHANNEL_ID` / `COOLDOWN_SECONDS` はブートストラップ用。2 回目以降の起動では DB の値が優先され、ENV を変更しても無視される。実行中に変更したい場合は Discord 上で `/intro-config` を使う([docs/usage.md](docs/usage.md))。

## ローカル実行(Docker Compose)

```bash
cp .env.example .env
# .env を編集して DISCORD_TOKEN / GUILD_ID / INTRO_CHANNEL_ID を記入
# DATABASE_URL は compose.yaml で postgres サービスを参照するため記入不要
docker compose up --build
```

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
   - `DISCORD_TOKEN`
   - `GUILD_ID`
   - `INTRO_CHANNEL_ID`(初回のみ)
   - `COOLDOWN_SECONDS`(任意)
4. デプロイ後 Logs で `Logged in as ...` を確認
5. Discord で管理者として `/intro-config show` を実行し、設定が反映されているか確認

詳細手順と注意点は [SPEC.md § 8.2](SPEC.md#82-railway-デプロイ)。

## Bot 権限

サーバー全体に以下を付与しておけば追加設定不要。

- `View Channel`
- `Send Messages`
- `Embed Links`
- `Read Message History`

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
