import sqlite3
import json
import time
from typing import Optional, Dict, Any

DB_PATH = "gator_trivia.sqlite"

def conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with conn() as c:
        cur = c.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS games(
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            sport TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            mode TEXT NOT NULL,
            seconds INTEGER NOT NULL,
            active_question_id INTEGER,
            ends_at INTEGER
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS scores(
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            wrong INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            last_played INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            answer_key TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            hash TEXT NOT NULL
        )
        """)
        c.commit()

def upsert_game(channel_id: str, guild_id: str, sport: str, difficulty: str, mode: str, seconds: int):
    with conn() as c:
        c.execute("""
        INSERT INTO games(channel_id,guild_id,sport,difficulty,mode,seconds,active_question_id,ends_at)
        VALUES(?,?,?,?,?,?,NULL,NULL)
        ON CONFLICT(channel_id) DO UPDATE SET
          guild_id=excluded.guild_id,
          sport=excluded.sport,
          difficulty=excluded.difficulty,
          mode=excluded.mode,
          seconds=excluded.seconds,
          active_question_id=NULL,
          ends_at=NULL
        """, (channel_id, guild_id, sport, difficulty, mode, seconds))
        c.commit()

def stop_game(channel_id: str):
    with conn() as c:
        c.execute("DELETE FROM games WHERE channel_id=?", (channel_id,))
        c.commit()

def get_game(channel_id: str) -> Optional[dict]:
    with conn() as c:
        cur = c.execute("SELECT channel_id,guild_id,sport,difficulty,mode,seconds,active_question_id,ends_at FROM games WHERE channel_id=?", (channel_id,))
        row = cur.fetchone()
        if not row:
            return None
        keys = ["channel_id","guild_id","sport","difficulty","mode","seconds","active_question_id","ends_at"]
        return dict(zip(keys, row))

def set_active_question(channel_id: str, qid: int, ends_at: int):
    with conn() as c:
        c.execute("UPDATE games SET active_question_id=?, ends_at=? WHERE channel_id=?", (qid, ends_at, channel_id))
        c.commit()

def record_question(channel_id: str, payload: Dict[str, Any], answer_key: str, h: str) -> int:
    with conn() as c:
        cur = c.cursor()
        cur.execute("""
        INSERT INTO questions(channel_id,payload_json,answer_key,created_at,hash)
        VALUES(?,?,?,?,?)
        """, (channel_id, json.dumps(payload), answer_key, int(time.time()), h))
        c.commit()
        return cur.lastrowid

def get_question(qid: int) -> Optional[dict]:
    with conn() as c:
        cur = c.execute("SELECT id, channel_id, payload_json, answer_key, created_at, hash FROM questions WHERE id=?", (qid,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "channel_id": row[1],
            "payload": json.loads(row[2]),
            "answer_key": row[3],
            "created_at": row[4],
            "hash": row[5]
        }

def question_hash_exists(h: str) -> bool:
    with conn() as c:
        cur = c.execute("SELECT 1 FROM questions WHERE hash=? LIMIT 1", (h,))
        return cur.fetchone() is not None

def bump_score(guild_id: str, user_id: str, correct: bool, points_delta: int):
    now = int(time.time())
    with conn() as c:
        # Ensure row exists
        c.execute("""
        INSERT OR IGNORE INTO scores(guild_id,user_id,points,correct,wrong,streak,last_played)
        VALUES(?,?,?,?,?,?,?)
        """, (guild_id, user_id, 0, 0, 0, 0, now))

        if correct:
            c.execute("""
            UPDATE scores
            SET points = points + ?,
                correct = correct + 1,
                streak = streak + 1,
                last_played = ?
            WHERE guild_id=? AND user_id=?
            """, (points_delta, now, guild_id, user_id))
        else:
            c.execute("""
            UPDATE scores
            SET wrong = wrong + 1,
                streak = 0,
                last_played = ?
            WHERE guild_id=? AND user_id=?
            """, (now, guild_id, user_id))
        c.commit()

def top_scores(guild_id: str, limit: int = 10):
    with conn() as c:
        cur = c.execute("""
        SELECT user_id, points, correct, wrong, streak
        FROM scores
        WHERE guild_id=?
        ORDER BY points DESC
        LIMIT ?
        """, (guild_id, limit))
        return cur.fetchall()
