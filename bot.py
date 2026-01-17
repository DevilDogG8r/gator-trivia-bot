import os
import time
import asyncio
import random
import hashlib
import re

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

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")
QUESTION_INTERVAL_SECONDS = 5 * 60
ANSWER_WINDOW_SECONDS = 30
RECENT_WINDOW = 1000

SPORTS = [
    "football",
    "men's basketball",
    "women's basketball",
    "baseball",
    "softball",
    "gymnastics",
    "track & field",
    "swimming & diving",
    "lacrosse",
    "soccer",
]
DIFFICULTIES = ["easy", "medium", "hard", "expert"]
DIFFICULTY_WEIGHTS = [0.35, 0.35, 0.22, 0.08]

db.init_db()

# Keep fallback, but duplicates are now controlled by canonical question hash.
FALLBACK_BANK = [
    {"sport":"football","difficulty":"easy","question":"What is the nickname of Ben Hill Griffin Stadium?","choices":["The Swamp","Death Valley","The Horseshoe","The Big House"],"answer_index":0},
    {"sport":"football","difficulty":"easy","question":"What conference do the Florida Gators play in?","choices":["SEC","ACC","Big Ten","Big 12"],"answer_index":0},
    {"sport":"football","difficulty":"medium","question":"Who coached Florida to the 1996 football national championship?","choices":["Steve Spurrier","Urban Meyer","Ron Zook","Jim McElwain"],"answer_index":0},
    {"sport":"football","difficulty":"medium","question":"Florida won football national titles in which seasons?","choices":["1996 and 2008","2006 and 2007","1984 and 1992","2016 and 2020"],"answer_index":0},
    {"sport":"men's basketball","difficulty":"easy","question":"Florida won back-to-back NCAA men's basketball titles in which years?","choices":["2006 and 2007","2004 and 2005","2007 and 2008","2005 and 2006"],"answer_index":0},
    {"sport":"men's basketball","difficulty":"medium","question":"Who coached Florida’s men’s basketball teams to the 2006 and 2007 titles?","choices":["Billy Donovan","Mike White","Lon Kruger","Todd Golden"],"answer_index":0},
    {"sport":"baseball","difficulty":"easy","question":"What is the name of Florida’s baseball stadium?","choices":["Condron Ballpark","Ben Hill Griffin Stadium","O'Connell Center","Pressly Stadium"],"answer_index":0},
    {"sport":"baseball","difficulty":"medium","question":"Florida won the College World Series national title in which year?","choices":["2017","2015","2005","2021"],"answer_index":0},
    {"sport":"softball","difficulty":"easy","question":"What is the name of Florida’s softball stadium?","choices":["Katie Seashole Pressly Stadium","Condron Ballpark","O'Connell Center","Exactech Arena"],"answer_index":0},
]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

GUILD_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for_guild(guild_id: str) -> asyncio.Lock:
    if guild_id not in GUILD_LOCKS:
        GUILD_LOCKS[guild_id] = asyncio.Lock()
    return GUILD_LOCKS[guild_id]


def _now() -> int:
    return int(time.time())


def _clean(s: str) -> str:
    s = str(s).replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    return s.strip()


def _canonical_question(q: str) -> str:
    """
    Canonicalize question text so tiny edits / punctuation / choice order can't bypass duplicate detection.
    """
    q = _clean(q).lower()
    q = q.replace("university of florida", "florida")
    q = re.sub(r"[^a-z0-9\s]", "", q)   # drop punctuation
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _qid_from_question_only(question_text: str) -> str:
    canon = _canonical_question(question_text)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> str:
    return str(TRIVIA_CHANNEL_ID) if TRIVIA_CHANNEL_ID else str(interaction.channel_id)


async def _safe_fetch_channel(channel_id: int):
    try:
        return await client.fetch_channel(channel_id)
    except Exception:
        return None


def _validate_trivia(trivia: dict) -> dict | None:
    try:
        q = _clean(trivia["question"])
        choices = [_clean(c) for c in trivia["choices"]]
        ans = int(trivia["answer_index"])
        if len(choices) != 4:
            return None
        if ans < 0 or ans > 3:
            return None
        if any(c.strip() == "" or c.strip() == "?" for c in choices):
            return None
        if choices[ans].strip() in {"", "?"}:
            return None
        if len(set(c.lower() for c in choices)) != 4:
            return None
        sport = _clean(trivia.get("sport", "")) or "football"
        difficulty = _clean(trivia.get("difficulty", "")) or "medium"
        return {"question": q, "choices": choices, "answer_index": ans, "sport": sport, "difficulty": difficulty}
    except Exception:
        return None


