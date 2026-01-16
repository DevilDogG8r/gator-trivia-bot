import os
import time
import asyncio
import random
import hashlib

import discord
from discord import app_commands

import db

# Try to import your project modules, but don't let them break posting
try:
    from ai import generate_trivia  # can be async or sync
except Exception as e:
    generate_trivia = None
    print("WARN: ai.generate_trivia import failed:", repr(e))

try:
    from match import free_is_correct
except Exception as e:
    free_is_correct = None
    print("WARN: match.free_is_correct import failed:", repr(e))

# -------------------------
# Config (Railway Variables)
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing bot token. Set Railway Variable: DISCORD_TOKEN (or TOKEN / BOT_TOKEN).")

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")  # optional override
QUESTION_INTERVAL_SECONDS = 5 * 60
ANSWER_WINDOW_SECONDS = 30

# -------------------------
# Init DB
# -------------------------
db.init_db()

# -------------------------
# Fallback question bank (always works)
# Keep choices <= 5 (Discord button row limit)
# -------------------------
FALLBACK_QUESTIONS = [
    {
        "question": "What year did Florida win its first football national championship?",
        "choices": ["1992", "1996", "2006", "2008"],
        "answer": "1996",
    },
    {
        "question": "What is the nickname of Ben Hill Griffin Stadium?",
        "choices": ["The Swamp", "Death Valley", "The Horseshoe", "The Big House"],
        "answer": "The Swamp",
    },
    {
        "question": "What are Florida’s official colors?",
        "choices": ["Orange & Blue", "Red & Black", "Green & Gold", "Maroon & Gold"],
        "answer": "Orange & Blue",
    },
    {
        "question": "Which conference do the Florida Gators compete in?",
        "choices": ["SEC", "ACC", "Big Ten", "Big 12"],
        "answer": "SEC",
    },
]

# -------------------------
# Discord client
# -------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _now() -> int:
    return int(time.time())


def _qid(question_text: str, choices: list[str]) -> str:
    payload = question_text + "|" + "|".join(choices)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> str:
    return str(TRIVIA_CHANNEL_ID) if TRIVIA_CHANNEL_ID else str(interaction.channel_id)


