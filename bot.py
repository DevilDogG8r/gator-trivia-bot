import time
import discord
from discord import app_commands

import db
from ai import generate_trivia
from match import free_is_correct


# -----------------------------
# Helpers
# -----------------------------

def points_for(difficulty: str) -> int:
    return 2 if difficulty == "Easy" else 4 if difficulty == "Medium" else 6


# -----------------------------
# Question Posting
# -----------------------------

async def post_next_question(channel: discord.abc.Messageable):
    game = db.get_game(str(channel.id))
    if not game:
        await channel.send("No active game in this channel.")
        return

    payload = None
    err = None

    # Try a few times in case OpenAI gives a weak result
    for _ in range(3):
        try:
            p, _ = generate_trivia(
                game["sport"],
                game["difficulty"],
                game["mode"]
            )
            if p.get("confidence", 0) < 0.6:
                continue
            payload = p
            break
        except Exception as e:
            err = e

    if not payload:
        await channel.send(f"❌ Failed to generate question: `{err}`")
        return

    # Save question
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
        title=f"

