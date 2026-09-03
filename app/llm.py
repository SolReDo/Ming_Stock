"""LLM 交互层：分离 context 与 memory，分层 prompt 组装，多轮对话支持."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.memory import (
    add_memory,
    append_to_file,
    load_old_memories,
    load_recent_memory,
    load_scratchpad,
    load_skill,
    load_soul,
    load_summaries,
    search_memory,
)
from app.session import append_to_session, get_session

MINIMAX_API_URL = os.getenv("MINIMAX_API_URL", "https://api.minimaxi.com/v1/text/chatcompletion_v2")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")


# ---- System Prompt 组装（分层记忆） ----

def assemble_system_prompt(message: str) -> str:
    prompt_name = "check.md" if message.startswith("/check") else "review.md" if message.startswith("/review") else ""

    sections = [
        load_soul(),                          # 人格规则（始终）
        load_skill(),                         # 技能文件（始终）
        _load_prompt(prompt_name),            # 命令专用提示（如有）
        _build_memory_section(message),       # 分层记忆区
        _build_scratchpad_section(),          # 待处理预警
    ]
    return "\n\n".join(s for s in sections if s.strip())


def _load_prompt(name: str) -> str:
    if not name:
        return ""
    path = os.getenv("PI_AGENT_DIR", "") or ""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "Ming_Assistant" / "prompts" / name
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _build_memory_section(message: str) -> str:
    """分层构建记忆区：
    1. 短期记忆（近 7 天，最新 10 条）
    2. 语义检索记忆（当前 query 相关）
    3. 摘要记忆（90 天内的日级别摘要）
    4. 旧记忆（90 天前仍有 embedding 的条目）
    """
    parts = ["## 近期记忆（近 7 天）\n" + (load_recent_memory(limit=10, days=7) or "（无）")]

    # 语义检索
    if len(message) > 3:
        relevant = search_memory(message, top_k=5, days=90)
        if relevant:
            parts.append("## 相关记忆（语义检索）\n" + "\n".join(f"[{r['date']}] {r['content']}" for r in relevant))

    # 摘要
    summaries = load_summaries(days=90)
    if summaries:
        parts.append("## 历史摘要（90 天内）\n" + summaries)

    # 旧记忆
    old = load_old_memories(days=90)
    if old:
        parts.append("## 早期记忆（90 天前）\n" + old)

    return "\n\n".join(parts)


def _build_scratchpad_section() -> str:
    scratch = load_scratchpad()
    if scratch and "<!--" not in scratch:
        return "## 待处理预警\n" + scratch
    return ""


# ---- LLM 调用 ----

async def chat(
    message: str,
    context: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        raise RuntimeError("未配置 MINIMAX_API_KEY")

    # 多轮对话历史
    history: list[dict[str, str]] = []
    if session_id:
        history = get_session(session_id)

    # context 格式化为独立区块
    context_text = _format_context(context)

    system_prompt = assemble_system_prompt(message)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message + context_text})

    payload = {
        "model": MINIMAX_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(MINIMAX_API_URL, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    response.raise_for_status()
    data = response.json()
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("MiniMax 返回格式不正确") from error
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("MiniMax 返回空内容")

    # 追加到会话历史
    if session_id:
        append_to_session(session_id, "user", message)
        append_to_session(session_id, "assistant", answer)

    # 自动记忆：如有风险确认决策，写入记忆
    if session_id and _is_decision_message(message):
        _record_decision(session_id, message, answer)

    return answer.strip()


async def chat_stream(
    message: str,
    context: dict[str, Any] | None = None,
    session_id: str | None = None,
):
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        raise RuntimeError("未配置 MINIMAX_API_KEY")

    history: list[dict[str, str]] = []
    if session_id:
        history = get_session(session_id)

    context_text = _format_context(context)
    system_prompt = assemble_system_prompt(message)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message + context_text})

    payload = {
        "model": MINIMAX_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": True,
    }

    answer = ""
    yielded = False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream("POST", MINIMAX_API_URL, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or choice.get("text") or ""
                    if text:
                        answer += text
                        yielded = True
                        yield text
    except (httpx.HTTPError, TimeoutError):
        pass

    if not yielded and answer:
        for i in range(0, len(answer), 24):
            yield answer[i : i + 24]
        return

    if not yielded:
        content = await chat(message, context, session_id)
        for i in range(0, len(content), 24):
            yield content[i : i + 24]
        return

    # 流式成功后同样追加历史并记录决策
    if session_id:
        append_to_session(session_id, "user", message)
        append_to_session(session_id, "assistant", answer)
        if _is_decision_message(message):
            _record_decision(session_id, message, answer)


# ---- 工具函数 ----

def _format_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    lines = ["\n\n## 当前系统数据："]
    for key, value in context.items():
        lines.append(f"### {key}")
        lines.append(str(value)[:800])  # 限制单条长度
    return "\n".join(lines)


def _is_decision_message(message: str) -> bool:
    decision_keywords = ("确认", "confirm", "决策", "下单", "买入", "卖出", "加入自选", "加入监控")
    return any(kw in message for kw in decision_keywords)


def _record_decision(session_id: str, message: str, answer: str) -> None:
    try:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = f"\n- {date} [session:{session_id[:8]}]: {message.strip()[:100]} → {answer.strip()[:100]}"
        add_memory(content, category="decision")
        append_to_file("memory/MEMORY.md", content)
    except Exception:
        pass