def _normalize_trivia(raw) -> dict | None:
    """
    Normalize whatever generate_trivia returns into:
      {question:str, choices:[str], answer:str}
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        q = raw.get("question") or raw.get("q")
        choices = raw.get("choices") or raw.get("answers") or raw.get("options") or raw.get("a")
        ans = raw.get("answer") or raw.get("correct") or raw.get("c")

        if not q or not choices or ans is None:
            return None

        choices = [str(x) for x in choices]
        if len(choices) < 2:
            return None
        if len(choices) > 5:
            choices = choices[:5]

        if isinstance(ans, int) and 0 <= ans < len(choices):
            ans_text = choices[ans]
        else:
            ans_text = str(ans)

        return {"question": str(q), "choices": choices, "answer": ans_text}

    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        q = str(raw[0])
        choices = [str(x) for x in raw[1]]
        ans = raw[2]

        if len(choices) < 2:
            return None
        if len(choices) > 5:
            choices = choices[:5]

        if isinstance(ans, int) and 0 <= ans < len(choices):
            ans_text = choices[ans]
        else:
            ans_text = str(ans)

        return {"question": q, "choices": choices, "answer": ans_text}

    return None


def _is_correct(chosen: str, correct: str) -> bool:
    if free_is_correct:
        try:
            return bool(free_is_correct(chosen, correct))
        except Exception:
            pass
    return chosen.strip().lower() == correct.strip().lower()


async def _announce_start(channel: discord.abc.Messageable, event_type: str, end_ts: int):
    title = "✅ Day Trivia Event started!" if event_type == "day" else "✅ Week Trivia Event started!"
    msg = (
        f"@everyone\n"
        f"**{title}**\n"
        f"⏱️ 30 seconds to answer\n"
        f"🕔 Every 5 minutes\n"
        f"🏆 Top 10 posted at the end"
    )
    await channel.send(msg)


async def _announce_end(channel: discord.abc.Messageable, event_id: int):
    top10 = db.top_scores(event_id, 10)
    lines = ["@everyone", "**🏁 Trivia Event ended!**", "", "**🏆 Top 10**"]
    if not top10:
        lines.append("No scores yet.")
    else:
        for i, (user_id, points) in enumerate(top10, start=1):
            lines.append(f"**{i}.** <@{user_id}> — **{points}**")
    await channel.send("\n".join(lines))


class TriviaView(discord.ui.View):
    def __init__(self, event_id: int, question_id: str, choices: list[str], correct_answer: str):
        super().__init__(timeout=ANSWER_WINDOW_SECONDS)
        self.event_id = event_id
        self.question_id = question_id
        self.choices = choices
        self.correct_answer = correct_answer
        self.answered_users: set[int] = set()
        self.message: discord.Message | None = None

        for choice in choices:
            self.add_item(TriviaButton(choice))

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

        try:
            if self.message:
                await self.message.channel.send(f"✅ Correct answer: **{self.correct_answer}**")
        except Exception:
            pass


class TriviaButton(discord.ui.Button):
    def __init__(self, label: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label[:80])

    async def callback(self, interaction: discord.Interaction):
        view: TriviaView = self.view  # type: ignore

        if interaction.user.id in view.answered_users:
            await interaction.response.send_message("You already answered this one.", ephemeral=True)
            return

        view.answered_users.add(interaction.user.id)

        chosen = self.label
        correct = _is_correct(chosen, view.correct_answer)

        if correct:
            db.add_point(view.event_id, str(interaction.user.id))
            await interaction.response.send_message("✅ Correct!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Wrong!", ephemeral=True)


async def _get_trivia_nonrepeat(event_id: int) -> dict:
    """
    Returns a trivia dict guaranteed to not repeat within the event if possible.
    Uses AI if available, otherwise fallback questions.
    """
    # Try AI up to 3 times
    if generate_trivia:
        for attempt in range(3):
            try:
                raw = generate_trivia()
                if asyncio.iscoroutine(raw):
                    raw = await raw
                trivia = _normalize_trivia(raw)
                if not trivia:
                    continue
                qid = _qid(trivia["question"], trivia["choices"])
                if db.event_has_question(event_id, qid):
                    continue
                trivia["qid"] = qid
                return trivia
            except Exception as e:
                print("AI_FAILED:", repr(e))

    # Fallback (try to avoid repeats)
    for _ in range(10):
        trivia = random.choice(FALLBACK_QUESTIONS).copy()
        qid = _qid(trivia["question"], trivia["choices"])
        if not db.event_has_question(event_id, qid):
            trivia["qid"] = qid
            return trivia

    # Worst case: return something anyway (even if repeated)
    trivia = random.choice(FALLBACK_QUESTIONS).copy()
    trivia["qid"] = _qid(trivia["question"], trivia["choices"])
    return trivia


async def _post_question_for_guild(guild_id: str):
    event = db.get_active_event(guild_id)
    if not event:
        return

    event_id = int(event["id"])
    channel_id = int(event["channel_id"])
    now = _now()
    next_ask = int(event["next_ask_ts"])
    end_ts = int(event["end_ts"])

    print(f"TICK guild={guild_id} event={event_id} now={now} next={next_ask} end={end_ts}")

    # End event
    if now >= end_ts:
        channel = await client.fetch_channel(channel_id)
        await _announce_end(channel, event_id)
        db.end_event(event_id)
        print(f"EVENT_ENDED event={event_id}")
        return

    # Not time yet
    if now < next_ask:
        return

    # Get non-repeating trivia
    trivia = await _get_trivia_nonrepeat(event_id)

    # Record asked + schedule next
    db.record_question(event_id, trivia["qid"], now)
    db.update_next_ask(event_id, now + QUESTION_INTERVAL_SECONDS)

    # Post question
    channel = await client.fetch_channel(channel_id)

    embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
    embed.set_footer(text=f"You have {ANSWER_WINDOW_SECONDS} seconds to answer.")

    view = TriviaView(event_id=event_id, question_id=trivia["qid"], choices=trivia["choices"], correct_answer=trivia["answer"])
    msg = await channel.send(embed=embed, view=view)
    view.message = msg

    print(f"POSTED event={event_id} qid={trivia['qid']}")


async def scheduler_loop():
    await client.wait_until_ready()
    print("SCHEDULER_STARTED")
    while not client.is_closed():
        try:
            for g in client.guilds:
                await _post_question_for_guild(str(g.id))
        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))
        await asyncio.sleep(15)


# -------------------------
# Slash commands
# -------------------------
@tree.command(name="event_day", description="Start a 24-hour trivia event (question every 5 minutes)")
async def event_day(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    if db.get_active_event(str(interaction.guild_id)):
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    channel_id = _pick_channel_id(interaction)
    now = _now()
    end_ts = now + 24 * 60 * 60

    db.create_event(str(interaction.guild_id), str(channel_id), "day", now, end_ts)

    channel = await client.fetch_channel(int(channel_id))
    await _announce_start(channel, "day", end_ts)

    await interaction.followup.send("✅ Day event started.", ephemeral=True)


@tree.command(name="event_week", description="Start a 7-day trivia event (question every 5 minutes)")
async def event_week(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    if db.get_active_event(str(interaction.guild_id)):
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    channel_id = _pick_channel_id(interaction)
    now = _now()
    end_ts = now + 7 * 24 * 60 * 60

    db.create_event(str(interaction.guild_id), str(channel_id), "week", now, end_ts)

    channel = await client.fetch_channel(int(channel_id))
    await _announce_start(channel, "week", end_ts)

    await interaction.followup.send("✅ Week event started.", ephemeral=True)


@tree.command(name="stop", description="Stop the current trivia event")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    event = db.get_active_event(str(interaction.guild_id))
    if not event:
        await interaction.followup.send("No active event.", ephemeral=True)
        return

    channel = await client.fetch_channel(int(event["channel_id"]))
    await _announce_end(channel, int(event["id"]))
    db.end_event(int(event["id"]))

    await interaction.followup.send("✅ Event stopped and Top 10 posted.", ephemeral=True)


@tree.command(name="status", description="Show event status")
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
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
