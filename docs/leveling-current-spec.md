# レベリング / チル場所解放 現状仕様

この文書は `intro-bot` の現行実装と、連携先である `../level-bot` の実装を参照して作成した現状仕様です。
主な責務分担は次の通りです。

- `level-bot`: Discord 上の活動を集計し、XP・総合レベル・項目別レベルを算出する。
- `intro-bot`: `level-bot` のレベル API を参照し、自己紹介 embed にレベル、進捗、チル場所、統計リンクを表示する。

## 全体像

1. `level-bot` がメッセージ、VC、リアクションを `daily_stats` に日次・ユーザー・チャンネル単位で蓄積する。
2. `level-bot` の API `GET /api/v1/guilds/{guild_id}/users/{user_id}/levels` が lifetime または直近 N 日のレベル情報を返す。
3. `intro-bot` は `LEVEL_API_BASE` が設定されている場合のみ、その API から対象ユーザーの `total.level` と `total.progress` を取得する。
4. `intro-bot` は取得した総合レベルを使い、自己紹介 embed に現在解放済みのチル場所と次の解放場所を表示する。
5. レベル取得に失敗した場合、自己紹介投稿自体は止めず、レベル footer とチル場所を表示しない。

## level-bot の集計対象

### メッセージ

- 対象イベントは通常メッセージと返信メッセージ。
- 空本文でも添付ファイルがあればメッセージとして加算される。
- メッセージごとに `message_count` を 1 加算する。
- `char_count` には本文文字数、`attachment_count` には添付数を加算する。
- Bot 投稿は通常除外される。ギルド設定 `count_bots` が有効な場合のみ Bot 投稿も集計する。
- ギルド設定 `tracking_enabled` が無効な場合は集計しない。
- `excluded_channels` に登録されたチャンネルの活動は集計しない。

### ボイス

- VC 入室または移動で `voice_sessions` に進行中セッションを作る。
- 退室または別 VC への移動でセッションを終了し、経過秒数を `daily_stats.voice_seconds` に加算する。
- 1 セッションの最大加算秒数は 24 時間にクランプされる。
- 進行中セッションは、レベル API の個別ユーザー表示では live voice として現在時刻までの経過分も含めて計算される。
- Bot の VC は通常除外される。ギルド設定 `count_bots` が有効な場合のみ集計する。
- Bot 再起動時は、残っている進行中セッションを一度 `daily_stats` に flush し、現在 VC にいるメンバーからセッションを再構築する。

### リアクション

- `reactions_given`: ユーザーが他人のメッセージに付けたリアクション数。
- `reactions_received`: ユーザーのメッセージに付いたリアクション数。
- セルフリアクションは自己加点防止のため集計しない。
- Bot からのリアクション、Bot メッセージへのリアクションは、`count_bots=false` の場合は除外する。
- `reactions` テーブルで状態を管理し、`1 メッセージ x 1 リアクター = 1 加算` になるよう重複を抑制する。
- 絵文字違いのリアクションが複数あっても、同じメッセージと同じリアクターの組では daily stats への加算は 1 回だけ。
- リアクション解除時は対応する `reactions_given` / `reactions_received` を 0 未満にならないよう減算する。
- 管理者操作などでリアクションが一括削除された場合は `reactions` テーブルのみ掃除し、`daily_stats` は過去の engagement 履歴として保持する。

### 返信 / ソーシャルエッジ

レベル XP の直接要素ではないが、返信やリアクション相手は `social_edges_daily` に記録される。これは関係性グラフ用途のデータで、現行レベル計算の axis には含まれない。

## XP 換算

XP は以下の 4 axis に分けて算出される。

| axis | 元データ | XP 換算 |
| --- | --- | --- |
| `voice` | VC 滞在秒数 | `round(voice_seconds / 60 * 1.0)` |
| `text` | メッセージ数 | `round(message_count * message_weight)` |
| `reactions_received` | 受け取ったリアクション数 | `round(reactions_received * reaction_received_weight)` |
| `reactions_given` | 付けたリアクション数 | `round(reactions_given * reaction_given_weight)` |

総合 XP は、上記 4 axis の整数 XP を合計した値。

現在運用中の重み定数は次の通り。ただし実際のレベル計算では DB の XP 重み履歴が正とされる。

