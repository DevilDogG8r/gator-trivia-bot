# db.py
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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


def init_db() -> None:
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                event_type TEXT NOT NULL,   -- 'day' or 'week'
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
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                UNIQUE(event_id, user_id)
            )
        """)


def get_active_event(guild_id: str) -> Optional[Dict[str, Any]]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM events WHERE guild_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (guild_id,)
        ).fetchone()
        return dict(row) if row else None


def create_event(
    guild_id: str,
    channel_id: str,
    event_type: str,
    start_ts: int,
    end_ts: int,
    next_ask_ts: int
) -> int:
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO events (guild_id, channel_id, event_type, start_ts, end_ts, next_ask_ts, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (guild_id, channel_id, event_type, start_ts, end_ts, next_ask_ts)
        )
        return int(cur.lastrowid)


def end_event(event_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE events SET active = 0 WHERE id = ?", (event_id,))


def update_next_ask(event_id: int, next_ask_ts: int) -> None:
    with conn() as c:
        c.execute("UPDATE events SET next_ask_ts = ? WHERE id = ?", (next_ask_ts, event_id))


def record_question_asked(event_id: int, question_id: str, asked_ts: int) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO event_questions (event_id, question_id, asked_ts) VALUES (?, ?, ?)",
            (event_id, question_id, asked_ts)
        )


def event_has_question(event_id: int, question_id: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM event_questions WHERE event_id = ? AND question_id = ? LIMIT 1",
            (event_id, question_id)
        ).fetchone()
        return row is not None


def add_points(event_id: int, user_id: str, points: int = 1) -> None:
    with conn() as c:
        c.execute(
            """
            INSERT INTO scores (event_id, user_id, points)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id, user_id)
            DO UPDATE SET points = points + excluded.points
            """,
            (event_id, user_id, points)
        )


def get_top_scores(event_id: int, limit: int = 10) -> List[Tuple[str, int]]:
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, points FROM scores WHERE event_id = ? ORDER BY points DESC LIMIT ?",
            (event_id, limit)
        ).fetchall()
        return [(r["user_id"], r["points"]) for r in rows]
