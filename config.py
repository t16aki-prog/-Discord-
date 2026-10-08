import os
import sys
import re
import logging
from dotenv import load_dotenv

# Windowsコンソールでの文字化け・UnicodeEncodeError対策
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# .envファイルから環境変数を読み込む
load_dotenv()

# Discord Bot トークン
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

# Turso (クラウドSQLite) 設定
raw_turso_url = (os.getenv("TURSO_DATABASE_URL") or "").strip().strip("'\"")
raw_turso_token = (os.getenv("TURSO_AUTH_TOKEN") or "").strip().strip("'\"")

# URLのプロトコルを安全な https:// 形式に正規化 (WebSocket 400エラー防止)
if raw_turso_url:
    TURSO_DATABASE_URL = re.sub(r"^(libsql|wss|http)://", "https://", raw_turso_url)
    if not TURSO_DATABASE_URL.startswith("https://"):
        TURSO_DATABASE_URL = "https://" + TURSO_DATABASE_URL
else:
    TURSO_DATABASE_URL = None

TURSO_AUTH_TOKEN = raw_turso_token if raw_turso_token else None

# ローカルSQLiteファイル名
DB_FILE = "bot_database.db"

# サーバーポート
PORT = int(os.getenv("PORT", 8080))

# 起動時スラッシュコマンド同期フラグ
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "false").lower() in ("true", "1")
