import discord
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from database import Database, get_guild_settings
from ui.attendance import AttendanceView
from ui.room_status import RoomStatusView, AddRoomModal, DeleteRoomModal, update_room_panel

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
