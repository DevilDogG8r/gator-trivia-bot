import time
import asyncio
import random
import hashlib

import discord
from discord import app_commands

import db

# --- REQUIRED: init DB at startup ---
db.init_db()

TOKEN = "PUT_YOUR_TOKEN_IN_ENV"
GUILD_ID = None   # optional
TRIVIA_CHANNEL_ID = None  # set this if you want forced channel

QUESTIONS = [
    {"id": "q1", "q": "What year did Florida win its first national title?", "a": ["1992", "1996", "2006"], "c": 1},
    {"id": "q2", "q": "Who was Florida's coach in 1996?", "a": ["Meyer", "Spurrier", "Zook"], "c": 1},
]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def trivia_loop():
    await client.wait_until_ready()

    while True:
        event = db.get_active_event(str(GUILD_ID))
        if not event:
            await asyncio.sleep(10)
            continue

        now = int(time.time())
        if now < event["next_ask_ts"]:
            await asyncio.sleep(5)
            continue

        channel = client.get_channel(int(event["channel_id"]))
        if not channel:
            await asyncio.sleep(10)
            continue

        q = random.choice(QUESTIONS)
        if db.event_has_question(event["id"], q["id"]):
            await asyncio.sleep(1)
            continue

        await channel.send(q["q"])
        db.record_question(event["id"], q["id"], now)
        db.update_next_ask(event["id"], now + 300)

        await asyncio.sleep(30)
        await channel.send(f"Correct answer: **{q['a'][q['c']]}**")


@tree.command(name="event_day")
async def event_day(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if db.get_active_event(str(interaction.guild_id)):
        await interaction.followup.send("Event already running", ephemeral=True)
        return

    now = int(time.time())
    db.create_event(
        str(interaction.guild_id),
        str(interaction.channel_id),
        "day",
        now,
        now + 86400
    )

    await interaction.followup.send("Day trivia event started", ephemeral=True)


@tree.command(name="stop")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    event = db.get_active_event(str(interaction.guild_id))
    if not event:
        await interaction.followup.send("No active event", ephemeral=True)
        return

    db.end_event(event["id"])
    await interaction.followup.send("Trivia stopped", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    client.loop.create_task(trivia_loop())
    print("Bot ready")


client.run(TOKEN)
