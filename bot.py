import time
import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct

SPORTS = ["Football", "Basketball", "Baseball", "Gymnastics", "All"]

def points_for(difficulty: str) -> int:
    return 2 if difficulty == "Easy" else 4 if difficulty == "Medium" else 6

def is_mod(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user:
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        return False
    perms = member.guild_permissions
    return perms.manage_guild or perms.manage_messages or perms.administrator

async def post_next_question(channel: discord.abc.Messageable):
    game = db.get_game(str(channel.id))
    if not game:
        await channel.send("No active game in this channel. Start with `/trivia start`.")
        return

    payload = None
    h = None

    for _ in range(4):
        p, hh = generate_trivia(game["sport"], game["difficulty"], game["mode"])
        if p.get("confidence", 0) < 0.60:
            continue
        if db.question_hash_exists(hh):
            continue
        if p["type"] == "mcq":
            if len(p.get("choices", [])) != 4:
                continue
            ai = int(p.get("answer_index", -1))
            if ai < 0 or ai > 3:
                continue
        payload, h = p, hh
        break

    if not payload:
        await channel.send("Could not generate a valid question. Try again.")
        return

    if payload["type"] == "mcq":
        answer_key = payload["choices"][payload["answer_index"]]
    else:
        answer_key = payload["answers"][0]

    qid = db.record_question(str(channel.id), payload, answer_key, h)
    db.set_active_question(str(channel.id), qid, int(time.time()) + game["seconds"])

    embed = discord.Embed(
        title=f"Florida Gators Trivia — {payload['sport']} — {payload['difficulty']}",
        description=payload["question"]
    )

    embed.set_footer(text=f"Mode: {payload['type'].upper()}")

    if payload["type"] == "mcq":
        for label, choice in zip(["A", "B", "C", "D"], payload["choices"]):
            embed.add_field(name=label, value=choice, inline=False)
        await channel.send(embed=embed, view=MCQView(str(channel.id), qid))
    else:
        await channel.send(embed=embed, view=FreeView(str(channel.id), qid))

class MCQView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int):
        super().__init__(timeout=30)
        self.channel_id = channel_id
        self.qid = qid
        self.answered = set()

    async def handle(self, interaction: discord.Interaction, index: int):
        if interaction.user.id in self.answered:
            await interaction.response.send_message("Already answered.", ephemeral=True)
            return
        self.answered.add(interaction.user.id)

        q = db.get_question(self.qid)
        payload = q["payload"]
        correct = index == payload["answer_index"]

        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])
        db.bump_score(str(interaction.guild_id), str(interaction.user.id), correct, pts)

        answer = payload["choices"][payload["answer_index"]]
        expl = payload.get("explanation", "")

        msg = "✅ Correct!" if correct else f"❌ Correct answer: **{answer}**"
        await interaction.response.send_message(f"{msg}\n\n{expl}")

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 3)

class FreeAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer = discord.ui.TextInput(label="Answer", max_length=100)

    def __init__(self, channel_id: str, qid: int):
        super().__init__()
        self.channel_id = channel_id
        self.qid = qid

    async def on_submit(self, interaction: discord.Interaction):
        q = db.get_question(self.qid)
        payload = q["payload"]

        correct = free_is_correct(self.answer.value, payload["answers"])
        game = db.get_game(self.channel_id)
        pts = points_for(game["difficulty"])

        db.bump_score(str(interaction.guild_id), str(interaction.user.id), correct, pts)

        ans = payload["answers"][0]
        msg = "✅ Correct!" if correct else f"❌ Correct answer: **{ans}**"
        await interaction.response.send_message(msg)

class FreeView(discord.ui.View):
    def __init__(self, channel_id: str, qid: int):
        super().__init__(timeout=30)
        self.channel_id = channel_id
        self.qid = qid

    @discord.ui.button(label="Submit Answer", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FreeAnswerModal(self.channel_id, self.qid))

class TriviaCog(app_commands.Group):
    def __init__(self):
        super().__init__(name="trivia", description="Florida Gators Trivia")

    @app_commands.command(name="start")
    async def start(self, interaction: discord.Interaction):
        db.upsert_game(
            str(interaction.channel_id),
            str(interaction.guild_id),
            "All",
            "Medium",
            "MCQ",
            30
        )
        await interaction.response.send_message("Trivia started!")
        await post_next_question(interaction.channel)

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TriviaCog())
        await self.tree.sync()

def main():
    import config
    db.init_db()
    bot = Bot()
    bot.run(config.DISCORD_TOKEN)

if __name__ == "__main__":
    main()
