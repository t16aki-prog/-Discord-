import discord
import logging
from datetime import datetime
from database import Database, get_guild_settings

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
