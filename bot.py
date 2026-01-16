import os
import time
import asyncio
import random
import hashlib

import discord
from discord import app_commands

import db

try:
    from ai import generate_trivia
except Exception as e:
    generate_trivia = None
    print("WARN: ai.generate_trivia import failed:", repr(e))

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing bot token. Set Railway Variable: DISCORD_TOKEN (or TOKEN / BOT_TOKEN).")

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")  # optional override
QUESTION_INTERVAL_SECONDS = 5 * 60
ANSWER_WINDOW_SECONDS = 30

db.init_db()

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
        "question": "Which conference do the Florida Gators compete in?",
        "choices": ["SEC", "ACC", "Big Ten", "Big 12"],
        "answer_index": 0,
    },
    {
        "question": "Who is Florida’s biggest in-state rival (commonly)?",
        "choices": ["FSU", "Miami", "UCF", "FAU"],
        "answer_index": 0,
    },
]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Per-guild lock to prevent double-post race (start command + scheduler tick)
GUILD_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for_guild(guild_id: str) -> asyncio.Lock:
    if guild_id not in GUILD_LOCKS:
        GUILD_LOCKS[guild_id] = asyncio.Lock()
    return GUILD_LOCKS[guild_id]


def _now() -> int:
    return int(time.time())


def _clean(s: str) -> str:
    s = str(s).replace("\n", " ").replace("\r", " ")
    s = s.replace(" ,", ",").replace(", ", " ").replace(",", " ")
    s = " ".join(s.split())
    return s.strip()


def _qid(question_text: str, choices: list[str]) -> str:
    payload = _clean(question_text) + "|" + "|".join(_clean(c) for c in choices)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> str:
    return str(TRIVIA_CHANNEL_ID) if TRIVIA_CHANNEL_ID else str(interaction.channel_id)


async def _safe_fetch_channel(channel_id: int):
    try:
        return await client.fetch_channel(channel_id)
    except discord.Forbidden:
        return None
    except discord.NotFound:
        return None
    except Exception as e:
        print("CHANNEL_FETCH_ERROR:", repr(e))
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
    def __init__(self, event_id: int, choices: list[str], answer_index: int):
        super().__init__(timeout=ANSWER_WINDOW_SECONDS)
        self.event_id = event_id
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
        super().__init__(style=discord.ButtonStyle.primary, label=_clean(label)[:80])
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


def _normalize_trivia(raw) -> dict | None:
    """
    Output:
      {question:str, choices:[str], answer_index:int}
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        q = raw.get("question") or raw.get("q")
        choices = raw.get("choices") or raw.get("answers") or raw.get("options") or raw.get("a")
        ans = raw.get("answer_index") or raw.get("answer") or raw.get("correct") or raw.get("c")

        if not q or not choices or ans is None:
            return None

        q = _clean(q)
        choices = [_clean(x) for x in choices]
        if len(choices) < 2:
            return None
        if len(choices) > 5:
            choices = choices[:5]

        if isinstance(ans, int) and 0 <= ans < len(choices):
            answer_index = ans
        else:
            ans_text = _clean(ans)
            try:
                answer_index = choices.index(ans_text)
            except ValueError:
                return None

        return {"question": q, "choices": choices, "answer_index": answer_index}

    return None


async def _pick_trivia(event_id: int) -> dict:
    # Try AI first
    if generate_trivia:
        for _ in range(5):
            try:
                raw = generate_trivia()
                if asyncio.iscoroutine(raw):
                    raw = await raw
                trivia = _normalize_trivia(raw)
                if trivia:
                    trivia["qid"] = _qid(trivia["question"], trivia["choices"])
                    return trivia
            except Exception as e:
                print("AI_FAILED:", repr(e))

    # Fallback
    trivia = random.choice(FALLBACK_QUESTIONS).copy()
    trivia["question"] = _clean(trivia["question"])
    trivia["choices"] = [_clean(c) for c in trivia["choices"]]
    trivia["qid"] = _qid(trivia["question"], trivia["choices"])
    return trivia


async def _post_question_for_guild(guild_id: str):
    # Prevent double-post race per guild
    async with _lock_for_guild(guild_id):
        event = db.get_active_event(guild_id)
        if not event:
            return

        event_id = int(event["id"])
        channel_id = int(event["channel_id"])
        now = _now()
        next_ask = int(event["next_ask_ts"])
        end_ts = int(event["end_ts"])

        if now >= end_ts:
            channel = await _safe_fetch_channel(channel_id)
            if channel:
                await _announce_end(channel, event_id)
            db.end_event(event_id)
            return

        if now < next_ask:
            return

        channel = await _safe_fetch_channel(channel_id)
        if not channel:
            print(f"POST_BLOCKED guild={guild_id} event={event_id} channel={channel_id}")
            db.update_next_ask(event_id, now + 60)
            return

        # HARD guarantee: record in DB first, only post if DB accepts it as new.
        # We’ll try multiple pulls to find a non-duplicate.
        posted = False
        for _ in range(12):
            trivia = await _pick_trivia(event_id)
            qid = trivia["qid"]

            # If record_question returns False, it's a duplicate -> DO NOT POST.
            if not db.record_question(event_id, qid, now):
                continue

            # Now safe to post (unique within event)
            db.update_next_ask(event_id, now + QUESTION_INTERVAL_SECONDS)

            embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
            embed.set_footer(text=f"You have {ANSWER_WINDOW_SECONDS} seconds to answer.")

            view = TriviaView(event_id, trivia["choices"], int(trivia["answer_index"]))
            msg = await channel.send(embed=embed, view=view)
            view.message = msg

            posted = True
            break

        if not posted:
            # If we somehow can't find a new question, try again later.
            db.update_next_ask(event_id, now + 60)
            await channel.send("⚠️ Ran out of new questions for this event (temporarily). Trying again soon.")


async def scheduler_loop():
    await client.wait_until_ready()
    print("SCHEDULER_STARTED")
    while not client.is_closed():
        for g in client.guilds:
            try:
                await _post_question_for_guild(str(g.id))
            except Exception as e:
                print("SCHEDULER_GUILD_ERROR:", str(g.id), repr(e))
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

    try:
        await _announce_start(interaction.channel, "day")
    except Exception as e:
        print("START_ANNOUNCE_FAILED:", repr(e))

    # Post first question immediately (safe; lock prevents race)
    try:
        await _post_question_for_guild(str(interaction.guild_id))
    except Exception as e:
        print("START_POST_FIRST_FAILED:", repr(e))

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

    try:
        await _announce_start(interaction.channel, "week")
    except Exception as e:
        print("START_ANNOUNCE_FAILED:", repr(e))

    # Post first question immediately (safe; lock prevents race)
    try:
        await _post_question_for_guild(str(interaction.guild_id))
    except Exception as e:
        print("START_POST_FIRST_FAILED:", repr(e))

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

    event_id = int(event["id"])
    channel = await _safe_fetch_channel(int(event["channel_id"]))
    if not channel:
        channel = interaction.channel

    try:
        await _announce_end(channel, event_id)
    except Exception as e:
        print("STOP_ANNOUNCE_END_FAILED:", repr(e))

    db.end_event(event_id)
    await interaction.followup.send("✅ Event stopped.", ephemeral=True)


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

