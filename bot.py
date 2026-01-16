import time
import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct


def points_for(difficulty: str) -> int:
    return 2 if difficulty == "Easy" else 4 if difficulty == "Medium" else 6


async def post_next_question(channel: discord.abc.Messageable):
    game = db.get_game(str(channel.id))
    if not game:
        await channel.send("No active trivia game in this channel.")
        return

    # Generate question
    try:
        payload, _ = generate_trivia(game["sport"], game["difficulty"], game["mode"])
    except Exception as e:
        await channel.send("❌ OpenAI error: " + str(e))
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

    # Send embed
    embed = discord.Embed(
        title="Florida Gators Trivia",
        description=payload["question"],
        color=0xFA4616
    )

    if payload["type"] == "mcq":
        labels = ["A", "B", "C", "D"]
        for i in range(4):
            embed.add_field(name=labels[i], value=payload["choices"][i], inline=False)
        await channel.send(embed=embed, view=MCQView(str(channel.id), qid))
    else:
        await channel.send(embed=embed, view=FreeView(str(channel.id), qid))


class MCQView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int):
        super().__init__(timeout=30)
        self.channel_id = channel_id
        self.qid = qid
        self.answered_users = set()

    async def handle(self, interaction: discord.Interaction, idx: int):
        if interaction.user.id in self.answered_users:
            await interaction.response.send_message("You already answered this one.", ephemeral=True)
            return

        self.answered_users.add(interaction.user.id)

        q = db.get_question(self.qid)
        payload = q["payload"]
        correct_idx = int(payload["answer_index"])
        correct = (idx == correct_idx)

        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])

        db.bump_score(str(interaction.guild_id), str(interaction.user.id), correct, pts)

        correct_answer = payload["choices"][correct_idx]
        expl = payload.get("explanation", "")

        if correct:
            msg = "✅ Correct!"
        else:
            msg = "❌ Wrong. Correct answer: **" + correct_answer + "**"

        await interaction.response.send_message(msg + ("\n\n" + expl if expl else ""))

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
        q = db.get_question(self.qid)
        payload = q["payload"]

        acceptable = payload.get("answers", [])
        correct = free_is_correct(self.answer.value, acceptable)

        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])

        db.bump_score(str(interaction.guild_id), str(interaction.user.id), correct, pts)

        correct_ans = acceptable[0] if acceptable else "N/A"

        if correct:
            msg = "✅ Correct!"
        else:
            msg = "❌ Wrong. Correct answer: **" + correct_ans + "**"

        await interaction.response.send_message(msg)


class FreeView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int):
        super().__init__(timeout=30)
        self.channel_id = channel_id
        self.qid = qid

    @discord.ui.button(label="Submit Answer", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(FreeAnswerModal(self.channel_id, self.qid))


class TriviaCog(app_commands.Group):
    def __init__(self):
        super().__init__(name="trivia", description="Florida Gators Trivia")

    @app_commands.command(name="start", description="Start trivia in this channel")
    async def start(self, interaction: discord.Interaction):
        # Prevent Discord timeout
        await interaction.response.defer(thinking=True)

        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            30
        )

        await interaction.followup.send("Trivia started!")
        await post_next_question(interaction.channel)


class GatorTriviaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TriviaCog())
        await self.tree.sync()


def main():
    import config
    db.init_db()
    bot = GatorTriviaBot()
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()

