from __future__ import annotations

import os
import math
import re
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import httpx
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from app import storage
from app.scheduler import Job, JobStore, Scheduler
from app.microstructure import calculate_microstructure_factors
from app import llm
from app import session as session_mgr

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
load_dotenv(ROOT / ".env")
TICKDB_BASE_URL = "https://api.tickdb.ai"
TICKDB_API_KEY = os.getenv("TICKDB_API_KEY", "")
USE_MOCK_DATA = os.getenv("USE_MOCK_DATA", "false").lower() in ("true", "1", "yes")

app = FastAPI(title="Pi Trading Risk Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
job_scheduler = Scheduler(JobStore(ROOT / "Ming_Assistant" / "cron" / "jobs.json"), lambda job: execute_scheduled_job(job))
_sentiment_cache: dict[str, object] = {"ts": 0.0, "data": {}}


@app.on_event("startup")
async def startup() -> None:
    storage.init_db()
    await job_scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await job_scheduler.stop()


class RiskRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RiskResult(BaseModel):
    symbol: str
    name: str
    verdict: Literal["SAFE", "WATCH", "DANGER", "ERROR"]
    score: int
    action: str
    reasons: list[str]
    indicators: dict[str, float | None]
    quote: dict[str, float | str]
    bars: list[dict[str, float | str]]
    minute_bars: list[dict[str, float | str]]
    microstructure_factors: dict[str, float | None]
    data_timestamp: str
    calculation_timestamp: str


class DecisionRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    decision: Literal["confirm", "reject"]
    note: str = ""


class DailyLogRequest(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=64)


class JobRequest(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{8,64}$")
    name: str = Field(min_length=1, max_length=100)
    task: Literal["scan_watchlist", "daily_review", "scan_high_beta"]
    schedule_kind: Literal["cron", "every", "at"]
    schedule_value: str = Field(min_length=1, max_length=100)
    session_target: Literal["main", "isolated"] = "isolated"
    delivery: Literal["announce", "webhook", "none"] = "none"
    enabled: bool = True
    timeout_seconds: int = Field(default=3600, ge=1, le=3600)
    retry_limit: int = Field(default=2, ge=0, le=5)


# 大盘指数代码（用于市场情绪分析）
MARKET_INDICES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "000016": "上证50",
    "000300": "沪深300",
}


def stock_name(symbol: str) -> str:
    return MARKET_INDICES.get(symbol, "演示股票")


def tickdb_symbol(symbol: str) -> str:
    return f"{symbol}.{'SH' if symbol.startswith('6') else 'SZ'}"


def tickdb_capital_flow_symbol(symbol: str) -> str:
    """capital-flow 端点使用与其他端点相同的 symbol 格式。"""
    return symbol  # 如 000001.SZ / 600036.SH


import random
import time as time_module


def _mock_kline(interval: str, limit: int) -> dict[str, object]:
    """生成模拟 K 线数据。"""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    bars = []
    base_price = 10.0
    for i in range(limit):
        if interval == "1d":
            ts = now - (limit - i) * 86400 * 1000
        else:
            ts = now - (limit - i) * 60000
        close = round(base_price + random.uniform(-0.5, 0.5), 2)
        open_ = round(close + random.uniform(-0.2, 0.2), 2)
        high = round(max(close, open_) + random.uniform(0, 0.2), 2)
        low = round(min(close, open_) - random.uniform(0, 0.2), 2)
        volume = round(random.uniform(1e6, 1e8), 0)
        bars.append({"time": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})
        base_price = close
    return {"code": 0, "message": "success", "data": {"klines": bars}}


def _mock_capital_flow(symbol: str) -> dict[str, object]:
    """生成模拟资金流向数据。"""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    flow = []
    for i in range(240):
        ts = now - (240 - i) * 60000
        flow.append({"timestamp": ts, "inflow": random.uniform(-1e6, 1e7)})
    return {"code": 0, "message": "success", "data": {"intraday_flow": flow}}


async def tickdb_get(path: str, params: dict[str, str | int]) -> dict[str, object]:
    print(f"[tickdb] {time_module.strftime('%H:%M:%S')} path={path} params={params}")
    if USE_MOCK_DATA:
        interval = params.get("interval", "1d")
        limit = params.get("limit", 80)
        if "capital-flow" in path:
            return _mock_capital_flow(params.get("symbol", ""))
        return _mock_kline(interval, limit)
    if not TICKDB_API_KEY:
        raise RuntimeError("未配置 TICKDB_API_KEY")
    async with httpx.AsyncClient(base_url=TICKDB_BASE_URL, timeout=10) as client:
        response = await client.get(path, params=params, headers={"X-API-Key": TICKDB_API_KEY})
    try:
        payload = response.json()
    except ValueError as error:
        response.raise_for_status()
        raise RuntimeError("TickDB 返回了无法解析的响应") from error
    if payload.get("code", 0) not in (0, None):
        details = payload.get("data")
        suffix = f" ({details})" if details else ""
        raise RuntimeError(f"{payload.get('message', 'TickDB 请求失败')}{suffix}")
    response.raise_for_status()
    return payload


def parse_capital_flow(payload: dict[str, object]) -> list[float]:
    """从 TickDB capital-flow 响应中提取日内的净流入序列。
    intraday_flow 每个元素含 timestamp 和 inflow（累计流入额，元）。
    对 inflow 差分得到每个时刻的净流入。
    """
    data = payload.get("data", payload)
    flow_array = data.get("intraday_flow", []) if isinstance(data, dict) else []
    if not flow_array or not isinstance(flow_array, list):
        return []
    net_flow: list[float] = []
    for index, item in enumerate(flow_array):
        if not isinstance(item, dict):
            continue
        try:
            current = float(item.get("inflow", 0))
        except (ValueError, TypeError):
            current = 0.0
        if index == 0:
            net_flow.append(current)
        else:
            prev = flow_array[index - 1]
            try:
                prev_val = float(prev.get("inflow", 0)) if isinstance(prev, dict) else 0.0
            except (ValueError, TypeError):
                prev_val = 0.0
            net_flow.append(current - prev_val)
    return net_flow


async def get_broad_market_sentiment() -> dict[str, object]:
    """获取大盘近期走势与情绪强弱（5分钟缓存）。
    分析上证、深证、创业板、科创50等主要指数的近期表现，
    计算综合情绪评分（0-100，强弱等级）。
    """
    now = time_module.time()
    if _sentiment_cache and (now - _sentiment_cache["ts"]) < 300:
        return _sentiment_cache["data"]
    results: dict[str, dict] = {}
    sentiment_scores: list[float] = []

    for symbol, name in MARKET_INDICES.items():
        try:
            api_symbol = tickdb_symbol(symbol)
            payload = await tickdb_get("/v1/market/kline", {"symbol": api_symbol, "interval": "1d", "limit": 20})
            bars = normalize_kline_bars(payload_data(payload), minimum=5)
            closes = [float(bar["close"]) for bar in bars]
            if len(closes) < 2:
                continue
            # 计算近20日涨跌幅
            change_20d = (closes[-1] / closes[-2] - 1) * 100
            # 计算近5日涨跌
            change_5d = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
            # RSI（近14日）
            rsi = calculate_rsi(closes)
            results[symbol] = {
                "name": name,
                "close": closes[-1],
                "change_20d": round(change_20d, 2),
                "change_5d": round(change_5d, 2),
                "rsi": rsi,
            }
            # 情绪评分：综合涨跌幅和RSI，标准化到0-100
            # 涨幅越大得分越高，RSI>50表示偏强
            sentiment_scores.append(change_20d)
        except Exception as e:
            print(f"[market_sentiment] {symbol} {name} 获取失败: {e}")
            continue

    if not sentiment_scores:
        return {"status": "error", "message": "无法获取大盘数据"}

    # 综合情绪：所有指数近20日涨跌幅平均值
    avg_change = sum(sentiment_scores) / len(sentiment_scores)
    # 转换为0-100情绪分（涨跌幅-10%~+10%映射到0-100）
    sentiment_score = max(0, min(100, (avg_change + 10) / 20 * 100))
    sentiment_level = "极弱" if sentiment_score < 30 else "偏弱" if sentiment_score < 45 else "中性" if sentiment_score < 60 else "偏强" if sentiment_score < 75 else "极强"

    data = {
        "status": "ok",
        "sentiment_score": round(sentiment_score, 1),
        "sentiment_level": sentiment_level,
        "indices": results,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _sentiment_cache["ts"] = time_module.time()
    _sentiment_cache["data"] = data
    return data


def payload_data(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload.get("data", payload)
    if isinstance(data, dict):
        data = data.get("data", data.get("klines", []))
    if not isinstance(data, list):
        raise ValueError("TickDB 返回数据格式不正确")
    return [item for item in data if isinstance(item, dict)]


def number(value: object) -> float | None:
    try:
        return None if value is None else float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def normalize_kline_bars(rows: list[dict[str, object]], minimum: int = 20) -> list[dict[str, float | str]]:
    bars: list[dict[str, float | str]] = []
    for row in rows:
        timestamp = number(row.get("time", row.get("timestamp")))
        values = {key: number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if timestamp is not None and all(value is not None for value in values.values()):
            date_format = "%Y-%m-%d %H:%M" if minimum == 1 else "%Y-%m-%d"
            date = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime(date_format)
            bars.append({"date": date, **{key: float(value) for key, value in values.items()}})
    if len(bars) < minimum:
        raise ValueError(f"TickDB 行情不足 {minimum} 条")
    return bars


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = sum(max(change, 0) for change in changes[-period:]) / period
    losses = sum(max(-change, 0) for change in changes[-period:]) / period
    return 100.0 if losses == 0 else round(100 - 100 / (1 + gains / losses), 2)


def calculate_atr(bars: list[dict[str, float | str]], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    ranges = [float(bar["high"]) - float(bar["low"]) for bar in bars[-period:]]
    return round(sum(ranges) / period, 2)


def calculate_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def calculate_macd(closes: list[float]) -> tuple[float | None, float | None]:
    if len(closes) < 35:
        return None, None
    fast = calculate_ema(closes, 12)
    slow = calculate_ema(closes, 26)
    macd = [fast[index] - slow[index] for index in range(len(closes))]
    signal = calculate_ema(macd, 9)
    return round(macd[-1], 4), round(signal[-1], 4)


async def evaluate(symbol: str) -> RiskResult:
    api_symbol = tickdb_symbol(symbol)
    daily_payload, minute_payload, flow_payload = await asyncio.gather(
        tickdb_get("/v1/market/kline", {"symbol": api_symbol, "interval": "1d", "limit": 80}),
        tickdb_get("/v1/market/kline", {"symbol": api_symbol, "interval": "1m", "limit": 240}),
        tickdb_get("/v1/market/capital-flow", {"symbol": tickdb_capital_flow_symbol(api_symbol), "type": "stock"}),
    )
    bars = normalize_kline_bars(payload_data(daily_payload))
    minute_bars = normalize_kline_bars(payload_data(minute_payload), minimum=1)
    net_flow = parse_capital_flow(flow_payload)
    current = bars[-1]
    closes = [float(bar["close"]) for bar in bars]
    rsi_value = calculate_rsi(closes)
    atr_value = calculate_atr(bars)
    previous_atr = calculate_atr(bars[:-14])
    macd_value, macd_signal = calculate_macd(closes)
    window = closes[-20:]
    average = sum(window) / len(window)
    deviation = math.sqrt(sum((value - average) ** 2 for value in window) / len(window))
    upper_band = round(average + 2 * deviation, 2)
    score = 0
    reasons: list[str] = []
    if rsi_value is not None and rsi_value > 70:
        score += 1
        reasons.append(f"RSI 超买 ({rsi_value:.2f})")
    elif rsi_value is not None and rsi_value < 30:
        score += 1
        reasons.append(f"RSI 超卖 ({rsi_value:.2f})，波动风险增加")
    if float(current["close"]) >= upper_band:
        score += 1
        reasons.append("价格突破布林带上轨")
    if atr_value is not None and previous_atr and atr_value > previous_atr * 1.5:
        score += 1
        reasons.append(f"ATR 异常放大 ({atr_value:.2f})")
    recent_high = max(closes[-10:])
    recent_volume = sum(float(bar["volume"]) for bar in bars[-5:]) / 5
    earlier_volume = sum(float(bar["volume"]) for bar in bars[-10:-5]) / 5
    if float(current["close"]) >= recent_high * 0.98 and recent_volume < earlier_volume * 0.8:
        score += 2
        reasons.append("价格接近近期高点但成交量萎缩，存在量价背离")
    if macd_value is not None and macd_signal is not None and macd_value < macd_signal and closes[-1] >= max(closes[-10:-1]):
        score += 2
        reasons.append("价格创近期新高但 MACD 未同步确认，存在顶背离")
    if not reasons:
        reasons.append("RSI、ATR 与布林带均未触发当前风控阈值")
    minute_prices = [float(bar["close"]) for bar in minute_bars]
    minute_volume = [float(bar["volume"]) for bar in minute_bars]

    # 加载市场情绪池截面数据，供因子 z-score 标准化使用
    sentiment = storage.load_market_sentiment()
    cross_section: dict[str, list[float]] | None = None
    if sentiment and sentiment.get("cross_section"):
        cross_section = {
            "i1": sentiment["cross_section"].get("close", []),
            "i2": sentiment["cross_section"].get("volume", []),
            "i3": [abs(r) for r in sentiment["cross_section"].get("return_20d", [])],
        }
    microstructure_factors = calculate_microstructure_factors(minute_prices, minute_volume, net_flow, cross_section=cross_section)
    verdict: Literal["SAFE", "WATCH", "DANGER", "ERROR"] = "DANGER" if score >= 3 else "WATCH" if score >= 1 else "SAFE"
    action = {"DANGER": "风险一票否决，建议暂停操作", "WATCH": "暂缓决策，等待更多确认", "SAFE": "当前指标未触发风险阈值"}[verdict]
    live_price = float(current["close"])
    live_change = round((live_price / float(bars[-2]["close"]) - 1) * 100, 2)
    return RiskResult(
        symbol=symbol,
        name=stock_name(symbol),
        verdict=verdict,
        score=score,
        action=action,
        reasons=reasons,
        indicators={"rsi": rsi_value, "atr": atr_value, "atr_baseline": previous_atr, "macd": macd_value, "macd_signal": macd_signal, "bollinger_upper": upper_band},
        quote={"price": live_price, "change_percent": live_change, "volume": float(current["volume"]), "source": "tickdb"},
        bars=bars,
        minute_bars=minute_bars,
        microstructure_factors=microstructure_factors,
        data_timestamp=str(current["date"]),
        calculation_timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk-api", "data_source": "tickdb"}


@app.post("/api/risk/check", response_model=RiskResult)
async def check_risk(request: RiskRequest) -> RiskResult:
    try:
        result = await evaluate(request.symbol)
        storage.save_risk(result.model_dump())
        return result
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"TickDB 暂不可用：{error}") from error


@app.get("/api/stocks/{symbol}/bars")
async def get_bars(symbol: str) -> dict[str, object]:
    if not symbol.isdigit() or len(symbol) != 6:
        raise HTTPException(status_code=400, detail="股票代码必须是 6 位数字")
    try:
        payload = await tickdb_get("/v1/market/kline", {"symbol": tickdb_symbol(symbol), "interval": "1d", "limit": 80})
        return {"symbol": symbol, "name": stock_name(symbol), "bars": normalize_kline_bars(payload_data(payload))}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"TickDB 暂不可用：{error}") from error


@app.get("/api/stocks/{symbol}/minutes")
async def get_minutes(symbol: str) -> dict[str, object]:
    if not symbol.isdigit() or len(symbol) != 6:
        raise HTTPException(status_code=400, detail="股票代码必须是 6 位数字")
    try:
        payload = await tickdb_get("/v1/market/kline", {"symbol": tickdb_symbol(symbol), "interval": "1m", "limit": 240})
        return {"symbol": symbol, "bars": normalize_kline_bars(payload_data(payload), minimum=1)}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"TickDB 暂不可用：{error}") from error


@app.get("/api/market/sentiment")
async def get_market_sentiment() -> dict[str, object]:
    """获取大盘近期走势与情绪强弱分析。"""
    return await get_broad_market_sentiment()


@app.post("/api/agent/chat")
async def agent_chat(request: AgentRequest) -> dict[str, object]:
    message = request.message.strip()
    command, _, argument = message.partition(" ")
    if command in ("/check", "检查"):
        symbol = argument.strip()
        if not symbol.isdigit() or len(symbol) != 6:
            raise HTTPException(status_code=400, detail="请提供 6 位股票代码，例如 /check 000712")
        result = await check_risk(RiskRequest(symbol=symbol))
        return {"type": "risk", "message": f"{symbol} 风险等级为 {result.verdict}，{result.action}", "result": result}
    if command in ("/batch", "批量"):
        symbols = [value.strip() for value in argument.split(",") if value.strip()]
        if not symbols or any(not value.isdigit() or len(value) != 6 for value in symbols):
            raise HTTPException(status_code=400, detail="请提供逗号分隔的 6 位股票代码")
        results = await asyncio.gather(*[check_risk(RiskRequest(symbol=value)) for value in symbols[:20]])
        return {"type": "batch", "message": f"已完成 {len(results)} 只股票的风险检查", "results": results}
    if message in ("/alerts", "预警"):
        return {"type": "alerts", "message": "以下是未处理预警", "alerts": storage.get_alerts(True)}
    if message in ("/watchlist", "自选股"):
        return {"type": "watchlist", "message": "当前自选股", "symbols": storage.get_watchlist()}
    if message in ("/review", "复盘"):
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {"type": "review", "message": f"{date} 已生成复盘数据", "checks": storage.list_risk(limit=100), "logs": storage.get_daily(date)}
    try:
        symbols = re.findall(r"(?<!\d)(\d{6})(?!\d)", message)
        # 获取大盘情绪作为上下文参考
        market_sentiment = await get_broad_market_sentiment()
        context: dict[str, object] = {
            "watchlist": storage.get_watchlist(),
            "recent_risks": storage.list_risk(limit=10),
            "market_sentiment": market_sentiment,
        }
        risk_result: RiskResult | None = None
        if symbols:
            risk_result = await check_risk(RiskRequest(symbol=symbols[0]))
            context["risk_result"] = risk_result.model_dump()
        session_id = request.session_id or "default"
        answer = await llm.chat(message, context, session_id=session_id)
        response: dict[str, object] = {"type": "llm", "message": answer}
        if risk_result:
            response["result"] = risk_result
        return response
    except Exception as error:
        return {"type": "help", "message": f"MiniMax 暂不可用：{error}。支持 /check 000712、/batch 000712,600036、/alerts、/watchlist、/review"}


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(request: AgentRequest) -> StreamingResponse:
    async def events():
        def emit(payload: dict[str, object]) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        message = request.message.strip()
        yield emit({"type": "status", "status": "正在解析请求"})
        try:
            if message.startswith("/"):
                yield emit({"type": "status", "status": "正在执行 Agent 命令"})
                result = await agent_chat(request)
                yield emit({"type": "delta", "content": str(result.get("message", ""))})
                yield emit({"type": "done", "result": result.get("result"), "results": result.get("results")})
                return
            symbols = re.findall(r"(?<!\d)(\d{6})(?!\d)", message)
            market_sentiment = await get_broad_market_sentiment()
            context: dict[str, object] = {"watchlist": storage.get_watchlist(), "recent_risks": storage.list_risk(limit=10), "market_sentiment": market_sentiment}
            risk_result: RiskResult | None = None
            if symbols:
                yield emit({"type": "status", "status": f"正在获取 {symbols[0]} 风险数据"})
                risk_result = await check_risk(RiskRequest(symbol=symbols[0]))
                context["risk_result"] = risk_result.model_dump()
            yield emit({"type": "status", "status": "正在请求 MiniMax"})
            session_id = request.session_id or "default"
            content = ""
            async for chunk in llm.chat_stream(message, context, session_id=session_id):
                content += chunk
                yield emit({"type": "delta", "content": chunk})
            yield emit({"type": "status", "status": "回答完成"})
            yield emit({"type": "done", "result": risk_result.model_dump() if risk_result else None})
        except Exception as error:
            yield emit({"type": "error", "message": str(error)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def run_watchlist_scan() -> dict[str, object]:
    results: list[RiskResult] = []
    alerts: list[dict[str, object]] = []
    for symbol in storage.get_watchlist():
        try:
            result = await evaluate(symbol)
            storage.save_risk(result.model_dump())
            results.append(result)
            if result.verdict == "DANGER":
                storage.add_alert(symbol, result.verdict, "; ".join(result.reasons))
                alerts.append({"symbol": symbol, "verdict": result.verdict, "reasons": result.reasons})
        except Exception as error:
            alerts.append({"symbol": symbol, "verdict": "ERROR", "reasons": [str(error)]})
    return {"scanned": len(results), "alerts": alerts, "results": results}


HIGH_BETA_TOP_N = 20  # 高标池股票数量


async def scan_high_beta_pool() -> dict[str, object]:
    """扫描全市场候选股票，按近20日涨幅选出Top20高标股，并计算截面因子分布。"""
    print(f"[high_beta] 开始扫描高标池，共 {len(storage.MARKET_SCAN_POOL)} 只候选股票")
    candidates: list[tuple[str, float, dict]] = []

    for symbol in storage.MARKET_SCAN_POOL:
        try:
            api_symbol = tickdb_symbol(symbol)
            payload = await tickdb_get("/v1/market/kline", {"symbol": api_symbol, "interval": "1d", "limit": 21})
            bars = normalize_kline_bars(payload_data(payload), minimum=5)
            closes = [float(bar["close"]) for bar in bars]
            if len(closes) < 2:
                continue
            ret = (closes[-1] / closes[-2] - 1) * 100 if closes[-2] != 0 else 0
            candidates.append((symbol, ret, {"close": closes[-1], "volume": float(bars[-1].get("volume", 0)), "change": ret}))
        except Exception as e:
            print(f"[high_beta] {symbol} 扫描失败: {e}")
            continue

    if not candidates:
        return {"status": "error", "message": "无可用候选股票"}

    # 按涨幅排序，取 Top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = candidates[:HIGH_BETA_TOP_N]
    pool = [{"symbol": sym, "return_20d": round(ret, 2), **extra} for sym, ret, extra in top]

    # 计算截面分布（用于因子z-score）
    all_closes = [c[2]["close"] for c in candidates]
    all_volumes = [c[2]["volume"] for c in candidates]
    all_returns = [c[1] for c in candidates]

    cross_section = {
        "close": all_closes,
        "volume": all_volumes,
        "return_20d": all_returns,
    }

    updated_at = datetime.now(timezone.utc).isoformat()
    storage.save_market_sentiment(pool, cross_section, updated_at)
    print(f"[high_beta] 高标池已更新: {len(pool)} 只, 时间 {updated_at}")
    return {"status": "ok", "pool_size": len(pool), "updated_at": updated_at}


async def execute_scheduled_job(job: dict[str, object]) -> dict[str, object]:
    if job.get("task") == "scan_watchlist":
        return await run_watchlist_scan()
    if job.get("task") == "daily_review":
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = f"请生成 {date} 的交易复盘。只使用系统记录，不估算收益或正确率。"
        report = await llm.chat(prompt, {"checks": storage.list_risk(limit=100), "logs": storage.get_daily(date)})
        storage.add_daily(date, report)
        return {"date": date, "report": report}
    if job.get("task") == "scan_high_beta":
        return await scan_high_beta_pool()
    raise ValueError(f"未知定时任务：{job.get('task')}")


@app.post("/api/monitor/scan")
async def scan_watchlist() -> dict[str, object]:
    return await run_watchlist_scan()


@app.post("/api/monitor/scan-high-beta")
async def scan_high_beta() -> dict[str, object]:
    return await scan_high_beta_pool()


@app.get("/api/sentiment/pool")
def get_sentiment_pool() -> dict[str, object]:
    data = storage.load_market_sentiment()
    if not data:
        return {"status": "empty", "pool": [], "updated_at": None}
    return {"status": "ok", **data}


@app.get("/api/cron/jobs")
def list_cron_jobs() -> list[dict[str, object]]:
    return job_scheduler.list()


@app.post("/api/cron/jobs")
async def create_cron_job(request: JobRequest) -> dict[str, object]:
    try:
        job = await job_scheduler.add(Job(**request.model_dump()))
        return job.as_dict()
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.patch("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, changes: dict[str, object]) -> dict[str, object]:
    try:
        return (await job_scheduler.update(job_id, changes)).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str) -> dict[str, str]:
    try:
        await job_scheduler.remove(job_id)
        return {"status": "deleted"}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error


@app.post("/api/cron/jobs/{job_id}/run")
async def run_cron_job(job_id: str) -> dict[str, object]:
    try:
        return await job_scheduler.run_now(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error


@app.get("/api/risk/history")
def risk_history(symbol: str | None = None) -> list[dict[str, object]]:
    return storage.list_risk(symbol)


@app.post("/api/decisions")
def save_decision(request: DecisionRequest) -> dict[str, str]:
    storage.add_decision(request.symbol, request.decision, request.note)
    return {"status": "saved"}


@app.get("/api/watchlist")
def watchlist() -> list[str]:
    return storage.get_watchlist()


@app.post("/api/watchlist/{symbol}")
def add_watchlist(symbol: str) -> dict[str, str]:
    if not symbol.isdigit() or len(symbol) != 6:
        raise HTTPException(status_code=400, detail="股票代码必须是 6 位数字")
    storage.add_watch(symbol)
    return {"status": "added", "symbol": symbol}


@app.delete("/api/watchlist/{symbol}")
def delete_watchlist(symbol: str) -> dict[str, str]:
    storage.remove_watch(symbol)
    return {"status": "removed", "symbol": symbol}


@app.get("/api/alerts")
def alerts(pending: bool = False) -> list[dict[str, object]]:
    return storage.get_alerts(pending)


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int) -> dict[str, str]:
    storage.resolve_alert(alert_id)
    return {"status": "resolved"}


@app.get("/api/daily/{log_date}")
def get_daily(log_date: str) -> list[dict[str, object]]:
    return storage.get_daily(log_date)


@app.post("/api/daily/{log_date}")
def write_daily(log_date: str, request: DailyLogRequest) -> dict[str, str]:
    storage.add_daily(log_date, request.content)
    return {"status": "saved"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
def static_files(path: str) -> FileResponse:
    file_path = WEB_DIR / path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(file_path)
