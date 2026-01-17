import os
import time
import asyncio
import random
import hashlib
import re

import discord
from discord import app_commands

import db
from ai import generate_trivia

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing bot token. Set DISCORD_TOKEN.")

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")
GUILD_ID = os.getenv("GUILD_ID")  # <-- set this to your server ID for instant slash command sync

# LOCKED SETTINGS
DAY_INTERVAL = 5 * 60
WEEK_INTERVAL = 30 * 60
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
    "volleyball",
    "tennis",
    "golf",
    "cross country",
    "rowing",
    "recruiting",
    "olympics",
]
DIFFICULTIES = ["easy", "medium", "hard", "expert"]

# Live events: challenging, but not impossible
LIVE_DIFFICULTY_WEIGHTS = [0.10, 0.30, 0.40, 0.20]
# Seeding: hard/expert heavy
SEED_DIFFICULTY_WEIGHTS = [0.05, 0.15, 0.45, 0.35]

db.init_db()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

GUILD_LOCKS: dict[str, asyncio.Lock] = {}
SEED_TASKS: dict[str, asyncio.Task] = {}


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
    q = _clean(q).lower()
    q = q.replace("university of florida", "florida")
    q = re.sub(r"[^a-z0-9\s]", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _qid(question_text: str) -> str:
    canon = _canonical_question(question_text)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _pick_channel_id(interaction: discord.Interaction) -> int:
    return int(TRIVIA_CHANNEL_ID) if TRIVIA_CHANNEL_ID else int(interaction.channel_id)


def _interval_for_event_type(event_type: str) -> int:
    return DAY_INTERVAL if event_type == "day" else WEEK_INTERVAL


def _interval_label(event_type: str) -> str:
    return "Every 5 minutes" if event_type == "day" else "Every 30 minutes"


async def _safe_fetch_channel(channel_id: int):
    try:
        return await client.fetch_channel(channel_id)
    except Exception:
        return None


def _validate_mcq(trivia: dict) -> dict | None:
    try:
        if trivia.get("type") != "mcq":
            return None
        q = _clean(trivia["question"])
        choices = [_clean(c) for c in trivia["choices"]]
        ans = int(trivia["answer_index"])
        if len(choices) != 4:
            return None
        if ans < 0 or ans > 3:
            return None
        if len(set(c.lower() for c in choices)) != 4:
            return None
        sport = _clean(trivia.get("sport", "")) or "football"
        difficulty = _clean(trivia.get("difficulty", "")) or "hard"
        explanation = _clean(trivia.get("explanation", ""))
        tags = trivia.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [_clean(t) for t in tags if _clean(t)]
        return {
            "sport": sport,
            "difficulty": difficulty,
            "question": q,
            "choices": choices,
            "answer_index": ans,
            "explanation": explanation,
            "tags": tags,
        }
    except Exception:
        return None


async def _gen_one(weights):
    sport = random.choice(SPORTS)
    difficulty = random.choices(DIFFICULTIES, weights=weights, k=1)[0]
    topic = random.choice([
        "recruiting (commits, flips, signing classes, staff, evaluations)",
        "Olympics (UF/Florida athletes, medals, events, years)",
        "records and milestones",
        "awards and honors",
        "postseason and championships",
        "iconic games and moments",
        "coaches and coaching eras",
        "all-time great players",
    ])

    # generate_trivia is sync (urllib), so run it in a thread
    trivia, _h = await asyncio.to_thread(
        generate_trivia, sport=sport, difficulty=difficulty, mode="MCQ", topic=topic
    )
    return _validate_mcq(trivia)


async def _announce_start(channel: discord.abc.Messageable, event_type: str):
    title = "✅ Day Trivia Event started!" if event_type == "day" else "✅ Week Trivia Event started!"
    await channel.send(
        f"**{title}**\n"
        f"⏱️ {ANSWER_WINDOW_SECONDS} seconds to answer\n"
        f"🕔 {_interval_label(event_type)}\n"
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
        event_type = str(event.get("event_type") or "day")
        interval = _interval_for_event_type(event_type)

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

        trivia = db.pick_question_from_bank(guild_id, event_id, effective_window)

        if not trivia:
            inserted = 0
            for _ in range(20):
                cand = await _gen_one(LIVE_DIFFICULTY_WEIGHTS)
                if not cand:
                    continue
                qid = _qid(cand["question"])
                ok = db.upsert_question_bank(
                    question_id=qid,
                    sport=cand["sport"],
                    difficulty=cand["difficulty"],
                    question=cand["question"],
                    choices=cand["choices"],
                    answer_index=cand["answer_index"],
                    explanation=cand["explanation"],
                    tags=cand["tags"],
                    created_ts=now,
                )
                if ok:
                    inserted += 1
                if inserted >= 8:
                    break
            trivia = db.pick_question_from_bank(guild_id, event_id, effective_window)

        if not trivia:
            db.update_next_ask(event_id, now + 60)
            await ch.send("⚠️ Couldn’t pull a fresh question right now. Trying again in 60 seconds.")
            return

        qid = trivia["question_id"]

        if not db.record_question(event_id, qid, now):
            db.update_next_ask(event_id, now + 60)
            return

        db.guild_recent_add(guild_id, qid, now, RECENT_WINDOW)
        db.update_next_ask(event_id, now + interval)

        embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
        embed.set_footer(text=f"Sport: {trivia['sport']} • Difficulty: {trivia['difficulty']} • {ANSWER_WINDOW_SECONDS}s to answer")

        view = TriviaView(event_id, trivia["choices"], trivia["answer_index"])
        msg = await ch.send(embed=embed, view=view)
        view.message = msg


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


async def _seed_worker(guild_id: str, channel_id: int, target_total: int, concurrency: int):
    ch = await _safe_fetch_channel(channel_id)
    if not ch:
        print("SEED_WORKER: channel not found", channel_id)
        return

    start = db.question_bank_count()
    await ch.send(f"🧠 Seeding question bank: **{start:,}** → **{target_total:,}** (hard/expert heavy).")

    sem = asyncio.Semaphore(concurrency)
    inserted = 0
    last_report = 0

    async def one():
        nonlocal inserted
        async with sem:
            cand = await _gen_one(SEED_DIFFICULTY_WEIGHTS)
            if not cand:
                return
            qid = _qid(cand["question"])
            ok = db.upsert_question_bank(
                question_id=qid,
                sport=cand["sport"],
                difficulty=cand["difficulty"],
                question=cand["question"],
                choices=cand["choices"],
                answer_index=cand["answer_index"],
                explanation=cand["explanation"],
                tags=cand["tags"],
                created_ts=_now(),
            )
            if ok:
                inserted += 1

    try:
        while db.question_bank_count() < target_total:
            burst = max(20, concurrency * 25)
            tasks = [asyncio.create_task(one()) for _ in range(burst)]
            await asyncio.gather(*tasks, return_exceptions=True)

            if inserted - last_report >= 250:
                last_report = inserted
                await ch.send(f"📦 Bank now: **{db.question_bank_count():,}** (added **{inserted:,}** this run)")
            await asyncio.sleep(0.5)
    finally:
        SEED_TASKS.pop(guild_id, None)

    await ch.send(f"✅ Seeding complete. Bank total: **{db.question_bank_count():,}** (added **{inserted:,}**).")


@tree.command(name="seed_bank", description="Pre-generate at least 10,000 challenging trivia questions")
@app_commands.checks.has_permissions(manage_guild=True)
async def seed_bank(interaction: discord.Interaction, target_total: int = 10000, concurrency: int = 3):
    print("SEED_BANK_COMMAND_CALLED")  # <-- if you don't see this, Discord isn't calling your bot
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    if target_total < 10000:
        target_total = 10000
    concurrency = max(1, min(int(concurrency), 6))

    gid = str(interaction.guild_id)
    if gid in SEED_TASKS and not SEED_TASKS[gid].done():
        await interaction.followup.send("Seed job already running.", ephemeral=True)
        return

    channel_id = _pick_channel_id(interaction)
    current = db.question_bank_count()
    if current >= target_total:
        await interaction.followup.send(f"Bank already has **{current:,}** questions.", ephemeral=True)
        return

    SEED_TASKS[gid] = client.loop.create_task(_seed_worker(gid, channel_id, target_total, concurrency))
    await interaction.followup.send(f"✅ Seeding started. Progress will post in <#{channel_id}>.", ephemeral=True)


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
    await _post_question_for_guild(str(interaction.guild_id))


@tree.command(name="event_week", description="Start a 7-day trivia event (question every 30 minutes)")
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
    await _post_question_for_guild(str(interaction.guild_id))


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
    ch = await _safe_fetch_channel(int(event["channel_id"])) or interaction.channel

    try:
        await _announce_end(ch, event_id)
    except Exception as e:
        print("STOP_ANNOUNCE_END_FAILED:", repr(e))

    db.end_event(event_id)
    await interaction.followup.send("✅ Event stopped.", ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print("APP_COMMAND_ERROR:", repr(error))
    try:
        await interaction.response.send_message(f"Command error: {error}", ephemeral=True)
    except Exception:
        pass


@client.event
async def on_ready():
    # Force fast guild sync if GUILD_ID is set
    if GUILD_ID:
        gid = int(GUILD_ID)
        await tree.sync(guild=discord.Object(id=gid))
        print("SYNCED_COMMANDS_TO_GUILD:", gid)
    else:
        await tree.sync()
        print("SYNCED_COMMANDS_GLOBAL")

    print("COMMANDS_LOADED:", [c.name for c in tree.get_commands()])

    print(f"Logged in as {client.user} (ID: {client.user.id})")
    client.loop.create_task(scheduler_loop())


client.run(TOKEN)
