"""记忆管理系统：分层记忆、向量检索、自动摘要."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PI_AGENT_DIR = ROOT / "Ming_Assistant"
MEMORY_DIR = PI_AGENT_DIR / "memory"
DB_PATH = ROOT / "data" / "memory.db"

# ---- 向量嵌入 ----

EMBEDDING_MODEL = "embedding-2"
EMBEDDING_URL = os.getenv("MINIMAX_API_URL", "https://api.minimaxi.com/v1/text/chatcompletion_v2").replace("chatcompletion_v2", "embeddings")


def _embedding(text: str) -> list[float] | None:
    """调用 MiniMax embedding 接口，返回归一化向量。"""
    import httpx
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return None
    payload = {"model": EMBEDDING_MODEL, "text": text[:2000]}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(EMBEDDING_URL, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        embedding = data.get("data", [{}])[0].get("embedding", [])
        if not embedding:
            return None
        # L2 归一化
        norm = math.sqrt(sum(x * x for x in embedding))
        return [x / norm for x in embedding] if norm > 0 else embedding
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---- SQLite 表 ----

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            embedding BLOB,
            is_summary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS daily_summaries (date TEXT PRIMARY KEY, summary TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_date ON memory_entries(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_category ON memory_entries(category)")
    conn.commit()
    return conn


# ---- 写入记忆 ----

def add_memory(content: str, category: str = "general") -> str:
    """写入一条记忆，返回 entry_id。"""
    embedding = _embedding(content)
    now = datetime.now(timezone.utc).isoformat()
    today = now[:10]
    conn = _conn()
    cursor = conn.execute(
        "INSERT INTO memory_entries(date, category, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
        (today, category, content, json.dumps(embedding) if embedding else None, now),
    )
    conn.commit()
    entry_id = cursor.lastrowid
    conn.close()
    # 检查是否需要生成摘要
    _maybe_summarize(today)
    return str(entry_id)


def _maybe_summarize(date: str) -> None:
    """如果某天记忆超过 3 条，生成摘要并删除原始条目的 embedding。"""
    conn = _conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM memory_entries WHERE date = ? AND is_summary = 0", (date,)
    ).fetchone()[0]
    if count < 3:
        conn.close()
        return
    rows = conn.execute(
        "SELECT content FROM memory_entries WHERE date = ? AND is_summary = 0 ORDER BY created_at", (date,)
    ).fetchall()
    conn.close()

    combined = "\n".join(r[0] for r in rows)
    summary = _generate_summary(date, combined)
    if not summary:
        return

    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO daily_summaries(date, summary, created_at) VALUES (?, ?, ?)", (date, summary, now))
    conn.execute("DELETE FROM memory_entries WHERE date = ? AND is_summary = 0", (date,))
    conn.commit()
    conn.close()


def _generate_summary(date: str, content: str) -> str | None:
    """调用 LLM 将多天记忆压缩为摘要。"""
    import httpx
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return None
    prompt = f"你是一个交易决策记录员。请将以下日期 {date} 的多条记录压缩为一段简洁摘要，保留关键决策、股票、风险结论。\n\n{content[:1500]}\n\n摘要："
    payload = {
        "model": os.getenv("MINIMAX_MODEL", "MiniMax-M2.7"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 300,
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(os.getenv("MINIMAX_API_URL", "https://api.minimaxi.com/v1/text/chatcompletion_v2"), json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        return None


# ---- 向量检索记忆 ----

def search_memory(query: str, top_k: int = 5, days: int = 90) -> list[dict[str, Any]]:
    """语义检索记忆，返回 top_k 条最相关的记忆。"""
    embedding = _embedding(query)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    conn = _conn()

    if not embedding:
        # fallback: 关键词匹配
        rows = conn.execute(
            "SELECT id, date, category, content, is_summary, created_at FROM memory_entries WHERE date >= ? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, f"%{query[:20]}%", top_k),
        ).fetchall()
        conn.close()
        return [_row_to_dict(row) for row in rows]

    rows = conn.execute(
        "SELECT id, date, category, content, embedding, is_summary, created_at FROM memory_entries WHERE date >= ? AND embedding IS NOT NULL",
        (cutoff,),
    ).fetchall()
    conn.close()

    scored: list[tuple[float, dict]] = []
    for row in rows:
        emb = json.loads(row[4])
        score = _cosine(embedding, emb)
        scored.append((score, _row_to_dict(row)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return {"id": row[0], "date": row[1], "category": row[2], "content": row[3], "is_summary": bool(row[5]), "created_at": row[6]}


# ---- 分层加载记忆 ----

def load_recent_memory(limit: int = 10, days: int = 7) -> str:
    """加载最近 limit 条非摘要记忆（最近 N 天），用于短期记忆。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    conn = _conn()
    rows = conn.execute(
        "SELECT date, content FROM memory_entries WHERE date >= ? AND is_summary = 0 ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    conn.close()
    if not rows:
        return ""
    return "\n".join(f"[{r[0]}] {r[1]}" for r in reversed(rows))


def load_summaries(days: int = 90) -> str:
    """加载所有摘要记忆（更长时间范围）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    conn = _conn()
    rows = conn.execute(
        "SELECT date, summary FROM daily_summaries WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall()
    conn.close()
    if not rows:
        return ""
    return "\n".join(f"[{r[0]}] {r[1]}" for r in rows)


def load_old_memories(days: int = 90) -> str:
    """加载没有摘要的旧记忆（更早的条目）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    conn = _conn()
    rows = conn.execute(
        "SELECT date, content FROM memory_entries WHERE date < ? AND is_summary = 0 AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 20",
        (cutoff,),
    ).fetchall()
    conn.close()
    if not rows:
        return ""
    return "\n".join(f"[{r[0]}] {r[1]}" for r in reversed(rows))


# ---- 传统文件兼容 ----

def read_agent_file(relative_path: str) -> str:
    path = PI_AGENT_DIR / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def append_to_file(relative_path: str, content: str) -> None:
    path = PI_AGENT_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(content + "\n")


# ---- 预警处理 ----

def load_scratchpad() -> str:
    return read_agent_file("memory/SCRATCHPAD.md")


def load_soul() -> str:
    return read_agent_file("memory/SOUL.md")


def load_skill() -> str:
    return read_agent_file("skills/risk_interpreter/SKILL.md")


