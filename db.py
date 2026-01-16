import sqlite3
import json
import time
from typing import Optional, List, Dict, Any, Tuple

DB_PATH = "gator_trivia.sqlite"


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _conn() as con:
        cur = con.cursor()

        # Per-channel game settings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            channel_id TEXT PRIMARY KEY,
            guild_id   TEXT NOT NULL,
            sport      TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            mode       TEXT NOT NULL,
            q_timeout  INTEGER NOT NULL DEFAULT 180
        )
        """)

        # Questions
        cur.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            payload    TEXT NOT NULL,
            answer_key TEXT NOT NULL,
            created_ts INTEGER NOT NULL
        )
        """)

        # Events (one active event per channel)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE NOT NULL,
            guild_id   TEXT NOT NULL,
            active     INTEGER NOT NULL,
            started_ts INTEGER NOT NULL,
            end_ts     INTEGER NOT NULL,
            next_ts    INTEGER NOT NULL,
            min_gap    INTEGER NOT NULL,
            max_gap    INTEGER NOT NULL
        )
        """)

        # Event scores (scoped to event_id)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS event_scores (
            event_id INTEGER NOT NULL,
            user_id  TEXT NOT NULL,
            points   INTEGER NOT NULL DEFAULT 0,
            correct  INTEGER NOT NULL DEFAULT 0,
            wrong    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, user_id)
        )
        """)

        con.commit()


# -------------------------
# Games
# -------------------------

def upsert_game(channel_id: str, guild_id: str, sport: str, difficulty: str, mode: str, q_timeout: int):
    with _conn() as con:
        con.execute("""
        INSERT INTO games(channel_id, guild_id, sport, difficulty, mode, q_timeout)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id=excluded.guild_id,
            sport=excluded.sport,
            difficulty=excluded.difficulty,
            mode=excluded.mode,
            q_timeout=excluded.q_timeout
        """, (channel_id, guild_id, sport, difficulty, mode, int(q_timeout)))
        con.commit()


def get_game(channel_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as con:
        cur = con.execute("SELECT channel_id,guild_id,sport,difficulty,mode,q_timeout FROM games WHERE channel_id=?", (channel_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "channel_id": row[0],
            "guild_id": row[1],
            "sport": row[2],
            "difficulty": row[3],
            "mode": row[4],
            "q_timeout": int(row[5]),
        }


# -------------------------
# Questions
# -------------------------

def record_question(channel_id: str, payload: Dict[str, Any], answer_key: str, created_ts: str) -> int:
    with _conn() as con:
        cur = con.execute("""
        INSERT INTO questions(channel_id, payload, answer_key, created_ts)
        VALUES(?,?,?,?)
        """, (channel_id, json.dumps(payload), answer_key, int(float(created_ts))))
        con.commit()
        return int(cur.lastrowid)


def get_question(qid: int) -> Dict[str, Any]:
    with _conn() as con:
        cur = con.execute("SELECT id, channel_id, payload, answer_key, created_ts FROM questions WHERE id=?", (qid,))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Question not found: {qid}")
        return {
            "id": row[0],
            "channel_id": row[1],
            "payload": json.loads(row[2]),
            "answer_key": row[3],
            "created_ts": int(row[4]),
        }


# -------------------------
# Events
# -------------------------

def start_event(channel_id: str, guild_id: str, duration_minutes: int, min_gap: int, max_gap: int) -> int:
    now = int(time.time())
    end_ts = now + int(duration_minutes) * 60
    next_ts = now  # post immediately
    with _conn() as con:
        # If an old event exists for this channel, deactivate it
        con.execute("UPDATE events SET active=0 WHERE channel_id=?", (channel_id,))
        cur = con.execute("""
        INSERT INTO events(channel_id, guild_id, active, started_ts, end_ts, next_ts, min_gap, max_gap)
        VALUES(?,?,?,?,?,?,?,?)
        """, (channel_id, guild_id, 1, now, end_ts, next_ts, int(min_gap), int(max_gap)))
        con.commit()
        return int(cur.lastrowid)


def stop_event(channel_id: str):
    with _conn() as con:
        con.execute("UPDATE events SET active=0 WHERE channel_id=?", (channel_id,))
        con.commit()


def get_active_events() -> List[Dict[str, Any]]:
    with _conn() as con:
        cur = con.execute("""
        SELECT id, channel_id, guild_id, started_ts, end_ts, next_ts, min_gap, max_gap
        FROM events
        WHERE active=1
        """)
        out = []
        for r in cur.fetchall():
            out.append({
                "id": int(r[0]),
                "channel_id": r[1],
                "guild_id": r[2],
                "started_ts": int(r[3]),
                "end_ts": int(r[4]),
                "next_ts": int(r[5]),
                "min_gap": int(r[6]),
                "max_gap": int(r[7]),
            })
        return out


def get_active_event_for_channel(channel_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as con:
        cur = con.execute("""
        SELECT id, channel_id, guild_id, started_ts, end_ts, next_ts, min_gap, max_gap
        FROM events
        WHERE active=1 AND channel_id=?
        """, (channel_id,))
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": int(r[0]),
            "channel_id": r[1],
            "guild_id": r[2],
            "started_ts": int(r[3]),
            "end_ts": int(r[4]),
            "next_ts": int(r[5]),
            "min_gap": int(r[6]),
            "max_gap": int(r[7]),
        }


def set_next_question_ts(channel_id: str, next_ts: int):
    with _conn() as con:
        con.execute("UPDATE events SET next_ts=? WHERE channel_id=? AND active=1", (int(next_ts), channel_id))
        con.commit()


# -------------------------
# Scoring (event-scoped)
# -------------------------

def bump_event_score(event_id: int, user_id: str, correct: bool, points: int):
    with _conn() as con:
        con.execute("""
        INSERT INTO event_scores(event_id, user_id, points, correct, wrong)
        VALUES(?,?,?,?,?)
        ON CONFLICT(event_id, user_id) DO UPDATE SET
            points = points + excluded.points,
            correct = correct + excluded.correct,
            wrong = wrong + excluded.wrong
        """, (int(event_id), user_id, int(points), 1 if correct else 0, 0 if correct else 1))
        con.commit()


def get_event_leaderboard(event_id: int, limit: int = 10) -> List[Tuple[str, int, int, int]]:
    with _conn() as con:
        cur = con.execute("""
        SELECT user_id, points, correct, wrong
        FROM event_scores
        WHERE event_id=?
        ORDER BY points DESC, correct DESC
        LIMIT ?
        """, (int(event_id), int(limit)))
        return [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in cur.fetchall()]
