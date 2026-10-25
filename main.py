"""
Slack 要約 LINE 通知（5-1-1① Slack の情報を取得・要約して LINE に送るツール）

Slack の指定チャンネルから直近のメッセージを取得し、OpenAI で日本語の箇条書きに要約して、
自分の LINE に送る（プッシュメッセージ）。

使い方：
  python main.py            … 取得 → 要約 → LINE に送信
  python main.py --dry-run  … LINE には送らず、送る予定の内容を画面に出すだけ（試し実行）
"""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ============================================================
# 設定（ここをまとめて書いておく）
# ============================================================

# このプログラム（main.py）が置いてあるフォルダ
BASE_DIR = Path(__file__).resolve().parent

# 鍵などを書いておくファイル
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"

# .env の項目名
SLACK_TOKEN_NAME = "SLACK_BOT_TOKEN"
SLACK_CHANNEL_NAME = "SLACK_CHANNEL_ID"
OPENAI_KEY_NAME = "OPENAI_API_KEY"
LINE_TOKEN_NAME = "LINE_CHANNEL_ACCESS_TOKEN"
LINE_USER_ID_NAME = "LINE_USER_ID"

# 値の形（貼り間違いに早く気づくため）
SLACK_TOKEN_PATTERN = r"xoxb-\S+"            # Bot トークンは xoxb- で始まる
SLACK_CHANNEL_PATTERN = r"C[0-9A-Z]+"        # 公開チャンネルの ID は C で始まる
LINE_USER_ID_PATTERN = r"U[0-9a-f]{32}"      # U のあとに 0〜9・a〜f が 32 文字

# 何時間前までのメッセージを取るか
LOOKBACK_HOURS = 24

# 要約に使うメッセージの最大件数（新しい方から数える）
MAX_MESSAGES = 200

# Slack から1回にもらう件数（Slack のおすすめは 200 以下）
SLACK_PAGE_SIZE = 200

# 要約に使う OpenAI のモデルと、要約の頼み方
OPENAI_MODEL = "gpt-4o-mini"
SUMMARY_INSTRUCTION = (
    "あなたは Slack のメッセージを要約するアシスタントです。"
    "与えられたメッセージを読み、日本語の箇条書き（行頭は「・」）で要約してください。"
    "重要な連絡・締め切り・お願いごとを先に書き、全体で400字以内にしてください。"
    "メッセージに書かれていないことは書かないでください。"
)

# 各 API の URL
SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# LINE のテキストメッセージ1通の上限文字数（LINE の決まり）と、超えたときに付ける印
LINE_MAX_TEXT_LENGTH = 5000
TRUNCATED_MARK = "…（省略）"

# 返事を待つ秒数（要約は時間がかかることがあるので長めにする）
TIMEOUT_SECONDS = 15
OPENAI_TIMEOUT_SECONDS = 60

# 試し実行にするときの指定
DRY_RUN_OPTION = "--dry-run"


# ============================================================
# F1 設定の読み込み
# ============================================================
def load_settings():
    """
    .env から5つの項目を読み込み、{項目名: 値} の辞書で返す。
    空の項目や、形が違う項目があれば、まとめて知らせて終了する（F6）。
    """
    # .env の中身を「環境変数」（プログラムが読める設定値）として読み込む
    # （すでに同じ名前の環境変数があれば、そちらが優先される）
    load_dotenv(ENV_FILE)
    names = [SLACK_TOKEN_NAME, SLACK_CHANNEL_NAME, OPENAI_KEY_NAME, LINE_TOKEN_NAME, LINE_USER_ID_NAME]
    settings = {name: os.getenv(name, "").strip() for name in names}

    # 空の項目を集めて、まとめて知らせる
    missing = [name for name, value in settings.items() if not value]
    if missing:
        show_error_and_exit(
            f"{'、'.join(missing)} が設定されていません。\n"
            f"{ENV_EXAMPLE_FILE.name} をコピーして「{ENV_FILE.name}」という名前にし、= の後ろに値を書いてください。\n"
            f"置き場所: {BASE_DIR}"
        )

    # 形を確かめる（値そのものは表示しない）
    wrong = []
    if not re.fullmatch(SLACK_TOKEN_PATTERN, settings[SLACK_TOKEN_NAME]):
        wrong.append(f"・{SLACK_TOKEN_NAME}：Bot User OAuth Token（xoxb- で始まる）を貼ってください")
    if not re.fullmatch(SLACK_CHANNEL_PATTERN, settings[SLACK_CHANNEL_NAME]):
        wrong.append(f"・{SLACK_CHANNEL_NAME}：チャンネル名ではなく、チャンネル ID（C で始まる）を書いてください")
    if not re.fullmatch(LINE_USER_ID_PATTERN, settings[LINE_USER_ID_NAME]):
        wrong.append(
            f"・{LINE_USER_ID_NAME}：LINE Developers の「チャネル基本設定」にある「あなたのユーザー ID」"
            "（U で始まる33文字）を貼ってください（LINE アプリの「LINE ID」とは別物です）"
        )
    if wrong:
        show_error_and_exit(f"{ENV_FILE.name} の値の形が正しくありません。\n" + "\n".join(wrong))

    return settings


