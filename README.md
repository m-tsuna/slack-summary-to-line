# slack-summary-to-line

Slack の指定チャンネルから直近24時間のメッセージを取得し、OpenAI（gpt-4o-mini）で日本語の箇条書きに要約して、自分の LINE に送る Python のプログラムです。
重要な連絡を LINE で受け取り、見逃しを防ぐためのプロトタイプ（試作品）です。

```
Slack（conversations.history）──メッセージ──▶ main.py ──本文──▶ OpenAI（要約）
                                                 │
                                                 └──要約──▶ LINE（プッシュメッセージ）
```

## 事前準備

### 1. ライブラリ

```bash
pip install requests python-dotenv
```

（Anaconda には最初から入っています）

### 2. Slack アプリ（メッセージを読むボット）

1. [Slack API: Your Apps](https://api.slack.com/apps) →「Create New App」→ **Blank app** を選び、アプリ名とワークスペースを決める
2. Bot scopes に **`channels:history`** だけを追加する（招待された公開チャンネルのメッセージを読む権限）
3. ワークスペースにインストールし、「OAuth & Permissions」の **Bot User OAuth Token**（`xoxb-`）を控える
4. 読みたい公開チャンネルで `/invite @アプリ名` と打ってボットを招待し、チャンネル詳細の一番下にあるチャンネル ID（`C` で始まる）を控える

### 3. OpenAI

[OpenAI Platform](https://platform.openai.com/) で API キー（`sk-`）を作り、Billing でクレジットを入れておく（1回の要約は1円未満の見込み）。

### 4. LINE 公式アカウント（Messaging API）

LINE Developers でチャネルを作り、「Messaging API 設定」のチャネルアクセストークン（長期）と、「チャネル基本設定」の「あなたのユーザー ID」（`U` で始まる33文字）を控える。スマホの LINE で公式アカウントを友だち追加しておく。

## 秘密ファイル（.env）の用意の仕方

`.env.example` をコピーして名前を `.env` にし、`=` の後ろに控えた値を書きます。

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...
OPENAI_API_KEY=sk-...
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_USER_ID=U...
```

`.env` は `.gitignore` に書いてあり、GitHub には載りません。

## 使い方

```bash
python main.py            # 取得 → 要約 → LINE に送信
python main.py --dry-run  # LINE には送らず、送る予定の内容を画面に出すだけ（試し実行）
```

- メッセージが0件のときは、OpenAI も LINE も使わずに終わります
- 取得する時間（`LOOKBACK_HOURS`）・最大件数・モデル名は、`main.py` の先頭で変えられます

## うまくいかないとき

エラーの原因と直し方は、画面に日本語で表示されます。よくあるもの：

| 表示 | 直し方 |
|---|---|
| チャンネルが見つかりません | ボットをチャンネルに招待したか、チャンネル ID が正しいかを確かめる |
| メッセージを読む権限がありません | Bot Token Scopes に `channels:history` を追加し、再インストールする |
| OpenAI の残高が足りません | OpenAI の Billing でクレジットを確かめる |
| LINE のチャネルアクセストークンが正しくありません | LINE Developers でトークンを確かめて貼り直す |

詳しい決まりは [要件定義書.md](要件定義書.md) にあります。
