import time
import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct


def points_for(difficulty):
    if difficulty == "Easy":
        return 2
    if difficulty == "Medium":
        return 4
    return 6


async def post_next_question(channel):
    game = db.get_game(str(channel.id))
    if not game:
        await channel.send("No active trivia game.")
        return

    try:
        payload, _ = generate_trivia(
            game["sport"],
            game["difficulty"],
            game["mode"]
        )
    except Exception as e:
        await channel.send("OpenAI error: " + str(e))
        return

    if not payload or payload.get("confidence", 0) < 0.6:
        await channel.send("Failed to generate a good trivia question.")
        return

    if payload["type"] == "mcq":
        answer_key = payload["choices"][payload["answer_index"]]
    else:
        answer_key = payload["answers"][0]

    qid = db.record_question(
        str(channel.id),
        payload,
        answer_key,
        str(time.time())
    )

    embed = discord.Embed(
        title="Florida Gators Trivia",
        description=payload["question"],
        color=0xFA4616
    )

    if payload["type"] == "mcq":
        labels = ["A", "B", "C", "D"]
        for i in range(4):
            embed.add_field(
                name=labels[i],
                value=payload["choices"][i],
                inline=False
            )
        await channel.send(embed=embed, view=MCQView(str(channel.id), qid))
    else:
        await channel.send(embed=embed, view=FreeView(str(channel.id), qid))


class MCQView(discord.ui.View):
    def __init__(self, channel_id, qid):
        super().__init__(timeout=30)
        self.channel_id = channel_id
        self.qid = qid
        self.answered = set()

    async def handle(self, interaction, idx):
        if interaction.user.id in self.answered:
            await interaction.response.send_message(
                "You already answered.",
                ephemeral=True
            )
            return

        self.answered.add(interaction.user.id)

        q = db.get_question(self.qid)
        payload = q["payload"]
        correct = idx == payload["answer_index"]

        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])

        db.bump_score(
            str(interaction.guild_id),
            str(interaction.user.id),
            correct,
            pts
        )

        correct_answer = payload["choices"][payload["answer_index"]]
        msg = "Correct!" if correct else "Wrong. Correct answer: " + correct_answer
        await interaction.response.send_message(msg)

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def a(self, interaction, _):
        await self.handle(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def b(self, interaction, _):
        await self.handle(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def c(self, interaction, _):
        await self.handle(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def d(self, interaction, _):
        await self.handle(interaction, 3)


class FreeAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer = discord.ui.TextInput(label="Answer", max_length=120)

    def __init__(self, channel_id, qid):
        super().__init__()
        self.channel_id = channel_id
        self.qid = qid

    async def on_submit(self, interaction):
        q = db.get_question(self.qid)
        payload = q["payload"]

        correct = free_is_correct(self.answer.value, payload["answers"])
        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])

        db.bump_score(
            str(interaction.guild_id),
            str(interaction.user.id),
            correct,

