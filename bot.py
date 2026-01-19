import os
import time
import asyncio
import random
import hashlib
import re

import discord
from discord import app_commands

import db
from ai import generate_trivia, verify_trivia_mcq

# ========= ENV =========
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing bot token. Set DISCORD_TOKEN.")

TRIVIA_CHANNEL_ID = os.getenv("TRIVIA_CHANNEL_ID")  # optional
PY_UNBUFFERED_HINT = os.getenv("PYTHONUNBUFFERED")

print("BOT_VERSION: MONTH_ONLY_RUSH_8H_20Q", flush=True)

# ========= MONTH EVENT SETTINGS (LOCKED) =========
MONTH_LENGTH_SECONDS = 30 * 24 * 60 * 60

RUSH_EVERY_SECONDS = 8 * 60 * 60          # every 8 hours
RUSH_QUESTIONS = 20
RUSH_INTERVAL_SECONDS = 60               # 1 question per minute
ANSWER_WINDOW_SECONDS = 45               # must be < 60 to fit rush timing

FIRST_RUSH_WARMUP_SECONDS = 15           # start first rush quickly after /event_month

# Dupes control
RECENT_WINDOW = 1500

SPORTS = [
    "football", "men's basketball", "women's basketball", "baseball", "softball", "gymnastics",
    "track & field", "swimming & diving", "lacrosse", "soccer", "volleyball", "tennis", "golf",
    "cross country", "rowing", "recruiting", "olympics",
]
DIFFICULTIES = ["easy", "medium", "hard", "expert"]

# Challenging by default
LIVE_DIFFICULTY_WEIGHTS = [0.05, 0.20, 0.45, 0.30]
SEED_DIFFICULTY_WEIGHTS = [0.05, 0.15, 0.45, 0.35]

db.init_db()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

GUILD_LOCKS: dict[str, asyncio.Lock] = {}
SEED_TASKS: dict[str, asyncio.Task] = {}
ACTIVE_RUSH: set[str] = set()  # guild_ids currently running a rush


# ========= HELPERS =========
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


def _lock_for_guild(guild_id: str) -> asyncio.Lock:
    if guild_id not in GUILD_LOCKS:
        GUILD_LOCKS[guild_id] = asyncio.Lock()
    return GUILD_LOCKS[guild_id]


async def _safe_get_channel(channel_id: int):
    ch = client.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await client.fetch_channel(channel_id)
    except Exception as e:
        print("CHANNEL_FETCH_FAIL:", channel_id, repr(e), flush=True)
        return None


def _pick_channel_id_for_command(interaction: discord.Interaction) -> int:
    if TRIVIA_CHANNEL_ID:
        try:
            return int(TRIVIA_CHANNEL_ID)
        except Exception:
            pass
    return int(interaction.channel_id)


def _rules_text() -> str:
    return (
        "**🐊 Florida Gators Trivia – Official Rules**\n\n"
        "• **Honor system:** Please **do not look up answers** while a question is active.\n"
        "• Answer using your own knowledge — this is for fun, competition, and bragging rights.\n"
        "• Each question has a time limit. When time expires, answers lock and the correct answer is revealed.\n"
        "• The contest runs for the **full duration of the event (30 days)** and scores accumulate the whole time.\n"
        "• A **Top 10 leaderboard** will be posted when the event ends, and the winner will be announced **in this channel**.\n"
        "• Trivia is generated and verified by AI. Accuracy is taken seriously, but **occasional incorrect facts may appear**.\n"
        "  100% correctness **cannot be guaranteed**.\n\n"
        "**Go Gators 🐊**"
    )


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
            "type": "mcq",
            "question": q,
            "choices": choices,
            "answer_index": ans,
            "explanation": explanation,
            "tags": tags,
        }
    except Exception as e:
        print("VALIDATION_FAIL:", repr(e), flush=True)
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

    raw, _h = await asyncio.to_thread(generate_trivia, sport=sport, difficulty=difficulty, mode="MCQ", topic=topic)
    cand = _validate_mcq(raw)
    if not cand:
        return None

    verified, verdict = await asyncio.to_thread(verify_trivia_mcq, cand)
    if verdict == "FAIL" or not verified:
        return None

    return _validate_mcq(verified)


