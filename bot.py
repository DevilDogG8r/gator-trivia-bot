import time
import asyncio
import random
import json
import hashlib

import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct


# =============================
# Defaults
# =============================
ANSWER_SECONDS = 30

EVENT_DAY_MINUTES = 1440
EVENT_DAY_INTERVAL_SECONDS = 300

EVENT_WEEK_MINUTES = 10080
EVENT_WEEK_INTERVAL_SECONDS = 1800

MIN_SECONDS_BETWEEN_QUESTIONS = 15  # safety


# =============================
# Anti-spam safety
# =============================
LAST_ASKED_TS = {}  # channel_id -> last question ts


# =============================
# Scoring
# =============================
def points_for(difficulty: str) -> int:
    if difficulty == "Easy":
        return 2
    if difficulty == "Medium":
        return 4
    if difficulty == "Hard":
        return 6
    return 4


# =============================
# Scheduler
# =============================
async def trivia_scheduler(bot: discord.Client):
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            now = int(time.time())
            events = db.get_active_events()

            for ev in events:
                channel = bot.get_channel(int(ev["channel_id"]))
                if not channel:
                    continue

                # End event
                if now >= ev["end_ts"]:
                    lb = db.get_event_leaderboard(ev["id"], limit=10)

                    if not lb:
                        await channel.send("@everyone\n🏁 **Trivia event ended.** No answers recorded.")
                    else:
                        lines = [
                            "@everyone",
                            "🏁 **Trivia Event Ended!**",
                            "",
                            "**Top 10:**"
                        ]
                        for i, (uid, pts, c, w) in enumerate(lb, start=1):
                            lines.append(f"{i}. <@{uid}> — **{pts}** pts (✅{c} ❌{w})")

                        lines.append("")
                        lines.append(f"🏆 **Winner:** <@{lb[0][0]}>")
                        await channel.send("\n".join(lines))

                    db.stop_event(ev["channel_id"])
                    continue

                # Post next question if due
                if now >= ev["next_ts"]:
                    await post_next_question(channel)
                    db.set_next_question_ts(ev["channel_id"], now + int(ev["min_gap"]))

        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))

        await asyncio.sleep(10)


# =============================
# Ask Question
# =============================
async def post_next_question(channel: discord.abc.Messageable) -> bool:
    cid = str(channel.id)
    now = int(time.time())

    if now - LAST_ASKED_TS.get(cid, 0) < MIN_SECONDS_BETWEEN_QUESTIONS:
        return False

    game = db.get_game(cid)
    if not game:
        return False

    # Sport mixing
    sport = game["sport"]
    if sport == "All":
        sport = random.choice(["Football", "Basketball", "Baseball", "Gymnastics", "Softball"])

    # Identify active event (if any)
    ev = db.get_active_event_for_channel(cid)
    event_id = ev["id"] if ev else None

    # Try multiple times to avoid repeats
    for _ in range(6):
        try:
            payload, _ = generate_trivia(sport, game["difficulty"], game["mode"])
        except Exception as e:
            await channel.send(f"❌ OpenAI error: `{e}`")
            return False

        if not payload or payload.get("confidence", 0) < 0.6:
            continue

        qhash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

        # If event is running, never repeat within that event
        if event_id is not None and db.event_has_question(event_id, qhash):
            continue

        # Accept question
        if event_id is not None:
            db.add_event_question(event_id, qhash)

        # store question
        if payload["type"] == "mcq":
            answer_key = payload["choices"][int(payload["answer_index"])]
        else:
            answer_key = payload["answers"][0]

        qid = db.record_question(cid, payload, answer_key, str(now))
        LAST_ASKED_TS[cid] = now

        # Build embed + timer
        embed = discord.Embed(
            title=f"Florida Gators Trivia — {sport}",
            description=payload["question"],
            color=0xFA4616
        )
        embed.add_field(name="Time Remaining", value=f"<t:{now + ANSWER_SECONDS}:R>", inline=False)

        if payload["type"] == "mcq":
            for lbl, opt in zip(["A", "B", "C", "D"], payload["choices"]):
                embed.add_field(name=lbl, value=opt, inline=False)
            view = MCQView(cid, qid, ANSWER_SECONDS)
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
        else:
            view = FreeView(cid, qid, ANSWER_SECONDS)
            msg = await channel.send(embed=embed, view=view)
            view.message = msg

        return True

    # If we can't get a non-repeat after several tries, just skip this cycle
    return False


