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
# Fallback question bank
# -------------------------
FALLBACK_QUESTIONS = [
    {
        "question": "What year did Florida win its first football national championship?",
        "choices": ["1992", "1996", "2006", "2008"],
        "answer_index": 1,
    },
    {
        "question": "What is the nickname of Ben Hill Griffin Stadium?",
        "choices": ["The Swamp", "Death Valley", "The Horseshoe", "The Big House"],
        "answer_index": 0,
    },
    {
        "question": "What are Florida’s official colors?",
        "choices": ["Orange & Blue", "Red & Black", "Green & Gold", "Maroon & Gold"],
        "answer_index": 0,
    },
    {
        "question": "Which conference do the Florida Gators compete in?",
        "choices": ["SEC", "ACC", "Big Ten", "Big 12"],
        "answer_index": 0,
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


def _clean_label(s: str) -> str:
    # Remove stray commas and normalize whitespace
    s = str(s).replace("\n", " ").replace("\r", " ")
    s = s.replace(" ,", ",").replace(", ", " ").replace(",", " ")
    s = " ".join(s.split())
    return s.strip()


def _qid(question_text: str, choices: list[str]) -> str:
    payload = _clean_label(question_text) + "|" + "|".join(_clean_label(c) for c in choices)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> str:
    return str(TRIVIA_CHANNEL_ID) if TRIVIA_CHANNEL_ID else str(interaction.channel_id)


def _normalize_trivia(raw) -> dict | None:
    """
    Normalize AI output into:
      {question:str, choices:[str], answer_index:int}
    """
    if raw is None:
        return None

    # dict style
    if isinstance(raw, dict):
        q = raw.get("question") or raw.get("q")
        choices = raw.get("choices") or raw.get("answers") or raw.get("options") or raw.get("a")
        ans = raw.get("answer") or raw.get("correct") or raw.get("c") or raw.get("answer_index")

        if not q or not choices or ans is None:
            return None

        choices = [_clean_label(x) for x in choices]
        if len(choices) < 2:
            return None
        if len(choices) > 5:
            choices = choices[:5]

        # answer can be index or text
        if isinstance(ans, int) and 0 <= ans < len(choices):
            answer_index = ans
        else:
            ans_text = _clean_label(ans)
            try:
                answer_index = choices.index(ans_text)
            except ValueError:
                return None

        return {"question": _clean_label(q), "choices": choices, "answer_index": answer_index}

    # tuple style (q, choices, answer)
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        q = _clean_label(raw[0])
        choices = [_clean_label(x) for x in raw[1]]
        ans = raw[2]

        if len(choices) < 2:
            return None
        if len(choices) > 5:
            choices = choices[:5]

        if isinstance(ans, int) and 0 <= ans < len(choices):
            answer_index = ans
        else:
            ans_text = _clean_label(ans)
            try:
                answer_index = choices.index(ans_text)
            except ValueError:
                return None

        return {"question": q, "choices": choices, "answer_index": answer_index}

    return None


async def _announce_start(channel: discord.abc.Messageable, event_type: str):
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
    def __init__(self, event_id: int, qid: str, question: str, choices: list[str], answer_index: int):
        super().__init__(timeout=ANSWER_WINDOW_SECONDS)
        self.event_id = event_id
        self.qid = qid
        self.question = question
        self.choices = choices
        self.answer_index = answer_index
        self.answered_users: set[int] = set()
        self.message: discord.Message | None = None

        for idx, choice in enumerate(choices):
            self.add_item(TriviaButton(idx, choice))

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
                correct_text = self.choices[self.answer_index]
                await self.message.channel.send(f"✅ Correct answer: **{correct_text}**")
        except Exception:
            pass


class TriviaButton(discord.ui.Button):
    def __init__(self, idx: int, label: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label[:80])
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: TriviaView = self.view  # type: ignore

        if interaction.user.id in view.answered_users:
            await interaction.response.send_message("You already answered this one.", ephemeral=True)
            return

        view.answered_users.add(interaction.user.id)

        if self.idx == view.answer_index:
            db.add_point(view.event_id, str(interaction.user.id))
            await interaction.response.send_message("✅ Correct!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Wrong!", ephemeral=True)


async def _get_trivia_nonrepeat(event_id: int) -> dict:
    # Try AI up to 3 times
    if generate_trivia:
        for _ in range(3):
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

    # Fallback questions with no repeats if possible
    for _ in range(10):
        trivia = random.choice(FALLBACK_QUESTIONS).copy()
        trivia["question"] = _clean_label(trivia["question"])
        trivia["choices"] = [_clean_label(c) for c in trivia["choices"]]
        qid = _qid(trivia["question"], trivia["choices"])
        if not db.event_has_question(event_id, qid):
            trivia["qid"] = qid
            return trivia

    trivia = random.choice(FALLBACK_QUESTIONS).copy()
    trivia["question"] = _clean_label(trivia["question"])
    trivia["choices"] = [_clean_label(c) for c in trivia["choices"]]
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

    # End event
    if now >= end_ts:
        channel = await client.fetch_channel(channel_id)
        await _announce_end(channel, event_id)
        db.end_event(event_id)
        return

    # Not time yet
    if now < next_ask:
        return

    trivia = await _get_trivia_nonrepeat(event_id)

    db.record_question(event_id, trivia["qid"], now)
    db.update_next_ask(event_id, now + QUESTION_INTERVAL_SECONDS)

    channel = await client.fetch_channel(channel_id)

    embed = discord.Embed(
        title="🐊 Florida Gators Trivia",
        description=trivia["question"]
    )
    embed.set_footer(text=f"You have {ANSWER_WINDOW_SECONDS} seconds to answer.")

    view = TriviaView(
        event_id=event_id,
        qid=trivia["qid"],
        question=trivia["question"],
        choices=trivia["choices"],
        answer_index=int(trivia["answer_index"])
    )

    msg = await channel.send(embed=embed, view=view)
    view.message = msg


async def scheduler_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for g in client.guilds:
                await _post_question_for_guild(str(g.id))
        except Exception as e:
            print("SCHEDULER_ERROR:", repr(e))
        await asyncio.sleep(15)


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
    await _announce_start(channel, "day")

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
    await _announce_start(channel, "week")

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
