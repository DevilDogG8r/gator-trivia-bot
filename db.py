import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("trivia.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            next_ask_ts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS event_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            asked_ts INTEGER NOT NULL,
            UNIQUE(event_id, question_id)
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS
