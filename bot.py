import os
import time
import asyncio
import random
import hashlib

import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct

# -------------------------
# Config (Railway Variables)
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing bot token. Set Railway Variable: DISCORD_TOKEN (or TOKEN / BOT_TOKEN).")

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")  # optional: force a specific channel
QUESTION_INTERVAL_SECONDS = 5 * 60
ANSWER_WINDOW_SECONDS = 30

# -------------------------
# Init DB
# -------------------------
db.init_db()

# -------------------------
# Discord client
# -------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _now() -> int:
    return int(time.time())


def _qid_from_question(q_text: str, choices: list[str]) -> str:
    payload = q_text + "|" + "|".join(choices)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> str:
    if TRIVIA_CHANNEL_ID:
        return str(TRIVIA_CHANNEL_ID)
    return str(interaction.channel_id)


async def _post_start_announcement(channel: discord.abc.Messageable, event_type: str, end_ts: int):
    hours_left = max(0, int((end_ts - _now()) / 3600))
    title = "✅ Day Trivia Event started!" if event_type == "day" else "✅ Week Trivia Event started!"
    msg = (
        f"@everyone\n"
        f"**{title}**\n"
        f"⏱️ 30 seconds to answer\n"
        f"🕔 Every 5 minutes\n"
        f"🧭 Ends in ~{hours_left} hour(s)\n"
        f"🏆 Top 10 posted at the end"
    )
    await channel.send(msg)


async def _post_end_announcement(channel: discord.abc.Messageable, event_id: int):
    top10 = db.top_scores(event_id, 10)
    lines = ["@everyone", "**🏁 Trivia Event ended!**", "", "**🏆 Top 10**"]

    if not top10:
        lines.append("No scores yet.")
    else:
        for i, (user_id, points) in enumerate(top10, start=1):
            lines.append(f"**{i}.** <@{user_id}> — **{points}**")

    await channel.send("\n".join(lines))


def _normalize_trivia(raw) -> dict | None:
    """
    Tries to coerce whatever generate_trivia() returns into:
    {
      "question": str,
      "choices": [str, ...]  (2-5 choices)
      "answer": str (the correct answer text)
      "explanation": optional str
    }
    """
    if raw is None:
        return None

    # If raw is already a dict
    if isinstance(raw, dict):
        q = raw.get("question") or raw.get("q")
        choices = raw.get("choices") or raw.get("answers") or raw.get("options") or raw.get("a")
        ans = raw.get("answer") or raw.get("correct") or raw.get("c")
        expl = raw.get("explanation")

        if not q or not choices or ans is None:
            return None

        # if answer is an index
        if isinstance(ans, int) and isinstance(choices, list) and 0 <= ans < len(choices):
            ans_text = str(choices[ans])
        else:
            ans_text = str(ans)

        choices = [str(x) for x in choices]

        # Discord buttons: max 5 options
        if len(choices) > 5:
            choices = choices[:5]

        return {"question": str(q), "choices": choices, "answer": ans_text, "explanation": expl}

    # If raw is tuple-like (question, choices, answer)
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        q = str(raw[0])
        choices = [str(x) for x in raw[1]]
        ans = raw[2]
        if isinstance(ans, int) and 0 <= ans < len(choices):
            ans_text = choices[ans]
        else:
            ans_text = str(ans)

        if len(choices) > 5:
            choices = choices[:5]

        return {"question": q, "choices": choices, "answer": ans_text, "explanation": None}

    return None


class TriviaView(discord.ui.View):
    def __init__(self, event_id: int, question_id: str, choices: list[str], correct_answer: str):
        super().__init__(timeout=ANSWER_WINDOW_SECONDS)
        self.event_id = event_id
        self.question_id = question_id
        self.choices = choices
        self.correct_answer = correct_answer
        self.answered_users: set[int] = set()
        self.message: discord.Message | None = None

        for idx, choice in enumerate(choices):
            self.add_item(TriviaButton(label=choice, idx=idx))

    async def on_timeout(self):
        # Disable buttons
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

        # Post correct answer
        try:
            if self.message:
                channel = self.message.channel
                await channel.send(f"✅ Correct answer: **{self.correct_answer}**")
        except Exception:
            pass


class TriviaButton(discord.ui.Button):
    def __init__(self, label: str, idx: int):
        super().__init__(style=discord.ButtonStyle.primary, label=label)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: TriviaView = self.view  # type: ignore

        # Already answered
        if interaction.user.id in view.answered_users:
            await interaction.response.send_message("You already answered this one.", ephemeral=True)
            return

        view.answered_users.add(interaction.user.id)

        chosen = self.label
        # Use your existing matcher (free_is_correct) so it still works even if answer wording varies
        is_correct = False
        try:
            is_correct = bool(free_is_correct(chosen, view.correct_answer))
        except Exception:
            is_correct = (chosen.strip().lower() == view.correct_answer.strip().lower())

        if is_correct:
            db.add_point(view.event_id, str(interaction.user.id))
            await interaction.response.send_message("✅ Correct!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Wrong!", ephemeral=True)