# =============================
# Views (disable buttons on timeout)
# =============================
class MCQView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int, timeout: int):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.qid = qid
        self.answered = set()
        self.message: discord.Message | None = None

    async def on_timeout(self):
        try:
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception as e:
            print("VIEW_TIMEOUT_EDIT_FAILED:", repr(e))

    async def handle(self, interaction: discord.Interaction, idx: int):
        if interaction.user.id in self.answered:
            await interaction.response.send_message("Already answered.", ephemeral=True)
            return

        self.answered.add(interaction.user.id)

        q = db.get_question(self.qid)
        payload = q["payload"]
        correct = idx == int(payload["answer_index"])

        pts = points_for(db.get_game(self.channel_id)["difficulty"])

        ev = db.get_active_event_for_channel(str(interaction.channel_id))
        if ev:
            db.bump_event_score(ev["id"], str(interaction.user.id), correct, pts)

        correct_text = payload["choices"][int(payload["answer_index"])]
        msg = "✅ Correct!" if correct else f"❌ Wrong. Correct: **{correct_text}**"
        await interaction.response.send_message(msg, ephemeral=False)

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def a(self, i, _): await self.handle(i, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def b(self, i, _): await self.handle(i, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def c(self, i, _): await self.handle(i, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def d(self, i, _): await self.handle(i, 3)


class FreeAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer = discord.ui.TextInput(label="Answer", max_length=120)

    def __init__(self, channel_id: str, qid: int):
        super().__init__()
        self.channel_id = channel_id
        self.qid = qid

    async def on_submit(self, interaction: discord.Interaction):
        q = db.get_question(self.qid)
        payload = q["payload"]
        correct = free_is_correct(self.answer.value, payload["answers"])

        pts = points_for(db.get_game(self.channel_id)["difficulty"])

        ev = db.get_active_event_for_channel(str(interaction.channel_id))
        if ev:
            db.bump_event_score(ev["id"], str(interaction.user.id), correct, pts)

        msg = "✅ Correct!" if correct else f"❌ Wrong. Correct: **{payload['answers'][0]}**"
        await interaction.response.send_message(msg)


class FreeView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int, timeout: int):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.qid = qid
        self.message: discord.Message | None = None

    async def on_timeout(self):
        try:
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception as e:
            print("VIEW_TIMEOUT_EDIT_FAILED:", repr(e))

    @discord.ui.button(label="Submit Answer", style=discord.ButtonStyle.primary)
    async def submit(self, interaction, _):
        await interaction.response.send_modal(FreeAnswerModal(self.channel_id, self.qid))


# =============================
# Commands (NO TIME INPUTS)
# =============================
class TriviaCog(app_commands.Group):
    def __init__(self):
        super().__init__(name="trivia", description="Florida Gators Trivia")

    @app_commands.command(name="start", description="Post a question now (30 seconds to answer)")
    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            ANSWER_SECONDS
        )

        await interaction.followup.send("Trivia ready! 🎯 (30 seconds per question)")
        await post_next_question(interaction.channel)

    @app_commands.command(name="event_day", description="24-hour event: question every 5 minutes, top 10 @everyone at end")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_day(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            ANSWER_SECONDS
        )

        db.start_event(
            str(interaction.channel_id),
            str(interaction.guild_id),
            EVENT_DAY_MINUTES,
            EVENT_DAY_INTERVAL_SECONDS,
            EVENT_DAY_INTERVAL_SECONDS
        )

        await interaction.followup.send(
            "✅ **Day Trivia Event started!**\n"
            "⏱️ **30 seconds** to answer\n"
            "🕔 **Every 5 minutes**\n"
            "🏆 **@everyone + Top 10** posted at the end"
        )

    @app_commands.command(name="event_week", description="7-day event: question every 30 minutes, top 10 @everyone at end")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_week(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            ANSWER_SECONDS
        )

        db.start_event(
            str(interaction.channel_id),
            str(interaction.guild_id),
            EVENT_WEEK_MINUTES,
            EVENT_WEEK_INTERVAL_SECONDS,
            EVENT_WEEK_INTERVAL_SECONDS
        )

        await interaction.followup.send(
            "✅ **Week Trivia Event started!**\n"
            "⏱️ **30 seconds** to answer\n"
            "🕧 **Every 30 minutes**\n"
            "🏆 **@everyone + Top 10** posted at the end"
        )

    @app_commands.command(name="event_stop", description="Stop the active event in this channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_stop(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        db.stop_event(str(interaction.channel_id))
        await interaction.followup.send("🛑 Event stopped.")

    @app_commands.command(name="event_status", description="Show event status for this channel")
    async def event_status(self, interaction: discord.Interaction):
        ev = db.get_active_event_for_channel(str(interaction.channel_id))
        if not ev:
            await interaction.response.send_message("No active event in this channel.")
            return

        await interaction.response.send_message(
            f"⏳ Event ends <t:{ev['end_ts']}:R>\n"
            f"❓ Next question <t:{ev['next_ts']}:R>"
        )

    @app_commands.command(name="help", description="Show trivia commands")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Gator Trivia Commands**\n"
            "`/trivia start` — post one question now (30 seconds)\n"
            "`/trivia event_day` — 24h event, question every 5 min\n"
            "`/trivia event_week` — 7d event, question every 30 min\n"
            "`/trivia event_status` — show time left + next post\n"
            "`/trivia event_stop` — stop the event\n"
            "\n"
            "**Note:** Bot needs permission to mention @everyone in that channel.",
            ephemeral=True
        )


# =============================
# Bot
# =============================
class GatorTriviaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TriviaCog())
        await self.tree.sync()
        asyncio.create_task(trivia_scheduler(self))


def main():
    import config
    db.init_db()
    bot = GatorTriviaBot()
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

