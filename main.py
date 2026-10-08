import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import os
import sys
import re
import logging
import asyncio
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import threading
import aiohttp
from aiohttp import web

# Windowsコンソールでの文字化け・UnicodeEncodeError対策
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# .envファイルから環境変数を読み込む
load_dotenv()
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
DB_FILE = "bot_database.db"

# --- データベース抽象化ヘルパー (Turso & SQLite 両対応) ---

_turso_shared_client = None


class LibsqlCursorWrapper:
    """Turso の結果を aiosqlite カーソル互換で扱うためのラッパー"""
    def __init__(self, result_set):
        self._rs = result_set
        self.rowcount = getattr(result_set, "rows_affected", 0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def fetchone(self):
        if not self._rs or not getattr(self._rs, "rows", None) or len(self._rs.rows) == 0:
            return None
        return tuple(self._rs.rows[0])

    async def fetchall(self):
        if not self._rs or not getattr(self._rs, "rows", None):
            return []
        return [tuple(r) for r in self._rs.rows]


class Database:
    """ローカルSQLiteとTursoクラウドDBを自動判別して透過的に操作するクラス"""
    @classmethod
    def connect(cls):
        return cls()

    async def __aenter__(self):
        global _turso_shared_client
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            try:
                import libsql_client
                if _turso_shared_client is None:
                    _turso_shared_client = libsql_client.create_client(
                        url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
                    )
                self._client = _turso_shared_client
                self._is_turso = True
            except Exception as e:
                print(f"Turso接続エラー ({e})。ローカルSQLiteにフォールバック", flush=True)
                self._conn = await aiosqlite.connect(DB_FILE)
                self._is_turso = False
        else:
            self._conn = await aiosqlite.connect(DB_FILE)
            self._is_turso = False
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self._is_turso and hasattr(self, "_conn"):
            await self._conn.close()

    async def execute(self, sql, params=()):
        if self._is_turso:
            global _turso_shared_client
            for attempt in range(2):
                try:
                    rs = await self._client.execute(sql, list(params))
                    return LibsqlCursorWrapper(rs)
                except Exception as e:
                    err_msg = str(e).lower()
                    if attempt == 0 and ("disconnected" in err_msg or "closed" in err_msg or "reset" in err_msg):
                        if _turso_shared_client is not None:
                            try:
                                await _turso_shared_client.close()
                            except Exception:
                                pass
                            _turso_shared_client = None
                        import libsql_client
                        _turso_shared_client = libsql_client.create_client(
                            url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
                        )
                        self._client = _turso_shared_client
                        continue
                    print(f"Tursoクエリエラー: {e}", flush=True)
                    raise e
        else:
            return await self._conn.execute(sql, params)

    async def commit(self):
        if not self._is_turso and hasattr(self, "_conn"):
            await self._conn.commit()


# --- データベース初期化 ---

async def init_db():
    """全テーブルの作成と既存DBへのマイグレーションを行う"""
    async with Database.connect() as db:
        # サーバーごとの設定テーブル（マルチサーバー対応の中核）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                admin_channel_id INTEGER,
                attendance_channel_id INTEGER,
                room_status_channel_id INTEGER
            )
        """)
        # イベント（出欠）テーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                name TEXT,
                date TEXT,
                event_date TEXT
            )
        """)
        # 出欠状況テーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendances (
                message_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                status TEXT,
                PRIMARY KEY (message_id, user_id)
            )
        """)
        # 部屋テーブル（guild_id付き複合主キー）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                guild_id INTEGER,
                name TEXT,
                is_open INTEGER,
                last_updated TEXT,
                PRIMARY KEY (guild_id, name)
            )
        """)
        # 部屋パネルメッセージID保存用
        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_panel (
                guild_id INTEGER PRIMARY KEY,
                message_id INTEGER
            )
        """)
        # 既存DBのマイグレーション（カラムがない場合のみ追加）
        for migration_sql in [
            "ALTER TABLE events ADD COLUMN guild_id INTEGER",
            "ALTER TABLE events ADD COLUMN event_date TEXT",
        ]:
            try:
                await db.execute(migration_sql)
            except Exception:
                pass
        await db.commit()


# --- guild_settings ヘルパー関数 ---

async def get_guild_settings(guild_id: int):
    """指定サーバーの設定をDBから取得する"""
    async with Database.connect() as db:
        async with await db.execute(
            "SELECT admin_channel_id, attendance_channel_id, room_status_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {
        "admin_channel_id": row[0],
        "attendance_channel_id": row[1],
        "room_status_channel_id": row[2],
    }


# --- Render スリープ防止用 Keep-Alive Webサーバー ---

def run_keep_alive_server():
    """Renderのポート開放用ダミーWebサーバー（バックグラウンドスレッドで即時起動）"""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Bot is running healthy!", status=200)

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    port = int(os.getenv("PORT", 8080))

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port)
        loop.run_until_complete(site.start())
        print(f"Keep-Alive Webサーバー即時起動完了 (ポート: {port})", flush=True)
        loop.run_forever()
    except Exception as e:
        print(f"Webサーバーの起動スキップまたはエラー: {e}", flush=True)


def start_web_server_thread():
    """別スレッドでWebサーバーを立ち上げ、ポートを0.1秒で即座に開放する"""
    t = threading.Thread(target=run_keep_alive_server, daemon=True)
    t.start()


# --- UI コンポーネント (Persistent Views & Modals) ---

class AttendanceView(discord.ui.View):
    """出欠入力ボタンのView"""
    def __init__(self):
        super().__init__(timeout=None)

    async def update_attendance(self, interaction: discord.Interaction, status: str):
        await interaction.response.defer(ephemeral=True)
        today_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        try:
            async with Database.connect() as db:
                async with await db.execute(
                    "SELECT name, date, event_date FROM events WHERE message_id = ?", (interaction.message.id,)
                ) as cursor:
                    event_row = await cursor.fetchone()

                if not event_row:
                    await interaction.followup.send("イベントが見つかりませんでした。", ephemeral=True)
                    return

                event_name, date_str, event_date = event_row

                if event_date and today_str < event_date:
                    await interaction.followup.send(
                        f"このイベント（{event_date}）はまだ開催日ではありません。当日になってから登録してください。",
                        ephemeral=True,
                    )
                    return
                elif event_date and today_str > event_date:
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass
                    await interaction.followup.send(
                        f"このイベント（{event_date}）は過去のイベントのため、古いパネルを削除しました。",
                        ephemeral=True,
                    )
                    return

                await db.execute(
                    """
                    INSERT INTO attendances (message_id, user_id, user_name, status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(message_id, user_id) DO UPDATE SET status=excluded.status
                    """,
                    (interaction.message.id, interaction.user.id, interaction.user.display_name, status),
                )
                await db.commit()

                async with await db.execute(
                    "SELECT user_name, status FROM attendances WHERE message_id = ?", (interaction.message.id,)
                ) as att_cursor:
                    attendances = await att_cursor.fetchall()

            att_yes = [a[0] for a in attendances if a[1] == "出席"]
            att_no = [a[0] for a in attendances if a[1] == "欠席"]
            att_maybe = [a[0] for a in attendances if a[1] == "遅刻/未定"]

            if event_date is None:
                desc = f"**日時:** {date_str}\n\n下のボタンから出欠を入力してください！\n\n"
            else:
                desc = f"**日時:** {date_str}\n\n下のボタンから出欠を入力してください！\n※開催日({event_date})になるまで登録できません。\n\n"

            desc += f"**【出席】 ({len(att_yes)}名)**\n" + (", ".join(att_yes) if att_yes else "なし") + "\n\n"
            desc += f"**【欠席】 ({len(att_no)}名)**\n" + (", ".join(att_no) if att_no else "なし") + "\n\n"
            desc += f"**【遅刻/未定】 ({len(att_maybe)}名)**\n" + (", ".join(att_maybe) if att_maybe else "なし")

            embed = discord.Embed(title=f"📅 {event_name}", description=desc, color=discord.Color.blue())
            await interaction.message.edit(embed=embed)
            await interaction.followup.send(
                f"あなたの出欠を「{status}」で登録しました！",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"出欠の登録中にエラーが発生しました: {e}", ephemeral=True)

    @discord.ui.button(label="出席", style=discord.ButtonStyle.success, emoji="⭕", custom_id="attend_yes")
    async def btn_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_attendance(interaction, "出席")

    @discord.ui.button(label="欠席", style=discord.ButtonStyle.danger, emoji="❌", custom_id="attend_no")
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_attendance(interaction, "欠席")

    @discord.ui.button(label="遅刻/未定", style=discord.ButtonStyle.secondary, emoji="🔺", custom_id="attend_maybe")
    async def btn_maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_attendance(interaction, "遅刻/未定")


class RoomStatusSelect(discord.ui.Select):
    """部屋状態切り替え用セレクトメニュー"""
    def __init__(self, rooms: list):
        options = []
        if not rooms:
            options.append(discord.SelectOption(label="登録されている部屋がありません", value="none"))
        else:
            for room in rooms:
                name = room[0]
                is_open = room[1]
                status_emoji = "🟢" if is_open else "🔴"
                status_text = "開放中" if is_open else "施錠中"
                options.append(
                    discord.SelectOption(
                        label=f"{name}",
                        description=f"現在の状態: {status_text}",
                        emoji=status_emoji,
                        value=name,
                    )
                )
        super().__init__(
            placeholder="状態を変更する部屋を選択してください...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="room_status_select",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("部屋が登録されていません。", ephemeral=True)
            return

        room_name = self.values[0]
        guild_id = interaction.guild_id
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with Database.connect() as db:
                async with await db.execute(
                    "SELECT is_open FROM rooms WHERE guild_id = ? AND name = ?", (guild_id, room_name)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        new_status = 0 if row[0] == 1 else 1
                        await db.execute(
                            "UPDATE rooms SET is_open = ?, last_updated = ? WHERE guild_id = ? AND name = ?",
                            (new_status, now, guild_id, room_name),
                        )
                        await db.commit()

                        async with await db.execute(
                            "SELECT name, is_open, last_updated FROM rooms WHERE guild_id = ? ORDER BY name", (guild_id,)
                        ) as room_cursor:
                            rooms = await room_cursor.fetchall()

                        desc = ""
                        for r in rooms:
                            rname, is_open_val, last_up = r
                            status_emoji = "🟢" if is_open_val else "🔴"
                            status_text_val = "開放中" if is_open_val else "施錠中"
                            desc += f"{status_emoji} **{rname}** : {status_text_val} (更新: {last_up})\n"

                        embed = discord.Embed(title="🏢 部室・施設の利用状況", description=desc, color=discord.Color.green())
                        view = RoomStatusView(rooms)
                        await interaction.message.edit(embed=embed, view=view)

                        status_text = "開放" if new_status == 1 else "施錠"
                        await interaction.response.send_message(
                            f"「{room_name}」を【{status_text}】に変更しました！",
                            ephemeral=True,
                        )
                    else:
                        await interaction.response.send_message("部屋が見つかりませんでした。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"状態の変更中にエラーが発生しました: {e}", ephemeral=True)


class RoomStatusView(discord.ui.View):
    def __init__(self, rooms: list):
        super().__init__(timeout=None)
        self.add_item(RoomStatusSelect(rooms))


class EventSelect(discord.ui.Select):
    """イベント選択用セレクトメニュー"""
    def __init__(self, events, attendance_channel_id: int):
        self._attendance_channel_id = attendance_channel_id
        options = []
        for event in events:
            date_str = event.start_time.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
            options.append(
                discord.SelectOption(
                    label=event.name[:100],
                    description=f"開催日: {date_str}",
                    value=str(event.id),
                )
            )
        super().__init__(placeholder="パネルを設置するイベントを選択...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        event_id = int(self.values[0])
        guild_event = interaction.guild.get_scheduled_event(event_id)
        if not guild_event:
            await interaction.followup.send("イベントが見つかりませんでした。", ephemeral=True)
            return

        date_str = guild_event.start_time.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        channel = interaction.client.get_channel(self._attendance_channel_id)
        if not channel:
            await interaction.followup.send("出欠チャンネルが見つかりません。`/setup` で設定を確認してください。", ephemeral=True)
            return

        display_time = guild_event.start_time.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y年%m月%d日 %H:%M")
        embed = discord.Embed(
            title=f"📅 {guild_event.name}",
            description=(
                f"**日時:** {display_time}\n\n下のボタンから出欠を入力してください！\n"
                f"※開催日({date_str})になるまで登録できません。\n\n"
                "**【出席】**\n\n**【欠席】**\n\n**【遅刻/未定】**\n"
            ),
            color=discord.Color.blue(),
        )
        if guild_event.cover_image:
            embed.set_image(url=guild_event.cover_image.url)

        view = AttendanceView()
        msg = await channel.send(embed=embed, view=view)
        async with Database.connect() as db:
            await db.execute(
                "INSERT INTO events (message_id, guild_id, name, date, event_date) VALUES (?, ?, ?, ?, ?)",
                (msg.id, interaction.guild_id, guild_event.name, display_time, date_str),
            )
            await db.commit()
        await interaction.followup.send(f"出欠パネルを作成しました: {msg.jump_url}", ephemeral=True)


class EventSelectView(discord.ui.View):
    def __init__(self, events, attendance_channel_id: int):
        super().__init__(timeout=60.0)
        self.add_item(EventSelect(events, attendance_channel_id))


class CreateEventModal(discord.ui.Modal, title="出欠イベントの作成 (手動)"):
    """手動出欠イベント作成用のModal"""
    event_name = discord.ui.TextInput(label="イベント名", placeholder="例: 〇〇大会 ミーティング", required=True)
    event_date_input = discord.ui.TextInput(label="日付・時間 (表示用)", placeholder="例: 10月1日 15:00〜", required=True)

    def __init__(self, attendance_channel_id: int):
        super().__init__()
        self._attendance_channel_id = attendance_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.client.get_channel(self._attendance_channel_id)
        if not channel:
            await interaction.followup.send("出欠チャンネルが見つかりません。`/setup` で設定を確認してください。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📅 {self.event_name.value}",
            description=(
                f"**日時:** {self.event_date_input.value}\n\n下のボタンから出欠を入力してください！\n\n"
                "**【出席】**\n\n**【欠席】**\n\n**【遅刻/未定】**\n"
            ),
            color=discord.Color.blue(),
        )
        view = AttendanceView()
        msg = await channel.send(embed=embed, view=view)
        async with Database.connect() as db:
            await db.execute(
                "INSERT INTO events (message_id, guild_id, name, date, event_date) VALUES (?, ?, ?, ?, ?)",
                (msg.id, interaction.guild_id, self.event_name.value, self.event_date_input.value, None),
            )
            await db.commit()
        await interaction.followup.send(f"出欠パネルを設置しました: {msg.jump_url}", ephemeral=True)


async def update_room_panel(interaction: discord.Interaction):
    """部屋の追加・削除・状態変更時に、設置されている部屋パネルを自動更新する"""
    guild_id = interaction.guild_id
    settings = await get_guild_settings(guild_id)
    if not settings or not settings.get("room_status_channel_id"):
        return

    async with Database.connect() as db:
        async with await db.execute("SELECT message_id FROM room_panel WHERE guild_id = ?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return
            message_id = row[0]

        async with await db.execute(
            "SELECT name, is_open, last_updated FROM rooms WHERE guild_id = ? ORDER BY name", (guild_id,)
        ) as cursor:
            rooms = await cursor.fetchall()

    channel = interaction.client.get_channel(settings["room_status_channel_id"])
    if not channel:
        return

    try:
        msg = await channel.fetch_message(message_id)
        desc = ""
        for r in rooms:
            rname, is_open_val, last_up = r
            status_emoji = "🟢" if is_open_val else "🔴"
            status_text_val = "開放中" if is_open_val else "施錠中"
            last_up_str = last_up if last_up else "未更新"
            desc += f"{status_emoji} **{rname}** : {status_text_val} (更新: {last_up_str})\n"

        if not desc:
            desc = "部屋が登録されていません。"

        embed = discord.Embed(title="🏢 部室・施設の利用状況", description=desc, color=discord.Color.green())
        view = RoomStatusView(rooms)
        await msg.edit(embed=embed, view=view)
    except Exception as e:
        logging.getLogger("discord").error(f"Failed to auto-update room panel: {e}")


class AddRoomModal(discord.ui.Modal, title="部屋の追加"):
    """部屋追加用のModal"""
    room_name = discord.ui.TextInput(label="部屋の名前", placeholder="例: 部室A", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.room_name.value.strip()
        guild_id = interaction.guild_id
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with Database.connect() as db:
            try:
                await db.execute(
                    "INSERT INTO rooms (guild_id, name, is_open, last_updated) VALUES (?, ?, 0, ?)",
                    (guild_id, name, now),
                )
                await db.commit()
                await interaction.response.send_message(f"部屋「{name}」を追加しました！", ephemeral=True)
                await update_room_panel(interaction)
            except Exception as e:
                logging.getLogger("discord").error(f"Failed to add room '{name}': {e}")
                await interaction.response.send_message(
                    f"部屋「{name}」は既に存在するか、追加に失敗しました。", ephemeral=True
                )


class DeleteRoomModal(discord.ui.Modal, title="部屋の削除"):
    """部屋削除用のModal"""
    room_name = discord.ui.TextInput(label="削除する部屋の名前", placeholder="正確に入力してください", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.room_name.value.strip()
        guild_id = interaction.guild_id
        async with Database.connect() as db:
            cursor = await db.execute("DELETE FROM rooms WHERE guild_id = ? AND name = ?", (guild_id, name))
            if cursor.rowcount > 0:
                await db.commit()
                await interaction.response.send_message(f"部屋「{name}」を削除しました。", ephemeral=True)
                await update_room_panel(interaction)
            else:
                await interaction.response.send_message(f"部屋「{name}」は見つかりませんでした。", ephemeral=True)


class AdminPanelView(discord.ui.View):
    """Bot管理パネルのView"""
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内でのみ実行可能です。", ephemeral=True)
            return False
        perms = interaction.user.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_channels:
            return True
        await interaction.response.send_message(
            "⚠️ **操作権限がありません**\n「管理者」または「サーバー/チャンネルの管理」権限が必要です。",
            ephemeral=True,
        )
        return False

    async def _get_settings_or_warn(self, interaction: discord.Interaction):
        settings = await get_guild_settings(interaction.guild_id)
        if not settings:
            await interaction.response.send_message(
                "⚠️ チャンネルの設定が完了していません。先に `/setup` コマンドでチャンネルを設定してください。",
                ephemeral=True,
            )
        return settings

    @discord.ui.button(label="本日のイベントから出欠", style=discord.ButtonStyle.primary, emoji="📅", custom_id="admin_create_event_discord")
    async def btn_create_event_discord(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await self._get_settings_or_warn(interaction)
        if not settings:
            return
        today_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        events = [
            e for e in interaction.guild.scheduled_events
            if e.start_time.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d") == today_str
        ]
        events.sort(key=lambda e: e.start_time)
        if not events:
            await interaction.response.send_message(
                "本日のDiscordイベントは見つかりませんでした。\n事前にDiscord上部の「イベント」から予定を作成してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "本日のイベントから、出欠パネルを設置するものを選択してください。",
            view=EventSelectView(events, settings["attendance_channel_id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="手動で出欠作成", style=discord.ButtonStyle.primary, emoji="📝", custom_id="admin_create_event_manual")
    async def btn_create_event_manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await self._get_settings_or_warn(interaction)
        if not settings:
            return
        await interaction.response.send_modal(CreateEventModal(settings["attendance_channel_id"]))

    @discord.ui.button(label="部屋を追加", style=discord.ButtonStyle.success, emoji="🏠", custom_id="admin_add_room")
    async def btn_add_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddRoomModal())

    @discord.ui.button(label="部屋を削除", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="admin_delete_room")
    async def btn_delete_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeleteRoomModal())

    @discord.ui.button(label="部屋パネルを再設置", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="admin_reset_room_panel")
    async def btn_reset_room_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await self._get_settings_or_warn(interaction)
        if not settings:
            return
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel = interaction.client.get_channel(settings["room_status_channel_id"])
        if not channel:
            await interaction.followup.send("部室状況チャンネルが見つかりません。`/setup` で設定を確認してください。", ephemeral=True)
            return
        try:
            await channel.purge(check=lambda m: m.author == interaction.client.user, limit=50)
        except Exception as e:
            logging.getLogger("discord").error(f"Failed to purge room channel: {e}")

        async with Database.connect() as db:
            async with await db.execute(
                "SELECT name, is_open FROM rooms WHERE guild_id = ? ORDER BY name", (guild_id,)
            ) as cursor:
                rooms = await cursor.fetchall()

        view = RoomStatusView(rooms)
        embed = discord.Embed(title="🏢 部室・施設の利用状況", description="ローディング中...", color=discord.Color.green())
        msg = await channel.send(embed=embed, view=view)

        async with Database.connect() as db:
            await db.execute(
                "INSERT INTO room_panel (guild_id, message_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET message_id = excluded.message_id",
                (guild_id, msg.id),
            )
            await db.commit()
        await update_room_panel(interaction)
        await interaction.followup.send("部室状況パネルを再設置しました！", ephemeral=True)


# --- Bot 本体 ---

class CircleManagerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guild_scheduled_events = True
        intents.message_content = True  # !sync コマンド用
        super().__init__(command_prefix="!", intents=intents)
        self._ready_once = False

    async def setup_hook(self):
        await init_db()

        # 永続Viewの登録（再起動後もボタンが機能するよう）
        self.add_view(AdminPanelView())
        self.add_view(AttendanceView())

        # 部屋パネルのViewを全サーバー分登録
        try:
            async with Database.connect() as db:
                async with await db.execute("SELECT DISTINCT guild_id FROM rooms") as cursor:
                    guild_rows = await cursor.fetchall()
            for (gid,) in guild_rows:
                async with Database.connect() as db:
                    async with await db.execute(
                        "SELECT name, is_open FROM rooms WHERE guild_id = ? ORDER BY name", (gid,)
                    ) as cursor:
                        rooms = await cursor.fetchall()
                self.add_view(RoomStatusView(rooms))
        except Exception as e:
            logging.getLogger("discord").error(f"部屋パネルViewの初期化エラー: {e}")

        # スラッシュコマンド同期（環境変数 SYNC_COMMANDS=true の時のみ実行してレートリミットを防止）
        should_sync = os.getenv("SYNC_COMMANDS", "false").lower() in ("true", "1")
        if should_sync:
            try:
                synced = await self.tree.sync()
                print(f"スラッシュコマンドを {len(synced)} 件同期しました。", flush=True)
            except Exception as e:
                print(f"スラッシュコマンド同期スキップ（一時制限またはエラー）: {e}", flush=True)
        else:
            print("スラッシュコマンド同期はスキップされました（手動同期: チャットで !sync を実行するか SYNC_COMMANDS=true を設定）", flush=True)

        # 過去イベントの定期クリーンアップタスク開始（10分おき）
        if not self.cleanup_past_events.is_running():
            self.cleanup_past_events.start()

    async def on_ready(self):
        logger = logging.getLogger("discord")
        if self._ready_once:
            logger.info(f"Botセッション再接続完了: {self.user}")
            return
        self._ready_once = True

        logger.info("========================================")
        logger.info(f"Botログイン成功: {self.user} (ID: {self.user.id})")
        logger.info(f"参加中のサーバー数: {len(self.guilds)}")
        for guild in self.guilds:
            logger.info(f"   - {guild.name} (ID: {guild.id})")
        logger.info("========================================")

    async def on_guild_join(self, guild: discord.Guild):
        """新しいサーバーに参加したときセットアップを促すメッセージを送る"""
        logger = logging.getLogger("discord")
        logger.info(f"新しいサーバーに参加: {guild.name} (ID: {guild.id})")
        target_channel = guild.system_channel
        if not target_channel:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_channel = ch
                    break
        if target_channel:
            embed = discord.Embed(
                title="👋 サークル管理Botが参加しました！",
                description=(
                    "はじめまして！このBotは **出欠管理** と **部室・施設状況の管理** を自動化します。\n\n"
                    "**【最初にやること】**\n"
                    "管理者が `/setup` コマンドを実行して、各機能で使うチャンネルを設定してください。\n\n"
                    "```\n"
                    "/setup\n"
                    "  admin_channel      : 管理パネルを設置するチャンネル\n"
                    "  attendance_channel : 出欠パネルを投稿するチャンネル\n"
                    "  room_status_channel: 部室状況を表示するチャンネル\n"
                    "```\n\n"
                    "設定完了後、管理パネルが自動で設置され、すぐに使い始めることができます！"
                ),
                color=discord.Color.blurple(),
            )
            try:
                await target_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"ウェルカムメッセージ送信失敗 ({guild.name}): {e}")

    async def _ensure_admin_panel(self, guild: discord.Guild):
        """指定サーバーの管理チャンネルに管理パネルが存在しなければ設置する"""
        logger = logging.getLogger("discord")
        settings = await get_guild_settings(guild.id)
        if not settings or not settings.get("admin_channel_id"):
            return
        try:
            admin_channel = self.get_channel(settings["admin_channel_id"])
            if not admin_channel:
                admin_channel = await self.fetch_channel(settings["admin_channel_id"])
        except Exception as e:
            logger.error(f"管理チャンネル取得失敗 ({guild.name}): {e}")
            return
        panel_found = False
        try:
            async for message in admin_channel.history(limit=15):
                if message.author == self.user and message.embeds:
                    if message.embeds[0].title == "⚙️ Bot管理パネル":
                        panel_found = True
                        break
            if not panel_found:
                embed = discord.Embed(
                    title="⚙️ Bot管理パネル",
                    description="以下のボタンをクリックして操作してください。\n※このメッセージを削除してしまった場合は、Botを再起動すると再設置されます。",
                    color=discord.Color.dark_theme(),
                )
                await admin_channel.send(embed=embed, view=AdminPanelView())
                logger.info(f"[{guild.name}] 管理パネルを設置しました。")
        except Exception as e:
            logger.error(f"[{guild.name}] 管理パネル確認・送信失敗: {e}")

    @tasks.loop(minutes=10.0)
    async def cleanup_past_events(self):
        """過去イベントの定期クリーンアップ（10分おきの低負荷メンテナンス）"""
        try:
            today_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
            async with Database.connect() as db:
                async with await db.execute(
                    "SELECT message_id, guild_id, event_date FROM events WHERE event_date IS NOT NULL AND event_date < ?",
                    (today_str,)
                ) as event_cursor:
                    past_events = await event_cursor.fetchall()

            for msg_id, guild_id, event_date in past_events:
                if guild_id is None:
                    continue
                settings = await get_guild_settings(guild_id)
                if settings and settings.get("attendance_channel_id"):
                    channel = self.get_channel(settings["attendance_channel_id"])
                    if channel:
                        try:
                            msg = await channel.fetch_message(msg_id)
                            await msg.delete()
                        except Exception:
                            pass
                async with Database.connect() as db:
                    await db.execute("DELETE FROM events WHERE message_id = ?", (msg_id,))
                    await db.execute("DELETE FROM attendances WHERE message_id = ?", (msg_id,))
                    await db.commit()

        except Exception as e:
            if "disconnected" not in str(e).lower() and "closed" not in str(e).lower():
                logging.getLogger("discord").error(f"Error in cleanup_past_events: {e}")

    @cleanup_past_events.before_loop
    async def before_cleanup(self):
        await self.wait_until_ready()


# --- Bot インスタンスとスラッシュコマンド ---

bot = CircleManagerBot()


@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_commands(ctx: commands.Context):
    """管理者用: スラッシュコマンドを手動で同期する (!sync)"""
    try:
        msg = await ctx.send("スラッシュコマンドを同期中...")
        synced = await bot.tree.sync()
        await msg.edit(content=f"✅ スラッシュコマンドを {len(synced)} 件同期しました！")
    except Exception as e:
        await ctx.send(f"❌ 同期エラー: {e}")


@bot.tree.command(name="setup", description="【管理者専用】このBotで使用するチャンネルを設定します")
@app_commands.describe(
    admin_channel="管理パネルを設置するチャンネル",
    attendance_channel="出欠パネルを投稿するチャンネル",
    room_status_channel="部室の状態を表示するチャンネル",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(
    interaction: discord.Interaction,
    admin_channel: discord.TextChannel,
    attendance_channel: discord.TextChannel,
    room_status_channel: discord.TextChannel,
):
    """サーバーごとにチャンネルIDをDBに保存し、管理パネルを設置する"""
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id

    async with Database.connect() as db:
        await db.execute(
            """
            INSERT INTO guild_settings (guild_id, admin_channel_id, attendance_channel_id, room_status_channel_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                admin_channel_id = excluded.admin_channel_id,
                attendance_channel_id = excluded.attendance_channel_id,
                room_status_channel_id = excluded.room_status_channel_id
            """,
            (guild_id, admin_channel.id, attendance_channel.id, room_status_channel.id),
        )
        await db.commit()

    await bot._ensure_admin_panel(interaction.guild)

    embed = discord.Embed(
        title="✅ セットアップ完了！",
        description=(
            f"以下の設定でBotを構成しました。\n\n"
            f"🔧 **管理チャンネル**: {admin_channel.mention}\n"
            f"📅 **出欠チャンネル**: {attendance_channel.mention}\n"
            f"🏢 **部室状況チャンネル**: {room_status_channel.mention}\n\n"
            f"{admin_channel.mention} に管理パネルを設置しました！\n"
            f"部室状況パネルは、管理パネルの「部屋パネルを再設置」ボタンから設置できます。"
        ),
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@setup_command.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠️ このコマンドは「サーバーを管理する」権限を持つメンバーのみ実行できます。", ephemeral=True
        )
    else:
        await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)


# --- メインエントリーポイント ---

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    print("Botの起動処理を開始します...", flush=True)

    # Renderのヘルスチェック（ポートリッスン）を通すため、即座にWebサーバーを別スレッドで開始
    start_web_server_thread()

    if not TOKEN:
        print("エラー: .env ファイルに DISCORD_TOKEN または DISCORD_BOT_TOKEN が設定されていません。", flush=True)
    else:
        print("トークンを読み込みました。Discordサーバーへ接続中...", flush=True)
        bot.run(TOKEN)