| 項目 | 現行重み |
| --- | ---: |
| VC | 1.0 XP / 分 |
| メッセージ | 3.0 XP / 件 |
| リアクション受領 | 2.0 XP / 件 |
| リアクション送付 | 2.0 XP / 件 |

過去互換の旧重みもコード上に残っている。

| 項目 | 旧重み |
| --- | ---: |
| メッセージ | 2.0 XP / 件 |
| リアクション受領 | 0.5 XP / 件 |
| リアクション送付 | 0.5 XP / 件 |

### XP 重み履歴

- メッセージ、リアクション受領、リアクション送付の重みは `level_xp_weight_versions` の active な全体共通 version から取得する。
- 互換用 mirror として `level_xp_weight_logs` も存在する。
- 重みは `effective_from` の日付以降の活動にだけ適用される。
- 過去分を新しい重みで再計算しない。
- 読み取り側は重み履歴を短時間キャッシュする。
- 管理 API では重みログの追加、ロールバック、mirror 整合性確認ができる。

## レベル曲線

レベルは純粋累積 XP から計算する。期間による減衰はなく、一度上がった lifetime レベルは活動しないことで下がらない。

### 必要 XP

`L` レベル到達に必要な累計 XP:

```text
cum(L) = round(100 * (1.2^L - 1) / (1.2 - 1))
```

`L=0` は 0 XP。`L=1` は 100 XP。

次レベルまでの単区間必要 XP は概念上:

```text
req(L) = 100 * 1.2^(L - 1)
```

### レベル判定

- `xp < 100` は Lv.0。
- `cum(level) <= xp < cum(level + 1)` を満たす最大の `level` が現在レベル。
- 各 axis と総合は同じ曲線で個別に計算する。

### 進捗率

API が返す `progress` は、現在レベルから次レベルへの進捗。

```text
progress = (xp - current_floor) / (next_floor - current_floor)
```

値は `0.0` から `1.0` に丸め込まれる。`intro-bot` の footer では `int(progress * 100)` としてパーセント表示される。

## level-bot API

### ユーザーレベル

```http
GET /api/v1/guilds/{guild_id}/users/{user_id}/levels
GET /api/v1/guilds/{guild_id}/users/{user_id}/levels?days=30
```

`days` を省略すると lifetime 累積、指定すると直近 N 日で計算する。レスポンスは次の構造。

```json
{
  "total": {
    "level": 8,
    "xp": 1234,
    "current_floor": 1099,
    "next_floor": 1419,
    "progress": 0.421875
  },
  "voice": { "...": "..." },
  "text": { "...": "..." },
  "reactions_received": { "...": "..." },
  "reactions_given": { "...": "..." }
}
```

表示除外ユーザー、脱退済みユーザー、完全に活動ゼロのユーザーは `404` になる。レスポンスには `Cache-Control: private, max-age=30` が付く。

### レベルランキング

```http
GET /api/v1/guilds/{guild_id}/levels/leaderboard?axis=total&limit=10&offset=0
```

`axis` は次のいずれか。

- `total`
- `voice`
- `text`
- `reactions_received`
- `reactions_given`

ランキングは lifetime 累積 XP を元にする。表示除外ユーザーと脱退済みユーザーは除外される。パフォーマンス上、ランキングでは進行中 VC の live voice は含めない。

## レベルアップ通知

`level-bot` は活動により総合レベルが上がった場合、活動が発生した場所へレベルアップ通知 embed を送信する。

- 通知文は「レベルアップ！ <表示名> さんが Lv N になりました。」
- 通知は 30 秒後に削除される。
- 同一ユーザー・同一レベルの通知は 30 秒以内に重複送信しない。
- メッセージ、リアクション、VC 退室/移動時にレベル上昇を検知する。
- VC 接続中は 60 秒ごとの live voice チェックでもレベル上昇を検知する。

## レベル到達ロール

`level-bot` には、総合レベル到達時に Discord ロールを付与する仕組みがある。

### 設定

- 設定は Web 管理画面/API で行う。
- 内部保存は `role_id`。
- API は `PUT /api/v1/guilds/{guild_id}/level-role-awards` で全置換する。
- `level` は 0 以上の整数。Lv.0 ルールも指定できる。
- ルールは `guild_id`, `slot`, `level` の組で一意。
- `grant_mode` は `replace` または `stack`。
- 同じ slot 内で `grant_mode` を混在させることはできない。

