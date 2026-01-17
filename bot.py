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

# LOCKED EVENT SETTINGS
DAY_QUESTION_INTERVAL_SECONDS = 5 * 60        # every 5 minutes
WEEK_QUESTION_INTERVAL_SECONDS = 30 * 60      # every 30 minutes
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
# Default mix for live events (still challenging, but not impossible).
DIFFICULTY_WEIGHTS = [0.15, 0.35, 0.35, 0.15]

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


def _interval_for_event_type(event_type: str) -> int:
    return DAY_QUESTION_INTERVAL_SECONDS if event_type == "day" else WEEK_QUESTION_INTERVAL_SECONDS


def _interval_label(event_type: str) -> str:
    return "Every 5 minutes" if event_type == "day" else "Every 30 minutes"


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

        out = {"question": q, "choices": choices, "answer_index": ans, "sport": sport, "difficulty": difficulty}
        if isinstance(trivia.get("explanation"), str):
            out["explanation"] = _clean(trivia.get("explanation", ""))
        if isinstance(trivia.get("tags"), list):
            out["tags"] = [_clean(t) for t in trivia.get("tags", []) if _clean(t)]
        if isinstance(trivia.get("confidence"), (int, float)):
            try:
                out["confidence"] = float(trivia.get("confidence"))
            except Exception:
                pass
        return out
    except Exception:
        return None


async def _get_candidate() -> dict | None:
    """Return a validated trivia dict (question, choices, answer_index, sport, difficulty)."""
    sport = random.choice(SPORTS)
    difficulty = random.choices(DIFFICULTIES, weights=DIFFICULTY_WEIGHTS, k=1)[0]

    # Add topic variety so we can scale into 10k-25k questions without feeling repetitive.
    topic = random.choice(
        [
            "championships and titles",
            "coaches and coaching eras",
            "awards and honors",
            "records and milestones",
            "venues and traditions",
            "iconic games and moments",
            "recruiting (commits, flips, signing classes)",
            "Olympics (UF/Florida athletes, medals, events)",
            "all-time great players",
        ]
    )

    # Generate via OpenAI and validate. Offload sync call so we don't block the event loop.
    if generate_trivia:
        try:
            trivia, _h = await asyncio.to_thread(generate_trivia, sport=sport, difficulty=difficulty, mode="MCQ", topic=topic)
            v = _validate_trivia(trivia) if isinstance(trivia, dict) else None
            if v:
                return v
        except Exception as e:
            print("AI_TRIVIA_ERROR:", repr(e))

    # Fallback (only used if AI is unavailable). Small bank, but still valid.
    fb = random.choice(FALLBACK_BANK).copy()
    return _validate_trivia(fb)


async def _get_candidate_seed() -> dict | None:
    """Heavier on hard/expert + recruiting/Olympics for building a challenging bank."""
    sport = random.choice(SPORTS)
    difficulty = random.choices(DIFFICULTIES, weights=[0.05, 0.20, 0.45, 0.30], k=1)[0]

    topic = random.choice(
        [
            "recruiting (commits, flips, signing classes, staff, evaluations)",
            "Olympics (UF/Florida athletes, medals, events, years)",
            "records and milestones",
            "awards and honors",
            "postseason and championships",
            "iconic games and moments",
            "coaches and coaching eras",
            "all-time great players",
            "venue history and traditions (deep cuts)",
        ]
    )

    if generate_trivia:
        try:
            trivia, _h = await asyncio.to_thread(generate_trivia, sport=sport, difficulty=difficulty, mode="MCQ", topic=topic)
            v = _validate_trivia(trivia) if isinstance(trivia, dict) else None
            if v:
                return v
        except Exception as e:
            print("AI_TRIVIA_ERROR(SEED):", repr(e))

    fb = random.choice(FALLBACK_BANK).copy()
    return _validate_trivia(fb)


async def _seed_bank_worker(guild_id: str, channel_id: int, target_total: int, concurrency: int = 3):
    """Generate questions until the global bank has target_total rows."""
    ch = await _safe_fetch_channel(channel_id)
    if not ch:
        return

    start_total = db.question_bank_count()
    await ch.send(
        f"🧠 Seeding trivia bank: starting at **{start_total:,}** questions. Target: **{target_total:,}**.\n"
        f"This will generate mostly **hard/expert** questions (including recruiting + Olympics)."
    )

    inserted = 0
    last_report = 0

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def make_one():
        nonlocal inserted
        async with sem:
            cand = await _get_candidate_seed()
            if not cand:
                return
            qid = _qid_from_question_only(cand["question"])
            ok = db.upsert_question_bank(
                question_id=qid,
                sport=str(cand.get("sport") or ""),
                difficulty=str(cand.get("difficulty") or ""),
                question=str(cand["question"]),
                choices=list(cand["choices"]),
                answer_index=int(cand["answer_index"]),
                explanation=str(cand.get("explanation") or ""),
                tags=list(cand.get("tags") or []),
                created_ts=_now(),
            )
            if ok:
                inserted += 1

    try:
        while True:
            current_total = db.question_bank_count()
            if current_total >= target_total:
                break

            burst = max(10, concurrency * 10)
            tasks = [asyncio.create_task(make_one()) for _ in range(burst)]
            await asyncio.gather(*tasks, return_exceptions=True)

            if inserted - last_report >= 500 or (db.question_bank_count() >= target_total):
                last_report = inserted
                current_total = db.question_bank_count()
                await ch.send(f"📦 Bank progress: **{current_total:,}** total (added **{inserted:,}** this run)")

            await asyncio.sleep(0.5)
    finally:
        SEED_TASKS.pop(guild_id, None)

    end_total = db.question_bank_count()
    await ch.send(f"✅ Seeding complete. Bank now has **{end_total:,}** questions (added **{inserted:,}**).")


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
            for _ in range(30):
                cand = await _get_candidate()
                if not cand:
                    continue
                qid = _qid_from_question_only(cand["question"])
                if effective_window > 0 and db.guild_recent_has(guild_id, qid, effective_window):
                    continue
                ok = db.upsert_question_bank(
                    question_id=qid,
                    sport=str(cand.get("sport") or ""),
                    difficulty=str(cand.get("difficulty") or ""),
                    question=str(cand["question"]),
                    choices=list(cand["choices"]),
                    answer_index=int(cand["answer_index"]),
                    explanation=str(cand.get("explanation") or ""),
                    tags=list(cand.get("tags") or []),
                    created_ts=now,
                )
                if ok:
                    inserted += 1
                if inserted >= 12:
                    break

            trivia = db.pick_question_from_bank(guild_id, event_id, effective_window)

        if not trivia:
            db.update_next_ask(event_id, now + 60)
            await ch.send("⚠️ Couldn’t pull a fresh question right now. Trying again in 60 seconds.")
            return

        qid = trivia.get("question_id") or _qid_from_question_only(trivia["question"])

        if not db.record_question(event_id, qid, now):
            db.update_next_ask(event_id, now + 60)
            return

        db.guild_recent_add(guild_id, qid, now, RECENT_WINDOW)
        db.update_next_ask(event_id, now + interval)

        embed = discord.Embed(title="🐊 Florida Gators Trivia", description=trivia["question"])
        embed.set_footer(
            text=f"Sport: {trivia.get('sport','')} • Difficulty: {trivia.get('difficulty','')} • "
                 f"You have {ANSWER_WINDOW_SECONDS} seconds to answer."
        )

        view = TriviaView(event_id, trivia["choices"], int(trivia["answer_index"]))
        msg = await ch.send(embed=embed, view=view)
        view.message = msg
        return


async def scheduler_loop():
    await clien
