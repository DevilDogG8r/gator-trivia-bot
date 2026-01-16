import time
import asyncio
import random

import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct


# ---------- Scoring ----------
def points_for(difficulty: str) -> int:
    if difficulty == "Easy":
        return 2
    if difficulty == "Medium":
        return 4
    if difficulty == "Hard":
        return 6
    return 4


# ---------- Scheduler ----------
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
                        lines.append(f"🏆 Winner: <@{lb[0][0]}>")
                        await channel.send("\n".join(lines))

                    db.stop_event(ev["channel_id"])
                    continue

                # Post next question if due
                if now >= ev["next_ts"]:
                    await post_next_question(channel)

                    nxt = now + random.randint(ev["min_gap"], ev["max_gap"])
                    db.set_next_question_ts(ev["channel_id"], nxt)

        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))

        await asyncio.sleep(10)


# ---------- Posting ----------
async def post_next_question(channel: discord.abc.Messageable):
    game = db.get_game(str(channel.id))
    if not game:
        await channel.send("No game settings found. Run `/trivia start` first.")
        return

    # Mix sports if "All"
    sport = game["sport"]
    if sport == "All":
        sport = random.choice(["Football", "Basketball", "Baseball", "Gymnastics"])

    # Generate question
    try:
        payload, _ = generate_trivia(sport, game["difficulty"], game["mode"])
    except Exception as e:
        await channel.send(f"❌ OpenAI error: `{type(e).__name__}: {e}`")
        return

    if not payload or payload.get("confidence", 0) < 0.6:
        await channel.send("❌ Failed to generate a good question. Try again.")
        return

    # Store question
    if payload["type"] == "mcq":
        answer_key = payload["choices"][int(payload["answer_index"])]
    else:
        answer_key = payload["answers"][0]

    qid = db.record_question(str(channel.id), payload, answer_key, str(time.time()))

    # Build embed + timer
    embed = discord.Embed(
        title=f"Florida Gators Trivia — {sport} ({game['difficulty']})",
        description=payload["question"],
        color=0xFA4616,
    )

    q_timeout = int(game.get("q_timeout", 180))
    end_ts = int(time.time()) + q_timeout
    embed.add_field(name="Time", value=f"<t:{end_ts}:R>", inline=False)

    # Send question
    if payload["type"] == "mcq":
        labels = ["A", "B", "C", "D"]
        for i in range(4):
            embed.add_field(name=labels[i], value=payload["choices"][i], inline=False)

        await channel.send(embed=embed, view=MCQView(str(channel.id), qid, q_timeout))
    else:
        await channel.send(embed=embed, view=FreeView(str(channel.id), qid, q_timeout))


