"""对话会话管理：多轮对话历史（SQLite + LRU 缓存）."""
from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "sessions.db"

MAX_TURNS = 20           # 每个会话保留的最大对话轮数
MAX_SESSIONS = 50        # 内存中最多缓存多少个会话


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            history TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


class SessionStore:
    """LRU 缓存的会话存储。"""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        self._db_timestamps: dict[str, float] = {}

    def _load_from_db(self, session_id: str) -> list[dict[str, str]]:
        conn = _conn()
        row = conn.execute(
            "SELECT history, updated_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
        if not row:
            return []
        self._db_timestamps[session_id] = time.time()
        return json.loads(row[0])

    def _save_to_db(self, session_id: str, history: list[dict[str, str]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, history, created_at, updated_at) VALUES (?, ?, COALESCE((SELECT created_at FROM sessions WHERE session_id = ?), ?), ?)",
            (session_id, json.dumps(history, ensure_ascii=False), session_id, now, now),
        )
        conn.commit()
        conn.close()

    def get(self, session_id: str) -> list[dict[str, str]]:
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
            return self._cache[session_id]

        history = self._load_from_db(session_id)
        self._cache[session_id] = history
        self._evict_if_needed()
        return history

    def append(self, session_id: str, role: str, content: str) -> list[dict[str, str]]:
        history = self.get(session_id)
        history.append({"role": role, "content": content})
        # 限制轮数
        if len(history) > MAX_TURNS * 2:
            history = history[-(MAX_TURNS * 2):]
        self._cache[session_id] = history
        self._save_to_db(session_id, history)
        self._db_timestamps[session_id] = time.time()
        return history

    def clear(self, session_id: str) -> None:
        if session_id in self._cache:
            del self._cache[session_id]
        conn = _conn()
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()

    def _evict_if_needed(self) -> None:
        while len(self._cache) > MAX_SESSIONS:
            oldest = next(iter(self._cache))
            del self._cache[oldest]


# 全局单例
_store = SessionStore()


def get_session(session_id: str) -> list[dict[str, str]]:
    return _store.get(session_id)


def append_to_session(session_id: str, role: str, content: str) -> list[dict[str, str]]:
    return _store.append(session_id, role, content)


def clear_session(session_id: str) -> None:
    _store.clear(session_id)