async def _get_candidate() -> dict | None:
    sport = random.choice(SPORTS)
    difficulty = random.choices(DIFFICULTIES, weights=DIFFICULTY_WEIGHTS, k=1)[0]

    if generate_trivia:
        try:
            out = None
            try:
                out = generate_trivia(sport=sport, difficulty=difficulty)
            except TypeError:
                out = generate_trivia()
            if asyncio.iscoroutine(out):
                out = await out
            if isinstance(out, dict):
                v = _validate_trivia(out)
                if v:
                    return v
        except Exception as e:
            print("AI_TRIVIA_ERROR:", repr(e))

    fb = random.choice(FALLBACK_BANK).copy()
    return _validate_trivia(fb)


async def _announce_start(channel: discord.abc.Messageable, event_type: str):
    title = "✅ Day Trivia Event started!" if event_type == "day" else "✅ Week Trivia Event started!"
    await channel.send(
        f"**{title}**\n"
        "⏱️ 30 seconds to answer\n"
        "🕔 Every 5 minutes\n"
        "🏆 Top 10 posted at the end"
    )


async def _announce_end(channel: discord.abc.Messageable, event_id: int):
    top10 = db.top_scores(event_id, 10)
    lines = ["**🏁 Trivia Event ended!**", "", "**🏆 Top 10**"]
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
                await self.message.channel.send(f"✅ Correct answer: **{self.choices[self.answer_index]}**")
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


async def _post_question_for_guild(guild_id: str):
    async with _lock_for_guild(guild_id):
        event = db.get_active_event(guild_id)
        if not event:
            return

        event_id = int(event["id"])
        channel_id = int(event["channel_id"])
        now = _now()

        if now >= int(event["end_ts"]):
            ch = await _safe_fetch_channel(channel_id)
            if ch:
                await _announce_end(ch, event_id)
            db.end_event(event_id)
            return

        if now < int(event["next_ask_ts"]):
            return

        ch = await _safe_fetch_channel(channel_id)
        if not ch:
            db.update_next_ask(event_id, now + 60)
            return

        recent_count = db.guild_recent_count(guild_id)
        effective_window = min(RECENT_WINDOW, recent_count)

        for _ in range(50):
            trivia = await _get_candidate()
            if not trivia:
                continue

            # IMPORTANT: duplicates are now based on the QUESTION ONLY
            qid = _qid_from_question_only(trivia["question"])

            if effective_window > 0 and db.guild_recent_has(guild_id, qid, effective_window):
                continue

            # also prevent duplicates within the same event
            if not db.record_question(event_id, qid, now):
                continue

            db.guild_recent_add(guild_id, qid, now, RECENT_WINDOW)
            db.update_next_ask(event_id, now + QUESTION_INTERVAL_SECONDS)

            embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
            embed.set_footer(
                text=f"Sport: {trivia.get('sport','')} • Difficulty: {trivia.get('difficulty','')} • "
                     f"You have {ANSWER_WINDOW_SECONDS} seconds to answer."
            )

            view = TriviaView(event_id, trivia["choices"], trivia["answer_index"])
            msg = await ch.send(embed=embed, view=view)
            view.message = msg
            return

        db.update_next_ask(event_id, now + 60)
        await ch.send("⚠️ Couldn’t generate a new non-duplicate question right now. Trying again in 60 seconds.")


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

    now = _now()
    end_ts = now + 24 * 60 * 60
    channel_id = _pick_channel_id(interaction)

    db.create_event(str(interaction.guild_id), str(channel_id), "day", now, end_ts)

    try:
        await _announce_start(interaction.channel, "day")
    except Exception as e:
        print("ANNOUNCE_ERROR:", repr(e))

    await interaction.followup.send("✅ Day event started.", ephemeral=True)

    try:
        await _post_question_for_guild(str(interaction.guild_id))
    except Exception as e:
        print("POST_FIRST_ERROR:", repr(e))


@tree.command(name="event_week", description="Start a 7-day trivia event (question every 5 minutes)")
async def event_week(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    if db.get_active_event(str(interaction.guild_id)):
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    now = _now()
    end_ts = now + 7 * 24 * 60 * 60
    channel_id = _pick_channel_id(interaction)

    db.create_event(str(interaction.guild_id), str(channel_id), "week", now, end_ts)

    try:
        await _announce_start(interaction.channel, "week")
    except Exception as e:
        print("ANNOUNCE_ERROR:", repr(e))

    await interaction.followup.send("✅ Week event started.", ephemeral=True)

    try:
        await _post_question_for_guild(str(interaction.guild_id))
    except Exception as e:
        print("POST_FIRST_ERROR:", repr(e))


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
    ch = await _safe_fetch_channel(int(event["channel_id"]))
    if not ch:
        ch = interaction.channel

    try:
        await _announce_end(ch, event_id)
    except Exception as e:
        print("STOP_ANNOUNCE_END_FAILED:", repr(e))

    db.end_event(event_id)
    await interaction.followup.send("✅ Event stopped.", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    client.loop.create_task(scheduler_loop())


client.run(TOKEN)

