import discord
from datetime import datetime
from zoneinfo import ZoneInfo
from database import Database

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