# ========= DISCORD UI =========
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
        except Exception as e:
            print("TIMEOUT_EDIT_FAIL:", repr(e), flush=True)
        try:
            if self.message:
                await self.message.channel.send(f"✅ Correct answer: **{self.choices[self.answer_index]}**")
        except Exception as e:
            print("TIMEOUT_ANSWER_POST_FAIL:", repr(e), flush=True)


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


# ========= EVENT FLOW =========
async def _announce_month_start(channel: discord.abc.Messageable):
    await channel.send(
        "**✅ Month Trivia Event started!**\n"
        "🗓️ Length: 30 days\n"
        "🕗 Every 8 hours: **Trivia Rush** (20 questions in ~20 minutes)\n"
        f"⏱️ {ANSWER_WINDOW_SECONDS} seconds to answer each question\n"
        "🏆 Top 10 posted at the end"
    )
    await channel.send(_rules_text())


async def _announce_rush_start(channel: discord.abc.Messageable):
    await channel.send(
        f"**🔥 Trivia Rush starting now!**\n"
        f"{RUSH_QUESTIONS} questions • ~{RUSH_QUESTIONS} minutes • {ANSWER_WINDOW_SECONDS}s per question\n"
        "Honor system reminder: don’t look up answers."
    )


async def _announce_end(channel: discord.abc.Messageable, event_id: int):
    top10 = db.top_scores(event_id, 10)
    lines = ["**🏁 Month Trivia Event ended!**", "", "**🏆 Top 10**"]
    if not top10:
        lines.append("No scores yet.")
    else:
        for i, (user_id, points) in enumerate(top10, start=1):
            lines.append(f"**{i}.** <@{user_id}> — **{points}**")
    await channel.send("\n".join(lines))


async def _get_question_for_event(guild_id: str, event_id: int):
    recent_count = db.guild_recent_count(guild_id)
    effective_window = min(RECENT_WINDOW, recent_count)

    trivia = db.pick_question_from_bank(guild_id, event_id, effective_window)

    # If bank is thin, generate a small batch of verified questions
    if not trivia:
        inserted = 0
        now = _now()
        for _ in range(80):
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
            if inserted >= 25:
                break

        trivia = db.pick_question_from_bank(guild_id, event_id, effective_window)

    return trivia


async def _ask_one(channel: discord.abc.Messageable, guild_id: str, event_id: int):
    now = _now()
    trivia = await _get_question_for_event(guild_id, event_id)
    if not trivia:
        await channel.send("⚠️ Couldn’t pull a verified question right now. Skipping this slot.")
        return

    qid = trivia["question_id"]

    # No repeats within the month event
    if not db.record_question(event_id, qid, now):
        await channel.send("⚠️ Duplicate detected. Skipping.")
        return

    db.guild_recent_add(guild_id, qid, now, RECENT_WINDOW)

    embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
    embed.set_footer(text=f"Sport: {trivia['sport']} • Difficulty: {trivia['difficulty']} • {ANSWER_WINDOW_SECONDS}s to answer")

    view = TriviaView(event_id, trivia["choices"], trivia["answer_index"])
    msg = await channel.send(embed=embed, view=view)
    view.message = msg


async def _run_rush(guild_id: str, channel_id: int, event_id: int):
    if guild_id in ACTIVE_RUSH:
        print("RUSH_ALREADY_RUNNING:", guild_id, flush=True)
        return

    ACTIVE_RUSH.add(guild_id)
    try:
        ch = await _safe_get_channel(channel_id)
        if not ch:
            print("RUSH_NO_CHANNEL:", guild_id, channel_id, flush=True)
            return

        print("RUSH_START:", guild_id, "event", event_id, flush=True)
        await _announce_rush_start(ch)

        for i in range(RUSH_QUESTIONS):
            await _ask_one(ch, guild_id, event_id)

            if i < RUSH_QUESTIONS - 1:
                await asyncio.sleep(RUSH_INTERVAL_SECONDS)

        await ch.send("✅ Trivia Rush complete. Next rush in ~8 hours.")
        print("RUSH_DONE:", guild_id, flush=True)

    finally:
        ACTIVE_RUSH.discard(guild_id)


async def _tick_guild(guild_id: str):
    async with _lock_for_guild(guild_id):
        event = db.get_active_event(guild_id)
        if not event:
            return

        event_id = int(event["id"])
        channel_id = int(event["channel_id"])
        now = _now()

        # End event
        if now >= int(event["end_ts"]):
            ch = await _safe_get_channel(channel_id)
            if ch:
                await _announce_end(ch, event_id)
            db.end_event(event_id)
            return

        # Rush timing
        if now < int(event["next_ask_ts"]):
            return

        # Set next rush immediately so we never double-trigger
        db.update_next_ask(event_id, now + RUSH_EVERY_SECONDS)

        # Run rush outside DB lock
        client.loop.create_task(_run_rush(guild_id, channel_id, event_id))