### 付与動作

- `replace`: slot ごとに、到達済みの最大レベルのロール 1 つだけを保持する。より高いレベルのロールに到達すると、同じ slot の下位ロールは外される。
- `stack`: 到達済みのロールを外さず、条件を満たすロールを追加していく。
- 付与対象レベルは総合レベル。
- 活動後の個別チェックは 15 秒の簡易スロットリングがある。
- ルール変更後はギルド単位の一括同期要求が記録され、Bot 側の 20 秒周期ループが既存メンバーへ反映する。
- Bot を `count_bots=false` にしている場合、一括同期時の Bot メンバーはスキップされる。

## intro-bot の level-bot 連携

`intro-bot` は `LEVEL_API_BASE` が空の場合、レベル連携を完全に無効として扱う。

### 取得

- 取得先は `{LEVEL_API_BASE}/api/v1/guilds/{guild_id}/users/{user_id}/levels`。
- `EXTERNAL_API_KEY` が設定されている場合、`Authorization: Bearer <key>` を付与する。
- タイムアウトは `LEVEL_API_TIMEOUT_SECONDS`。デフォルト 3 秒。
- 成功時は `total.level` と `total.progress` だけを使用する。
- 失敗、非 200、レスポンス形式不正の場合は `None` として扱う。
- 結果は `(guild_id, user_id)` 単位で `LEVEL_CACHE_TTL_SECONDS` 秒キャッシュされる。デフォルト 60 秒。

### 自己紹介 embed 表示

レベル取得に成功した場合:

- footer に `Lv. {level} ({progress_percent}%)` を表示する。
- チル場所フィールドを追加する。

`USER_STATS_SITE_GUILD_ID` と `USER_STATS_SITE_BASE_URL` が設定され、対象 guild が一致する場合:

- embed に「30日間の統計を見る」リンクを追加する。
- URL は `{USER_STATS_SITE_BASE_URL}/u/{user_id}/level?days=30`。

レベル取得に失敗した場合:

- footer は表示しない。
- チル場所も表示しない。
- 自己紹介 embed 自体は通常通り投稿する。

## チル場所解放

チル場所は `intro-bot` 側の表示機能で、解放条件には `level-bot` の総合レベルを使う。

### 表示判定

- `level_info` が取得できない場合、チル場所表示は `None`。
- `required_level <= current_level` の場所が解放済み。
- 解放済みが 1 つ以上あり、ユーザー選択がない場合は、解放済みのうち最も高い `required_level` の場所を自動表示する。
- ユーザー選択があり、その場所が現在レベル以下なら、その選択を表示する。
- ユーザー選択があるが現在レベルでは未解放になっている場合、現在解放済みの最高場所を表示しつつ「選択中は未解放」の状態を付ける。
- 解放済みが 1 つもない場合は「まだ解放されていません」と表示し、次の解放場所を示す。
- 次の解放場所は、現在レベルより大きい最初の場所。

### embed のコンパクト表示

自己紹介 embed では 1 行寄りの短い形式で表示される。

```text
チル場所: 💤 ふかふかチェア (Lv.8) / 次: 🔌 充電席 Lv.9
```

未解放状態の例:

```text
チル場所: まだ解放されていません / 次: 🪑 入口のベンチ Lv.1
```

### 詳細表示

`/intro-chill mine` では、場所名、必要レベル、タグ、説明、次の解放場所を複数行で表示する。

```text
💤 ふかふかチェア (Lv.8)
まったり / 休憩
ちょっと疲れた日に沈み込む席。
次の解放: 🔌 充電席 Lv.9
```

## デフォルト解放プリセット