# ============================================================
# F2 Slack のメッセージ取得
# ============================================================
def fetch_slack_messages(token, channel_id):
    """
    Slack の conversations.history で、指定チャンネルの直近 LOOKBACK_HOURS 時間のメッセージを取得する。
    人が書いたメッセージだけを、古い順に並べたリスト [{"ts": 時刻, "text": 本文}, ...] で返す。

    - oldest に「この時刻より新しいものだけ」を、Unix 時間（1970年からの秒数）で渡す
    - 1回で取りきれないときは、返事の next_cursor を次の cursor に渡して続きをもらう
    - Slack は失敗しても HTTP 200 を返すので、返事の ok と error で成功・失敗を判断する
    """
    oldest = time.time() - LOOKBACK_HOURS * 60 * 60
    messages = []
    cursor = ""
    while True:
        # Slack の読み取り系の API は JSON の本文を受け付けないので、フォームの形（data=）で送る
        response = send_request(
            "Slack",
            SLACK_HISTORY_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"channel": channel_id, "oldest": f"{oldest:.6f}", "limit": SLACK_PAGE_SIZE, "cursor": cursor},
        )
        if response.status_code == 429:
            show_error_and_exit("Slack に短い時間に頼みすぎました。1分ほど待ってからやり直してください。")
        body = response.json()
        if not body.get("ok"):
            show_error_and_exit(explain_slack_error(body.get("error", ""), body.get("needed", "")))

        for message in body.get("messages", []):
            # 「参加しました」などの通知には subtype が付いているので除く
            if message.get("subtype") or not message.get("text"):
                continue
            messages.append({"ts": float(message["ts"]), "text": message["text"]})

        cursor = body.get("response_metadata", {}).get("next_cursor", "")
        if not body.get("has_more") or not cursor:
            break

    # 新しい方から MAX_MESSAGES 件だけ残し、読みやすいように古い順に並べ直す
    messages.sort(key=lambda m: m["ts"], reverse=True)
    return sorted(messages[:MAX_MESSAGES], key=lambda m: m["ts"])


def explain_slack_error(error, needed=""):
    """
    Slack が返したエラー名を、原因と直し方が分かる日本語の説明に直して返す。
    needed は、権限が足りないときに Slack が教えてくれる「必要な権限の名前」。
    """
    detail = f"詳細: {error}" + (f"（必要な権限: {needed}）" if needed else "")
    if error == "missing_scope" and needed == "groups:history":
        # 非公開チャンネルを読むには別の権限が要る。このツールは公開チャンネルだけを読む決まり
        return (
            "このチャンネルは非公開（プライベート）チャンネルのため、読めません。\n"
            "このツールは公開チャンネルだけを読みます。チャンネルの「設定」から\n"
            "「パブリックチャンネルに変更する」を選ぶか、公開チャンネルの ID を指定してください。\n" + detail
        )
    if error in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
        return (
            f"Slack のトークンが正しくありません。{ENV_FILE.name} の {SLACK_TOKEN_NAME} を、\n"
            "Slack アプリの「OAuth & Permissions」にある Bot User OAuth Token（xoxb-）で貼り直してください。\n" + detail
        )
    if error in ("channel_not_found", "not_in_channel"):
        # ボットを招待していないチャンネルは「見えない」扱いになり、存在しない ID と同じエラーになる
        return (
            "Slack のチャンネルが見つかりません。次の2つを確かめてください。\n"
            "  ・そのチャンネルで「/invite @アプリ名」と打ち、ボットを招待したか\n"
            f"  ・{ENV_FILE.name} の {SLACK_CHANNEL_NAME} が、チャンネル詳細の一番下にある ID（C で始まる）と同じか\n"
            + detail
        )
    if error == "missing_scope":
        return (
            "Slack アプリに、メッセージを読む権限がありません。\n"
            "「OAuth & Permissions」の Bot Token Scopes に channels:history を追加し、"
            "ワークスペースに再インストールしてください。\n" + detail
        )
    return "Slack からメッセージを取得できませんでした。\n" + detail