async def scheduler_loop():
    await client.wait_until_ready()
    print("SCHEDULER_STARTED", flush=True)
    while not client.is_closed():
        for g in client.guilds:
            try:
                await _tick_guild(str(g.id))
            except Exception as e:
                print("SCHEDULER_GUILD_ERROR:", str(g.id), repr(e), flush=True)
        await asyncio.sleep(15)


# ========= SEED BANK (OPTIONAL BUT RECOMMENDED) =========
async def _seed_worker(guild_id: str, channel_id: int, target_total: int, concurrency: int):
    ch = await _safe_get_channel(channel_id)
    if not ch:
        print("SEED_WORKER_NO_CHANNEL:", channel_id, flush=True)
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
    print("SEED_BANK_CALLED by", interaction.user.id, "guild", interaction.guild_id, flush=True)
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

    channel_id = _pick_channel_id_for_command(interaction)
    current = db.question_bank_count()

    await interaction.followup.send(
        f"✅ Seeding started. Bank is **{current:,}** now. Target: **{target_total:,}**. "
        f"Progress will post in <#{channel_id}>.",
        ephemeral=True,
    )

    SEED_TASKS[gid] = client.loop.create_task(_seed_worker(gid, channel_id, target_total, concurrency))


# ========= COMMANDS =========
@tree.command(
    name="event_month",
    description="Start a 30-day event: every 8 hours runs a 20-question Trivia Rush (1/min)."
)
async def event_month(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    gid = str(interaction.guild_id)

    if db.get_active_event(gid):
        await interaction.followup.send("An event is already running. Use /stop first.", ephemeral=True)
        return

    now = _now()
    end_ts = now + MONTH_LENGTH_SECONDS
    channel_id = _pick_channel_id_for_command(interaction)

    # Create the month event and schedule first rush shortly after start
    event_id = db.create_event(gid, str(channel_id), "month", now, end_ts, ANSWER_WINDOW_SECONDS)
    db.update_next_ask(event_id, now + FIRST_RUSH_WARMUP_SECONDS)

    ch = await _safe_get_channel(channel_id) or interaction.channel
    await _announce_month_start(ch)

    # ✅ Force the first rush without waiting on the scheduler tick
    async def _kickoff():
        await asyncio.sleep(FIRST_RUSH_WARMUP_SECONDS)
        # Double-check event still active
        ev = db.get_active_event(gid)
        if not ev:
            return
        await _run_rush(gid, channel_id, int(ev["id"]))

    client.loop.create_task(_kickoff())

    await interaction.followup.send(
        f"✅ Month event started. First Trivia Rush begins in ~{FIRST_RUSH_WARMUP_SECONDS} seconds.",
        ephemeral=True
    )


@tree.command(name="stop", description="Stop the current month trivia event")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("Use this in a server.", ephemeral=True)
        return

    gid = str(interaction.guild_id)
    event = db.get_active_event(gid)
    if not event:
        await interaction.followup.send("No active event.", ephemeral=True)
        return

    event_id = int(event["id"])
    ch = await _safe_get_channel(int(event["channel_id"])) or interaction.channel
    await _announce_end(ch, event_id)
    db.end_event(event_id)

    await interaction.followup.send("✅ Event stopped.", ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print("APP_COMMAND_ERROR:", repr(error), flush=True)
    try:
        await interaction.response.send_message(f"Command error: {error}", ephemeral=True)
    except Exception:
        try:
            await interaction.followup.send(f"Command error: {error}", ephemeral=True)
        except Exception:
            pass


@client.event
async def on_ready():
    await tree.sync()
    print("COMMANDS_LOADED:", [c.name for c in tree.get_commands()], flush=True)
    print("TRIVIA_CHANNEL_ID_ENV:", TRIVIA_CHANNEL_ID, flush=True)
    print("PYTHONUNBUFFERED:", PY_UNBUFFERED_HINT, flush=True)
    print(f"Logged in as {client.user} (ID: {client.user.id})", flush=True)
    client.loop.create_task(scheduler_loop())


client.run(TOKEN)
