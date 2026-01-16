# db.py
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("trivia.db")

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            event_type TEXT NOT NULL,            -- 'day' or 'week'
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            next_ask_ts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS event_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            asked_ts INTEGER NOT NULL,
            UNIQUE(event_id, question_id),
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            UNIQUE(event_id, user_id),
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
        """)

def get_active_event(guild_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE guild_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (guild_id,)
        ).fetchone()
        return dict(row) if row else None

def create_event(guild_id: str, channel_id: str, event_type: str, start_ts: int, end_ts: int, next_ask_ts: int):
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO events (guild_id, channel_id, event_type, start_ts, end_ts, next_ask_ts, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (guild_id, channel_id, event_type, start_ts, end_ts, next_ask_ts),
        )
        return cur.lastrowid

def end_event(event_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE events SET active = 0 WHERE id = ?", (event_id,))

def update_next_ask(event_id: int, next_ask_ts: int):
    with get_conn() as conn:
        conn.execute("UPDATE events SET next_ask_ts = ? WHERE id = ?", (next_ask_ts, event_id))

def record_question_asked(event_id: int, question_id: str, asked_ts: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO event_questions (event_id, question_id, asked_ts) VALUES (?, ?, ?)",
            (event_id, question_id, asked_ts),
        )

def event_has_question(event_id: int, question_id: str) -> bool:
    """Returns True if this question_id has already been used in this event."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM event_questions WHERE event_id = ? AND question_id = ? LIMIT 1",
            (event_id, question_id)