async def _post_one_question_for_event(guild_id: str):
    event = db.get_active_event(guild_id)
    if not event:
        return

    now = _now()

    # End event if time is up
    if now >= int(event["end_ts"]):
        channel = await client.fetch_channel(int(event["channel_id"]))
        await _post_end_announcement(channel, int(event["id"]))
        db.end_event(int(event["id"]))
        return

    # Not time yet
    if now < int(event["next_ask_ts"]):
        return

    # Try a few times to avoid repeats
    trivia = None
    for _ in range(5):
        try:
            raw = await generate_trivia()
        except TypeError:
            # generate_trivia might not be async in your project
            raw = generate_trivia()

        trivia = _normalize_trivia(raw)
        if not trivia:
            continue

        qid = _qid_from_question(trivia["question"], trivia["choices"])
        if db.event_has_question(int(event["id"]), qid):
            trivia = None
            continue

        # got a non-repeated question
        trivia["qid"] = qid
        break

    if not trivia:
        # If AI fails, try again next tick
        db.update_next_ask(int(event["id"]), now + 15)
        return

    channel = await client.fetch_channel(int(event["channel_id"]))

    # Record asked
    db.record_question(int(event["id"]), trivia["qid"], now)
    db.update_next_ask(int(event["id"]), now + QUESTION_INTERVAL_SECONDS)

    # Post question with buttons
    embed = discord.Embed(
        title="🐊 Florida Gators Trivia",
        description=trivia["question"]
    )
    embed.set_footer(text=f"You have {ANSWER_WINDOW_SECONDS} seconds to answer.")

    view = TriviaView(
        event_id=int(event["id"]),
        question_id=trivia["qid"],
        choices=trivia["choices"],
        correct_answer=trivia["answer"]
    )

    msg = await channel.send(embed=embed, view=view)
    view.message = msg


async def scheduler_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            # Loop through guilds the bot is in
            for g in client.guilds:
                await _post_one_question_for_event(str(g.id))
        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))

        await asyncio.sleep(15)  # heartbeat tick


# -------------------------
# Slash commands
# -------------------------
@tree.command(name="event_day", description="Start a 24-hour trivia event (question every 5 minutes)")
async def event_day(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    active = db.get_active_event(str(interaction.guild_id))
    if active:
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    channel_id = _pick_channel_id(interaction)
    now = _now()
    end_ts = now + 24 * 60 * 60

    event_id = db.create_event(str(interaction.guild_id), str(channel_id), "day", now, end_ts)

    channel = await client.fetch_channel(int(channel_id))
    await _post_start_announcement(channel, "day", end_ts)

    await interaction.followup.send("✅ Day event started.", ephemeral=True)


@tree.command(name="event_week", description="Start a 7-day trivia event (question every 5 minutes)")
async def event_week(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    active = db.get_active_event(str(interaction.guild_id))
    if active:
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    channel_id = _pick_channel_id(interaction)
    now = _now()
    end_ts = now + 7 * 24 * 60 * 60

    event_id = db.create_event(str(interaction.guild_id), str(channel_id), "week", now, end_ts)

    channel = await client.fetch_channel(int(channel_id))
    await _post_start_announcement(channel, "week", end_ts)

    await interaction.followup.send("✅ Week event started.", ephemeral=True)


@tree.command(name="stop", description="Stop the current trivia event")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    event = db.get_active_event(str(interaction.guild_id))
    if not event:
        await interaction.followup.send("No active event.", ephemeral=True)
        return

    channel = await client.fetch_channel(int(event["channel_id"]))
    await _post_end_announcement(channel, int(event["id"]))
    db.end_event(int(event["id"]))

    await interaction.followup.send("✅ Event stopped and Top 10 posted.", ephemeral=True)


@tree.command(name="status", description="Show event status")
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    event = db.get_active_event(str(interaction.guild_id))
    if not event:
        await interaction.followup.send("No active event.", ephemeral=True)
        return

    now = _now()
    ends_in = max(0, int(event["end_ts"]) - now)
    next_in = max(0, int(event["next_ask_ts"]) - now)

    await interaction.followup.send(
        f"Active: **{event['event_type']}**\n"
        f"Ends in: **{ends_in // 3600}h {(ends_in % 3600) // 60}m**\n"
        f"Next question in: **{next_in // 60}m {next_in % 60}s**",
        ephemeral=True
    )


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    client.loop.create_task(scheduler_loop())


client.run(TOKEN)

