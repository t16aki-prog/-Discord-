import asyncio
import threading
import logging
from aiohttp import web
from config import PORT

def run_keep_alive_server():
    """Renderのポート開放用ダミーWebサーバー（バックグラウンドスレッドで即時起動）"""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Bot is running healthy!", status=200)

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        loop.run_until_complete(site.start())
        print(f"Keep-Alive Webサーバー即時起動完了 (ポート: {PORT})", flush=True)
        loop.run_forever()
    except Exception as e:
        print(f"Webサーバーの起動スキップまたはエラー: {e}", flush=True)


def start_web_server_thread():
    """別スレッドでWebサーバーを立ち上げ、ポートを0.1秒で即座に開放する"""
    t = threading.Thread(target=run_keep_alive_server, daemon=True)
    t.start()
