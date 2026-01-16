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
# Anti-spam / anti-repeat
# =============================
LAST_ASKED_TS = {}      # channel_id -> last question unix ts
RECENT_QHASH = {}       # channel_id -> [hashes]
MIN_SECONDS_BETWEEN_QUESTIONS = 60


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
# Scheduler (EVENT MODE)
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
                        await channel.send("🏁 Trivia event ended. No answers recorded.")
                    else:
                        lines = ["🏁 **Trivia Event Ended!**", ""]
                        for i, (uid, pts, c, w) in enumerate(lb, start=1):
                            lines.append(f"{i}. <@{uid}> — **{pts}** pts (✅{c} ❌{w})")
                        lines.append("")
                        lines.append(f"🏆 **Winner:** <@{lb[0][0]}>")
                        await channel.send("\n".join(lines))

                    db.stop_event(ev["channel_id"])
                    continue

                # Ask next question if due
                if now >= ev["next_ts"]:
                    await post_next_question(channel)
                    nxt = now + random.randint(ev["min_gap"], ev["max_gap"])
                    db.set_next_question_ts(ev["channel_id"], nxt)

        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))

        await asyncio.sleep(10)


# =============================
# Ask Question
# =============================
async def post_next_question(channel: discord.abc.Messageable):
    cid = str(channel.id)
    now = int(time.time())

    # Cooldown
    if now - LAST_ASKED_TS.get(cid, 0) < MIN_SECONDS_BETWEEN_QUESTIONS:
        return

    game = db.get_game(cid)
    if not game:
        return

    # Sport mixing
    sport = game["sport"]
    if sport == "All":
        sport = random.choice([
            "Football",
            "Basketball",
            "Baseball",
            "Gymnastics",
            "Softball"
        ])

    # Generate question
    try:
        payload, _ = generate_trivia(sport, game["difficulty"], game["mode"])
    except Exception as e:
        await channel.send(f"❌ OpenAI error: `{e}`")
        return

    if not payload or payload.get("confidence", 0) < 0.6:
        return

    # Deduplicate questions
    qh = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    recent = RECENT_QHASH.get(cid, [])
    if qh in recent:
        return

    RECENT_QHASH[cid] = (recent + [qh])[-10:]
    LAST_ASKED_TS[cid] = now

    # Store question
    if payload["type"] == "mcq":
        answer_key = payload["choices"][int(payload["answer_index"])]
    else:
        answer_key = payload["answers"][0]

    qid = db.record_question(cid, payload, answer_key, str(now))

    # Build embed
    embed = discord.Embed(
        title=f"Florida Gators Trivia — {sport}",
        description=payload["question"],
        color=0xFA4616
    )

    timeout = int(game.get("q_timeout", 180))
    embed.add_field(
        name="Time Remaining",
        value=f"<t:{now + timeout}:R>",
        inline=False
    )

    # Send
    if payload["type"] == "mcq":
        for lbl, opt in zip(["A", "B", "C", "D"], payload["choices"]):
            embed.add_field(name=lbl, value=opt, inline=False)
        await channel.send(embed=embed, view=MCQView(cid, qid, timeout))
    else:
        await channel.send(embed=embed, view=FreeView(cid, qid, timeout))


# =============================
# Views
# =============================
class MCQView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int, timeout: int):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self.qid = qid
        self.answered = set()

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

        msg = "✅ Correct!" if correct else f"❌ Wrong. Correct: **{payload['choices'][payload['answer_index']]}**"
        await interaction.response.send_message(msg)

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

    @discord.ui.button(label="Submit Answer", style=discord.ButtonStyle.primary)
    async def submit(self, interaction, _):
        await interaction.response.send_modal(FreeAnswerModal(self.channel_id, self.qid))


# =============================
# Commands
# =============================
class TriviaCog(app_commands.Group):
    def __init__(self):
        super().__init__(name="trivia", description="Florida Gators Trivia")

    @app_commands.command(name="start")
    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            180
        )

        await interaction.followup.send("Trivia ready! 🎯")
        await post_next_question(interaction.channel)

    @app_commands.command(name="event_start")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_start(self, interaction: discord.Interaction, duration_minutes: int = 60, min_gap_seconds: int = 300, max_gap_seconds: int = 600):
        await interaction.response.defer(thinking=True)

        db.start_event(
            str(interaction.channel_id),
            str(interaction.guild_id),
            duration_minutes,
            min_gap_seconds,
            max_gap_seconds
        )

        await interaction.followup.send("✅ Trivia event started.")

    @app_commands.command(name="event_stop")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_stop(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        db.stop_event(str(interaction.channel_id))
        await interaction.followup.send("🛑 Trivia event stopped.")

    @app_commands.command(name="event_status")
    async def event_status(self, interaction: discord.Interaction):
        ev = db.get_active_event_for_channel(str(interaction.channel_id))
        if not ev:
            await interaction.response.send_message("No active event.")
        else:
            await interaction.response.send_message(
                f"Event ends <t:{ev['end_ts']}:R>, next question <t:{ev['next_ts']}:R>."
            )

    @app_commands.command(name="help")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Gator Trivia Commands**\n"
            "`/trivia start` – start trivia in this channel\n"
            "`/trivia event_start` – auto-post trivia event\n"
            "`/trivia event_status` – event status\n"
            "`/trivia event_stop` – stop event\n",
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