def format_messages(messages):
    """
    メッセージを「[10/25 14:30] 本文」の形の行にし、1つの文字列にまとめて返す（要約に渡す形）。
    """
    lines = []
    for message in messages:
        posted_at = datetime.fromtimestamp(message["ts"]).strftime("%m/%d %H:%M")
        # Slack は & < > を &amp; &lt; &gt; に置き換えて返すので、元の文字に戻す（&amp; は最後に戻す）
        text = message["text"].replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        lines.append(f"[{posted_at}] {text}")
    return "\n".join(lines)


# ============================================================
# F3 要約
# ============================================================
def summarize(api_key, messages_text):
    """
    OpenAI の Chat Completions API に、要約の頼み方（system）とメッセージ（user）を送り、
    返ってきた要約の文を返す。
    """
    response = send_request(
        "OpenAI",
        OPENAI_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": SUMMARY_INSTRUCTION},
                {"role": "user", "content": messages_text},
            ],
        },
        timeout=OPENAI_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        show_error_and_exit(explain_openai_error(response))
    return response.json()["choices"][0]["message"]["content"].strip()


def explain_openai_error(response):
    """
    OpenAI が返した失敗の返事を、原因と直し方が分かる日本語の説明に直して返す。
    """
    # OpenAI は失敗の理由を {"error": {"message": ..., "code": ...}} の形で返す
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code = error.get("code") or error.get("type") or ""
    detail = f"詳細: HTTP {response.status_code} {code}".rstrip()

    if response.status_code == 401:
        return (
            f"OpenAI の API キーが正しくありません。{ENV_FILE.name} の {OPENAI_KEY_NAME} を、\n"
            "OpenAI の API keys の画面で作ったキー（sk- で始まる）で貼り直してください。\n" + detail
        )
    if response.status_code == 429 and code == "insufficient_quota":
        return (
            "OpenAI の残高（クレジット）が足りません。OpenAI の Billing の画面で残高を確かめてください。\n" + detail
        )
    if response.status_code == 429:
        return "OpenAI に短い時間に頼みすぎました。少し待ってからやり直してください。\n" + detail
    if response.status_code == 404:
        return f"OpenAI のモデル「{OPENAI_MODEL}」が使えません。main.py の OPENAI_MODEL を確かめてください。\n" + detail
    return "OpenAI で要約できませんでした。\n" + detail


# ============================================================
# F4 LINE に送信
# ============================================================
def build_line_text(channel_id, message_count, summary):
    """
    LINE に送る文を作る。1通の上限（5000字）を超えたら切って「…（省略）」を付ける。
    """
    text = f"【Slack の要約】#{channel_id} 直近{LOOKBACK_HOURS}時間・{message_count}件\n{summary}"
    if len(text) > LINE_MAX_TEXT_LENGTH:
        text = text[: LINE_MAX_TEXT_LENGTH - len(TRUNCATED_MARK)] + TRUNCATED_MARK
    return text


def push_to_line(token, user_id, text):
    """
    LINE のプッシュメッセージの API で、自分（ユーザー ID の人）にテキストを1通送る。
    成功すると HTTP 200 が返る。失敗は 400・401 などのステータスで返る。
    """
    response = send_request(
        "LINE",
        LINE_PUSH_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
    )
    if response.status_code != 200:
        show_error_and_exit(explain_line_error(response))


