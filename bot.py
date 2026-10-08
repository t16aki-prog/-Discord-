import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
from config import SYNC_COMMANDS
from database import Database, init_db, get_guild_settings
from circuit_breaker import circuit_breaker
from ui import AdminPanelView, AttendanceView, RoomStatusView
import time

class CircleManagerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guild_scheduled_events = True
        intents.message_content = True  # !sync, !shutdown コマンド用
        super().__init__(command_prefix="!", intents=intents)
        self._ready_once = False

    async def on_interaction(self, interaction: discord.Interaction):
        """ユーザー操作（ボタン・コマンド等）を受信した際、API送信前に過剰リクエストを瞬時検知・遮断する"""
        if not await circuit_breaker.can_proceed():
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "🛡️ **システム保護モード (Circuit Breaker)**\n"
                        "短時間の過剰アクセスを検知したため、Cloudflare / Discord によるIPブロックを防ぐ目的で一時的に操作を制限しています。\n"
                        "約1分後に再度お試しください。",
                        ephemeral=True,
                    )
                except Exception:
                    pass
            return
        await super().on_interaction(interaction)

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
        if SYNC_COMMANDS:
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


# --- Bot インスタンス ---
bot = CircleManagerBot()


# --- 管理者用プレフィックスコマンド ---

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


@bot.command(name="shutdown", aliases=["stop", "kill"])
@commands.has_permissions(administrator=True)
async def shutdown_command(ctx: commands.Context):
    """管理者用: Botを安全に緊急停止する (!shutdown)"""
    await ctx.send("🚨 **緊急停止コマンドを受信しました。**\nDiscordとの接続を安全に切断してシャットダウンします...")
    logging.getLogger("discord").warning(f"緊急停止コマンドが実行されました (実行者: {ctx.author})")
    await bot.close()


@bot.command(name="maintenance")
@commands.has_permissions(administrator=True)
async def maintenance_command(ctx: commands.Context, state: str = ""):
    """管理者用: メンテナンスモードの切替 (!maintenance on / !maintenance off)"""
    if state.lower() in ("on", "enable", "1"):
        circuit_breaker.maintenance_mode = True
        await ctx.send("🛠️ **メンテナンスモードを【有効】にしました。**\n一般ユーザーのコマンドやボタン操作を一時停止します。")
    elif state.lower() in ("off", "disable", "0"):
        circuit_breaker.maintenance_mode = False
        circuit_breaker.reset()
        await ctx.send("✅ **メンテナンスモードを【無効】にしました。**\n通常のBot操作を再開します。")
    else:
        current_status = "🛠️ 有効（操作停止中）" if circuit_breaker.maintenance_mode else "🟢 無効（通常稼働）"
        await ctx.send(
            f"現在のメンテナンスモード: **{current_status}**\n"
            f"切り替え方法: `!maintenance on` または `!maintenance off`"
        )


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status_command(ctx: commands.Context):
    """管理者用: Botとサーキットブレーカーの稼働状態を表示 (!status)"""
    is_tripped = circuit_breaker.is_tripped
    m_mode = circuit_breaker.maintenance_mode
    remaining = max(0, int(circuit_breaker.tripped_until - time.time())) if is_tripped else 0

    embed = discord.Embed(
        title="🛡️ Botシステム防衛ステータス",
        color=discord.Color.red() if (is_tripped or m_mode) else discord.Color.green(),
    )
    embed.add_field(
        name="サーキットブレーカー",
        value=f"⚠️ **過剰リクエスト遮断中**\n(自動復旧まで残り {remaining} 秒)" if is_tripped else "🟢 **正常** (リクエスト監視中)",
        inline=False,
    )
    embed.add_field(name="メンテナンスモード", value="🛠️ **有効 (停止中)**" if m_mode else "🟢 **無効 (稼働中)**", inline=True)
    embed.add_field(name="直近リクエスト頻度", value=f"**{len(circuit_breaker._timestamps)}** 件 / 5秒", inline=True)
    await ctx.send(embed=embed)


# --- スラッシュコマンド ---

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


# --- グローバルエラーハンドラー ---

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """プレフィックスコマンドのエラーハンドリング"""
    if isinstance(error, commands.CommandNotFound):
        return  # 未登録のコマンドはサイレント無視
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ このコマンドを実行する権限（管理者権限）がありません。")
    else:
        logging.getLogger("discord").error(f"コマンドエラー ({ctx.command}): {error}")
        await ctx.send(f"⚠️ コマンド実行中にエラーが発生しました: {error}")


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """スラッシュコマンドのエラーハンドリング"""
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⚠️ このコマンドを実行する権限がありません。"
    else:
        msg = f"⚠️ エラーが発生しました: {error}"
        logging.getLogger("discord").error(f"スラッシュコマンドエラー: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