| Lv | 表示 | タグ | 説明 |
| ---: | --- | --- | --- |
| 1 | 🪑 入口のベンチ | はじめまして / 気軽 | まずはここで、ゆっくり空気を眺める席。 |
| 2 | 🛋️ ロビーソファ | 雑談 / のんびり | 通りすがりの会話に混ざりやすい、やわらかい場所。 |
| 3 | 🪟 窓際スツール | ひと休み / 明るい | 外の気配を感じながら、少しだけ腰を下ろす席。 |
| 4 | ☕ 小さな丸テーブル | 少人数 / 気軽 | 近くの人と軽く話すのにちょうどいいテーブル。 |
| 5 | 🥤 カフェカウンター | 雑談 / 作業前 | 飲み物を片手に、その日の調子を整える場所。 |
| 6 | 📚 本棚のそば | 静か / 読書 | 会話も作業も、少し落ち着いた声になる一角。 |
| 7 | 🪴 観葉植物の横 | すみっこ / 安心 | ほどよく人の気配がある、静かなすみっこ。 |
| 8 | 💤 ふかふかチェア | まったり / 休憩 | ちょっと疲れた日に沈み込む席。 |
| 9 | 🔌 充電席 | 回復 / 作業 | 端末も気持ちも、じわっと充電していく場所。 |
| 10 | ☕ いつものカフェ席 | 定位置 / 雑談 | 顔なじみの会話が自然に始まる席。 |
| 12 | 📝 静かな作業机 | 集中 / 静か | 少し集中したい日に向いた、整った机。 |
| 14 | 📖 本棚奥の席 | 読書 / 隠れ家 | 本棚の奥で、話しかけられすぎずに過ごせる場所。 |
| 16 | 🌙 夜更かしテーブル | 夜 / 作業 | 遅い時間のゆるい作業と雑談が似合うテーブル。 |
| 18 | 🕯️ 半個室ソファ | 少人数 / 落ち着く | 少しこもって、近い人たちと過ごせるソファ。 |
| 20 | 🍵 チルラウンジ | 節目 / まったり | ここまで来た人のための、広めでゆるいラウンジ。 |
| 25 | 🌤️ 窓辺の作業部屋 | 集中 / 景色 | 景色を横目に、ゆっくり手を動かす部屋。 |
| 30 | 🌃 深夜の作業部屋 | 深夜 / 集中 | 静かな夜に、ぽつぽつ人が集まる作業部屋。 |
| 40 | 🌿 中庭ベンチ | 外気 / 休憩 | 少し外に出た気分で、肩の力を抜けるベンチ。 |
| 50 | 🔥 暖炉前 | 常連 / ぬくもり | 長くいる人たちの会話がゆっくり続く場所。 |
| 75 | 🌌 屋上テラス | 夜風 / 特別 | 夜風にあたりながら、静かに話せる特別席。 |
| 100 | 🏆 常連席 | 記念 / 定位置 | ここまで過ごしてきた人だけの、ちょっと誇らしい席。 |

## チル場所のユーザー操作

### `/intro-chill list`

- 自分の現在レベルを取得し、解放済みは `✓`、未解放は `□` で一覧表示する。
- レベル取得できない場合は、場所一覧のみ表示する。
- 結果は ephemeral。

### `/intro-chill set <place>`

- 自己紹介 embed に表示するチル場所を選ぶ。
- autocomplete 候補には、現在レベル以下で解放済みの場所だけが出る。
- `place` はレベル番号、場所名、絵文字付き場所名、`場所名 (Lv.N)` 形式で解決される。
- 現在レベルを取得できない場合は選択不可。
- 未解放の場所を指定した場合は拒否される。
- 設定は `user_chill_places` に永続化される。

### `/intro-chill clear`

- ユーザーの選択を削除する。
- 削除後は現在レベルで解放済みの最高場所が自動表示される。

### `/intro-chill mine`

- 現在表示されるチル場所と次の解放場所を詳細形式で表示する。
- レベル取得できない場合は表示できない。

### 自己紹介 embed のボタン

単体の自己紹介表示では「チル場所を設定」ボタンから本人だけが選択 UI を開ける。
ボタン押下時に再度レベルを確認し、解放済みの場所だけを Select に表示する。

## チル場所の管理者操作

管理者は `/intro-config chill-place` 配下でギルド単位のカスタム場所を管理できる。

### `/intro-config chill-place add <level> <name> [emoji]`

- `level`: 1 から 1000。
- `name`: 1 から 80 文字。
- `emoji`: 任意。1 から 40 文字。省略時は、同レベルのプリセットがあればその絵文字を引き継ぐ。
- プリセットと同じ `level` の場合、そのギルドでは名前と絵文字を上書きする。
- プリセットにない `level` の場合、新しい解放場所として追加する。
- 保存先は `guild_chill_places`。