def explain_line_error(response):
    """
    LINE が返した失敗の返事を、原因と直し方が分かる日本語の説明に直して返す。
    """
    try:
        reason = response.json().get("message", "")
    except ValueError:
        reason = ""
    detail = f"詳細: HTTP {response.status_code} {reason}".rstrip()

    if response.status_code == 401:
        return (
            f"LINE のチャネルアクセストークンが正しくありません。{ENV_FILE.name} の {LINE_TOKEN_NAME} が、\n"
            "LINE Developers の「Messaging API 設定」にあるチャネルアクセストークン（長期）と同じか確認してください。\n"
            + detail
        )
    if response.status_code == 400:
        return (
            f"LINE がメッセージを受け付けませんでした。{ENV_FILE.name} の {LINE_USER_ID_NAME} が、\n"
            "このチャネルの「チャネル基本設定」にある「あなたのユーザー ID」と同じか確認してください。\n" + detail
        )
    if response.status_code == 429:
        return (
            "LINE で送れる数の上限を超えました（無料プランは月200通。来月まで待つか、少し待ってやり直す）。\n" + detail
        )
    return "LINE への送信に失敗しました。\n" + detail


# ============================================================
# F6 エラー表示（共通）
# ============================================================
def send_request(service, url, timeout=TIMEOUT_SECONDS, **kwargs):
    """
    API に POST で頼みごとを送り、返事を返す（Slack・OpenAI・LINE で共通）。
    つながらない・時間切れのときは、どのサービスで失敗したかを表示して終了する。
    """
    try:
        return requests.post(url, timeout=timeout, **kwargs)
    except requests.Timeout:
        show_error_and_exit(f"{service} から {timeout} 秒たっても応答がありませんでした。時間をおいてやり直してください。")
    except requests.RequestException as error:
        # エラーの文字列をそのまま出すと鍵などが出ることがあるので、種類の名前だけを出す
        show_error_and_exit(
            f"{service} につながりませんでした。インターネット接続を確認してください。\n"
            f"詳細: {type(error).__name__}"
        )


def show_error_and_exit(message):
    """
    原因が分かる日本語のエラーメッセージを表示して、プログラムを終了する。
    """
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(1)


# ============================================================
# メインの処理（上の関数を順番に呼ぶ）
# ============================================================
def main():
    """
    プログラム全体の流れ：
    設定の読み込み → Slack から取得 → OpenAI で要約 → LINE に送信（--dry-run なら画面に出すだけ）
    """
    # F5 試し実行かどうか（--dry-run 以外の指定は間違いとして知らせる）
    options = sys.argv[1:]
    if any(option != DRY_RUN_OPTION for option in options):
        show_error_and_exit(f"指定できるのは {DRY_RUN_OPTION} だけです。例：python main.py {DRY_RUN_OPTION}")
    dry_run = DRY_RUN_OPTION in options

    try:
        # F1 設定の読み込み（API を呼ぶ前に確かめる）
        settings = load_settings()
        channel_id = settings[SLACK_CHANNEL_NAME]

        # F2 Slack のメッセージ取得
        print(f"Slack からメッセージを取得しています…（直近{LOOKBACK_HOURS}時間）")
        messages = fetch_slack_messages(settings[SLACK_TOKEN_NAME], channel_id)
        print(f"  → {len(messages)}件ありました")
        if not messages:
            # 0件なら OpenAI も LINE も使わない（料金と LINE の無料枠を使わないため）
            print("新しいメッセージはありませんでした。LINE には送りません。")
            return

        # F3 要約
        print("OpenAI で要約しています…")
        summary = summarize(settings[OPENAI_KEY_NAME], format_messages(messages))
        text = build_line_text(channel_id, len(messages), summary)

        # F5 試し実行なら、送る予定の内容を見せて終わる
        if dry_run:
            print("----- 送る予定の内容（LINE には送っていません）-----")
            print(text)
            print("-" * 40)
            return

        # F4 LINE に送信
        print("LINE に送信しています…")
        push_to_line(settings[LINE_TOKEN_NAME], settings[LINE_USER_ID_NAME], text)
        print("✅ LINE に送りました。スマホで確かめてください。")

    except KeyboardInterrupt:
        # Ctrl + C で途中でやめた場合
        print("\n中止しました。")
        sys.exit(1)


# このファイルを直接実行したときだけ main() を動かす
if __name__ == "__main__":
    main()
