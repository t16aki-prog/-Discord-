import logging
from config import TOKEN
from web_server import start_web_server_thread
from bot import bot

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    print("Botの起動処理を開始します...", flush=True)

    # 1. Renderのヘルスチェック（ポートリッスン）を通すため、即座にWebサーバーを別スレッドで開始
    start_web_server_thread()

    # 2. Discord Bot の起動
    if not TOKEN:
        print("エラー: .env ファイルに DISCORD_TOKEN または DISCORD_BOT_TOKEN が設定されていません。", flush=True)
    else:
        print("トークンを読み込みました。Discordサーバーへ接続中...", flush=True)
        bot.run(TOKEN)
