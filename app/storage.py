from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "agent.db"
AGENT_DIR = ROOT / "Ming_Assistant"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS risk_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                verdict TEXT NOT NULL,
                score INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_risk(result: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute("INSERT INTO risk_checks(symbol, verdict, score, result_json, created_at) VALUES (?, ?, ?, ?, ?)", (result["symbol"], result["verdict"], result["score"], json.dumps(result, ensure_ascii=False), now()))


def list_risk(symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT id, symbol, verdict, score, result_json, created_at FROM risk_checks WHERE (? IS NULL OR symbol = ?) ORDER BY id DESC LIMIT ?", (symbol, symbol, limit)).fetchall()
    return [{"id": row["id"], "symbol": row["symbol"], "verdict": row["verdict"], "score": row["score"], "result": json.loads(row["result_json"]), "created_at": row["created_at"]} for row in rows]


def add_watch(symbol: str) -> None:
    with connect() as connection:
        connection.execute("INSERT OR IGNORE INTO watchlist(symbol, created_at) VALUES (?, ?)", (symbol, now()))


def remove_watch(symbol: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


def get_watchlist() -> list[str]:
    with connect() as connection:
        return [row["symbol"] for row in connection.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()]


def add_alert(symbol: str, verdict: str, reason: str) -> None:
    with connect() as connection:
        connection.execute("INSERT INTO alerts(symbol, verdict, reason, created_at) VALUES (?, ?, ?, ?)", (symbol, verdict, reason, now()))
    _append_markdown("memory/SCRATCHPAD.md", f"- [ ] {now()[:10]} {symbol} {verdict}: {reason}\n")


def get_alerts(pending_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT id, symbol, verdict, reason, resolved, created_at FROM alerts"
    if pending_only:
        query += " WHERE resolved = 0"
    query += " ORDER BY id DESC LIMIT 100"
    with connect() as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def resolve_alert(alert_id: int) -> None:
    with connect() as connection:
        connection.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (alert_id,))


def add_decision(symbol: str, decision: str, note: str) -> None:
    with connect() as connection:
        connection.execute("INSERT INTO decisions(symbol, decision, note, created_at) VALUES (?, ?, ?, ?)", (symbol, decision, note, now()))
    _append_markdown("memory/MEMORY.md", f"## {now()[:10]}\n- {symbol}: {decision}. {note}\n")


def add_daily(log_date: str, content: str) -> None:
    with connect() as connection:
        connection.execute("INSERT INTO daily_logs(log_date, content, created_at) VALUES (?, ?, ?)", (log_date, content, now()))
    _append_markdown(f"memory/daily/{log_date}.md", f"\n- {content}\n")


def get_daily(log_date: str) -> list[dict[str, Any]]:
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT id, log_date, content, created_at FROM daily_logs WHERE log_date = ? ORDER BY id DESC", (log_date,)).fetchall()]


def _append_markdown(relative_path: str, content: str) -> None:
    path = AGENT_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(content)


# ---- 市场情绪池（高标股）----
SENTIMENT_PATH = ROOT / "data" / "market_sentiment.json"


def save_market_sentiment(pool: list[dict[str, Any]], cross_section: dict[str, list[float]], updated_at: str) -> None:
    SENTIMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTIMENT_PATH.write_text(json.dumps({"pool": pool, "cross_section": cross_section, "updated_at": updated_at}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_market_sentiment() -> dict[str, Any] | None:
    if not SENTIMENT_PATH.exists():
        return None
    try:
        return json.loads(SENTIMENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---- 市场扫描候选人股票列表（沪深全市场）----
# 覆盖主要板块的代表性股票，用于每日盘后扫描高标
MARKET_SCAN_POOL = [
    # 银行
    "000001", "600036", "600000", "601166", "601398", "601288", "601328", "601818", "600015", "601988",
    # 保险
    "601318", "601628", "601336", "601319",
    # 白酒
    "000858", "600519", "000568", "000596", "002304", "603369",
    # 食品饮料
    "600887", "002557", "000895", "603288", "600438",
    # 家电
    "000333", "600690", "000651", "002508",
    # 新能源车
    "300750", "002594", "300124", "300014", "002812", "603799",
    # 光伏
    "601012", "002459", "600438", "300274", "603806",
    # 半导体
    "688981", "002371", "603986", "600584", "002049",
    # 消费电子
    "002475", "000725", "601138", "300408",
    # 医药
    "000538", "600276", "603259", "300760", "301573",
    # 互联网
    "300033", "603196",
    # 房地产
    "600048", "001979", "601155",
    # 基建
    "601668", "601390", "600028",
    # 化工
    "600309", "002601", "603260",
    # 航运
    "601919", "601866", "600026",
    # 煤炭
    "601088", "600188", "600971",
    # 钢铁
    "600019", "000898", "002110",
    # 军工
    "600893", "002013", "600038", "601989",
    # 锂电池
    "002460", "002709", "300618", "603799",
    # 风电
    "002202", "300772", "603806",
    # 储能
    "300274", "002459",
    # 游戏
    "002555", "603444", "300413",
    # 传媒
    "300058", "002602",
    # 算力/AI
    "300496", "688339",
    # 机器人
    "688777", "002230",
]