# ---------- Views ----------
class MCQView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int, timeout_seconds: int):
        super().__init__(timeout=timeout_seconds)
        self.channel_id = channel_id
        self.qid = qid
        self.answered_users = set()

    async def handle(self, interaction: discord.Interaction, idx: int):
        try:
            if interaction.user.id in self.answered_users:
                await interaction.response.send_message("You already answered this one.", ephemeral=True)
                return

            self.answered_users.add(interaction.user.id)
            is_first_answer = (len(self.answered_users) == 1)

            q = db.get_question(self.qid)
            payload = q["payload"]
            correct_idx = int(payload["answer_index"])
            correct = (idx == correct_idx)

            game = db.get_game(self.channel_id)
            pts = points_for(game["difficulty"])

            # Event-scoped scoring (only if event active)
            ev = db.get_active_event_for_channel(str(interaction.channel_id))
            if ev:
                db.bump_event_score(ev["id"], str(interaction.user.id), correct, pts)

            correct_answer = payload["choices"][correct_idx]
            expl = payload.get("explanation", "")

            if correct:
                msg = "✅ Correct!"
            else:
                msg = "❌ Wrong. Correct answer: **" + correct_answer + "**"

            await interaction.response.send_message(msg + ("\n\n" + expl if expl else ""))

            # Auto-next (only once per question)
            if is_first_answer:
                await post_next_question(interaction.channel)

        except Exception as e:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Interaction error: `{type(e).__name__}: {e}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Interaction error: `{type(e).__name__}: {e}`", ephemeral=True)
            except Exception:
                pass

            print("INTERACTION_FAILED:", repr(e))
            raise

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def a(self, interaction: discord.Interaction, _):
        await self.handle(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def b(self, interaction: discord.Interaction, _):
        await self.handle(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def c(self, interaction: discord.Interaction, _):
        await self.handle(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def d(self, interaction: discord.Interaction, _):
        await self.handle(interaction, 3)


class FreeAnswerModal(discord.ui.Modal, title="Answer the question"):
    answer = discord.ui.TextInput(label="Your answer", max_length=120)

    def __init__(self, channel_id: str, qid: int):
        super().__init__()
        self.channel_id = channel_id
        self.qid = qid

    async def on_submit(self, interaction: discord.Interaction):
        try:
            q = db.get_question(self.qid)
            payload = q["payload"]

            acceptable = payload.get("answers", [])
            correct = free_is_correct(self.answer.value, acceptable)

            game = db.get_game(self.channel_id)
            pts = points_for(game["difficulty"])

            ev = db.get_active_event_for_channel(str(interaction.channel_id))
            if ev:
                db.bump_event_score(ev["id"], str(interaction.user.id), correct, pts)

            correct_ans = acceptable[0] if acceptable else "N/A"
            msg = "✅ Correct!" if correct else "❌ Wrong. Correct answer: **" + correct_ans + "**"
            await interaction.response.send_message(msg)

            # Auto-next after any free response
            await post_next_question(interaction.channel)

        except Exception as e:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Interaction error: `{type(e).__name__}: {e}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Interaction error: `{type(e).__name__}: {e}`", ephemeral=True)
            except Exception:
                pass
            print("FREE_INTERACTION_FAILED:", repr(e))
            raise


class FreeView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int, timeout_seconds: int):
        super().__init__(timeout=timeout_seconds)
        self.channel_id = channel_id
        self.qid = qid

    @discord.ui.button(label="Submit Answer", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(FreeAnswerModal(self.channel_id, self.qid))


# ---------- Slash Commands ----------
class TriviaCog(app_commands.Group):
    def __init__(self):
        super().__init__(name="trivia", description="Florida Gators Trivia")

    @app_commands.command(name="start", description="Initialize trivia settings in this channel and post a question")
    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        # Default settings (you can change later)
        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",        # sport
            "Medium",     # difficulty
            "MCQ",        # mode: MCQ / Free / Mixed if your ai supports it
            180           # seconds to answer
        )

        await interaction.followup.send("Trivia started!")
        await post_next_question(interaction.channel)

    @app_commands.command(name="event_start", description="Start auto-posting trivia in this channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_start(
        self,
        interaction: discord.Interaction,
        duration_minutes: int = 60,
        min_gap_seconds: int = 300,
        max_gap_seconds: int = 600
    ):
        await interaction.response.defer(thinking=True)

        if max_gap_seconds < min_gap_seconds:
            await interaction.followup.send("max_gap_seconds must be >= min_gap_seconds")
            return

        # Ensure game exists in this channel
        if not db.get_game(str(interaction.channel_id)):
            db.upsert_game(
                str(interaction.channel_id),
                str(interaction.guild_id),
                "All",
                "Medium",
                "MCQ",
                180
            )

        db.start_event(
            str(interaction.channel_id),
            str(interaction.guild_id),
            int(duration_minutes),
            int(min_gap_seconds),
            int(max_gap_seconds),
        )

        await interaction.followup.send(
            f"✅ Trivia event started for **{duration_minutes}** minutes.\n"
            f"Posting every **{min_gap_seconds}-{max_gap_seconds}** seconds in this channel."
        )

    @app_commands.command(name="event_stop", description="Stop the trivia event in this channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_stop(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        db.stop_event(str(interaction.channel_id))
        await interaction.followup.send("🛑 Trivia event stopped.")

    @app_commands.command(name="event_status", description="Show trivia event status in this channel")
    async def event_status(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        ev = db.get_active_event_for_channel(str(interaction.channel_id))
        if not ev:
            await interaction.followup.send("No active trivia event in this channel.")
            return

        await interaction.followup.send(
            f"⏳ Event running.\n"
            f"Ends: <t:{ev['end_ts']}:R>\n"
            f"Next question: <t:{ev['next_ts']}:R>"
        )


# ---------- Bot Setup ----------
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

