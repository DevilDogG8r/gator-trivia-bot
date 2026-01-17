import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("trivia.db")

print("DB_LOADED:", __file__)


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
        c.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "guild_id TEXT NOT NULL, "
            "channel_id TEXT NOT NULL, "
            "event_type TEXT NOT NULL, "
            "start_ts INTEGER NOT NULL, "
            "end_ts INTEGER NOT NULL, "
            "next_ask_ts INTEGER NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1"
            ")"
        )

        c.execute(
            "CREATE TABLE IF NOT EXISTS event_questions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id INTEGER NOT NULL, "
            "question_id TEXT NOT NULL, "
            "asked_ts INTEGER NOT NULL, "
            "UNIQUE(event_id, question_id)"
            ")"
        )

        c.execute(
            "CREATE TABLE IF NOT EXISTS scores ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id INTEGER NOT NULL, "
            "user_id TEXT NOT NULL, "
            "points INTEGER NOT NULL DEFAULT 0, "
            "UNIQUE(event_id, user_id)"
            ")"
        )

        c.execute(
            "CREATE TABLE IF NOT EXISTS guild_recent_questions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "guild_id TEXT NOT NULL, "
            "question_id TEXT NOT NULL, "
            "asked_ts INTEGER NOT NULL"
            ")"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_grq_guild_id_id ON guild_recent_questions(guild_id, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_grq_guild_qid ON guild_recent_questions(guild_id, question_id)")


def get_active_event(guild_id):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM events WHERE guild_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        return dict(row) if row else None


def create_event(guild_id, channel_id, event_type, start_ts, end_ts):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO events "
            "(guild_id, channel_id, event_type, start_ts, end_ts, next_ask_ts, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (guild_id, channel_id, event_type, start_ts, end_ts, start_ts),
        )
        return cur.lastrowid


def end_event(event_id):
    with conn() as c:
        c.execute("UPDATE events SET active=0 WHERE id=?", (event_id,))


def update_next_ask(event_id, ts):
    with conn() as c:
        c.execute("UPDATE events SET next_ask_ts=? WHERE id=?", (ts, event_id))


def record_question(event_id, question_id, ts) -> bool:
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO event_questions (event_id, question_id, asked_ts) VALUES (?, ?, ?)",
                (event_id, question_id, ts),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def add_point(event_id, user_id):
    with conn() as c:
        c.execute(
            "INSERT INTO scores (event_id, user_id, points) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(event_id, user_id) DO UPDATE SET points = points + 1",
            (event_id, user_id),
        )


def top_scores(event_id, limit=10):
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, points FROM scores WHERE event_id=? ORDER BY points DESC LIMIT ?",
            (event_id, limit),
        ).fetchall()
        return [(r["user_id"], r["points"]) for r in rows]


# ---------- recent-window helpers ----------
def guild_recent_count(guild_id: str) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(1) AS n FROM guild_recent_questions WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


def guild_recent_has(guild_id: str, question_id: str, window_size: int) -> bool:
    if window_size <= 0:
        return False
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM guild_recent_questions "
            "WHERE guild_id=? AND question_id=? "
            "AND id >= (SELECT COALESCE(MAX(id) - ? + 1, 0) FROM guild_recent_questions WHERE guild_id=?) "
            "LIMIT 1",
            (guild_id, question_id, window_size, guild_id),
        ).fetchone()
        return row is not None


def guild_recent_add(guild_id: str, question_id: str, ts: int, window_size: int):
    with conn() as c:
        c.execute(
            "INSERT INTO guild_recent_questions (guild_id, question_id, asked_ts) VALUES (?, ?, ?)",
            (guild_id, question_id, ts),
        )
        c.execute(
            "DELETE FROM guild_recent_questions "
            "WHERE guild_id=? AND id < (SELECT COALESCE(MAX(id) - ? + 1, 0) FROM guild_recent_questions WHERE guild_id=?)",
            (guild_id, window_size, guild_id),
        )