プリセット上書きの場合、タグと説明はプリセットのものを引き継ぐ。新規レベルの場合、タグと説明は空になる。

### `/intro-config chill-place remove <level>`

- 指定レベルのカスタム設定だけを削除する。
- プリセット自体は削除されない。
- プリセットと同じレベルを remove すると、表示はプリセットに戻る。

### `/intro-config chill-place list`

- プリセットとカスタム設定を統合した、このギルドで有効なチル場所一覧を表示する。
- 結果は ephemeral。

## intro-bot 側 DB

### `guild_chill_places`

ギルド単位のチル場所カスタム設定。

| column | 内容 |
| --- | --- |
| `guild_id` | Discord guild ID |
| `required_level` | 解放レベル。1 以上 |
| `name` | 場所名。1 から 80 文字 |
| `emoji` | 任意表示絵文字。1 から 40 文字 |
| `updated_at` | 更新日時 |

主キーは `(guild_id, required_level)`。

### `user_chill_places`

ユーザーごとのチル場所選択。

| column | 内容 |
| --- | --- |
| `guild_id` | Discord guild ID |
| `user_id` | Discord user ID |
| `required_level` | 選択中の場所の解放レベル |
| `updated_at` | 更新日時 |

主キーは `(guild_id, user_id)`。

### `intro_messages`

外部 API 用に同期された、ユーザーごとの最新自己紹介。

| column | 内容 |
| --- | --- |
| `guild_id` | Discord guild ID |
| `user_id` | Discord user ID |
| `message_id` | 自己紹介メッセージ ID |
| `channel_id` | 自己紹介チャンネル ID |
| `content` | 自己紹介本文 |
| `jump_url` | Discord メッセージへのリンク |
| `image_url` | 最初に見つかった画像添付 URL |
| `author_display_name` | 表示名 |
| `author_avatar_url` | アバター URL |
| `created_at` | メッセージ作成日時 |
| `updated_at` | 同期更新日時 |

同じユーザーの古い投稿より新しい投稿だけが保持される。

## intro-bot 外部 API

`intro-bot` は別プロセスで読み取り専用 API を提供できる。

```http
GET /api/v1/guilds/{guild_id}/users/{user_id}/intro
```

認証は `Authorization: Bearer <INTRO_API_KEY>`。`INTRO_API_KEY` が未設定の場合は `EXTERNAL_API_KEY` を流用する。どちらも空なら常に `401`。

レスポンスには、同期済み自己紹介、level-bot から取得したレベル、解決済みチル場所、統計 URL が含まれる。

`level` が取得できない場合:

- `level` は `null`。
- `chill_place` は `null`。

`chill_place` の主な構造:

```json
{
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
    "name": "充電席"
  },
  "selected_locked": false,
  "display_text": "💤 ふかふかチェア (Lv.8)\nまったり / 休憩\nちょっと疲れた日に沈み込む席。\n次の解放: 🔌 充電席 Lv.9"
}
```

## 現状の注意点

- チル場所の解放は `level-bot` の総合レベルだけを見る。項目別レベルでは解放されない。
- チル場所は Discord チャンネル権限や実際のチャンネル作成とは連動していない。現状は自己紹介 embed 上の表示概念。
- level-bot 側で API が 404、非公開、タイムアウト、認証失敗などになると、intro-bot 側では単にレベルなし表示になる。
- level-bot のランキングは live voice を含まないため、個別ユーザーレベルと一時的に差が出る可能性がある。
- ユーザーが過去に選んだチル場所が後から管理者変更で未解放扱いになった場合、選択自体は残り、表示時に `selected_locked` として扱われる。
- `intro-bot` のチル場所カスタム削除はカスタム行の削除であり、プリセット削除ではない。

## 主な参照実装

- `intro-bot`: `bot.py`
- `intro-bot`: `api.py`
- `intro-bot`: `docs/usage.md`
- `level-bot`: `src/features/leveling/service.py`
- `level-bot`: `src/features/leveling/routes.py`
- `level-bot`: `src/cogs/tracking.py`
- `level-bot`: `src/features/guilds/service.py`
- `level-bot`: `src/database/models.py`
