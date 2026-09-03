# MING Risk Agent

A 股股票风险辅助分析系统。输入 6 位股票代码后，通过 TickDB 获取最近 80 个交易日的日线 + 当日分钟 K 线，由 MiniMax-M2.7 辅助解释风险结果，并可通过 Agent 工作台用自然语言交互。

## 本地运行

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000>，输入股票代码并运行检查。

## 项目结构

```
app/
  main.py           FastAPI API 与风险计算引擎
  llm.py            LLM 对话层（分层记忆 + 多轮会话）
  memory.py         记忆管理系统（向量检索 + 分层记忆 + 自动摘要）
  session.py        多轮对话会话管理（SQLite + LRU 缓存）
  storage.py        SQLite 持久化（风险记录、预警、决策）
  scheduler.py      定时任务调度器
  microstructure.py 十个微观结构因子计算
web/
  index.html        前端页面
  app.js            页面交互与图表
  styles.css        页面样式
Ming_Assistant/
  memory/           Agent 记忆文件（SOUL.md、MEMORY.md、SCRATCHPAD.md、daily/）
  prompts/          Agent 命令提示词（check.md、review.md）
  skills/           Agent 技能定义（risk_interpreter/SKILL.md）
  cron/jobs.json    定时任务持久化
data/
  agent.db          SQLite 数据库
  memory.db         记忆向量数据库
  sessions.db       会话历史数据库
  market_sentiment.json  高标池截面数据
.env                API 密钥配置
requirements.txt    Python 依赖
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/risk/check` | 单只股票风险检查（body: `{symbol}`） |
| `GET` | `/api/stocks/{symbol}/bars` | 获取日线 K 线 |
| `GET` | `/api/stocks/{symbol}/minutes` | 获取当日分钟 K 线 |
| `POST` | `/api/agent/chat` | Agent 对话（支持 session_id） |
| `POST` | `/api/agent/chat/stream` | Agent 流式对话 |
| `GET/POST` | `/api/watchlist` | 自选股管理 |
| `GET/POST` | `/api/alerts` | 预警查看与处理 |
| `POST` | `/api/decisions` | 记录交易决策 |
| `GET/POST` | `/api/daily/{date}` | 每日复盘日志 |
| `POST` | `/api/monitor/scan` | 扫描自选股风险 |
| `POST` | `/api/monitor/scan-high-beta` | 扫描高标池 Top20 |
| `GET` | `/api/sentiment/pool` | 查看当前高标池 |
| `GET` | `/api/market/sentiment` | 大盘情绪分析 |
| `GET/PATCH/DELETE` | `/api/cron/jobs/{id}` | 定时任务 CRUD |
| `POST` | `/api/cron/jobs/{id}/run` | 手动触发定时任务 |
| `GET` | `/api/health` | 健康检查 |

## Agent 命令

| 命令 | 说明 |
|------|------|
| `/check 000712` | 检查单只股票风险 |
| `/batch 000712,600036` | 批量检查（最多 20 只） |
| `/alerts` | 查看未处理预警 |
| `/watchlist` | 查看自选股 |
| `/review` | 生成当日复盘 |
| 自然语言 | 大盘情绪参考 + 风险数据 + 记忆检索 |

## 核心概念

### 风险评分逻辑

每次风险检查触发：日线（80 条）+ 分钟线（240 条）+ 资金流向，三者并发请求。

| 触发条件 | 风险分数 |
|---------|---------|
| RSI > 70 或 < 30 | +1 |
| 价格突破布林带上轨 | +1 |
| ATR 异常放大（> 1.5× 基准） | +1 |
| 价格接近高点但成交量萎缩 | +2 |
| 价格创新高但 MACD 未确认 | +2 |

- `score ≥ 3` → **DANGER**（一票否决）
- `score ≥ 1` → **WATCH**（暂缓决策）
- `score = 0` → **SAFE**（未触发阈值）

### 微观结构因子

10 个日内因子（需要分钟 K 线）：Factor 1-10，详见 `Ming_Assistant/skills/risk_interpreter/SKILL.md`。其中 Factor 2（残差历史）和 Factor 6/9 的 z-score 部分依赖高标池截面数据。

### 记忆系统（分层）

```
System Prompt 记忆区：
  1. SOUL.md（人格规则，始终）
  2. skills/risk_interpreter/SKILL.md（始终）
  3. 命令专用提示（如 /check）
  4. 近期记忆（近 7 天，最新 10 条）
  5. 相关记忆（语义向量检索 top-5，90 天内）
  6. 历史摘要（90 天内日级别摘要）
  7. 早期记忆（90 天前有 embedding 的条目）
  8. 待处理预警（SCRATCHPAD.md）
```

- **向量检索**：每条记忆写入时调用 MiniMax embedding 接口，存储归一化向量，检索时做余弦相似度排序
- **自动摘要**：同一天记忆 ≥3 条时，自动调用 LLM 生成摘要并删除原始条目的 embedding
- **会话历史**：通过 `session_id` 支持多轮对话，最多保留 20 轮（SQLite + LRU 缓存）

### 高标池

每日盘后扫描候选股票池（80 只，覆盖主要板块），按近 20 日涨幅选出 Top 20，存入 `data/market_sentiment.json`，供因子截面标准化使用。

### 定时任务

支持 `cron`（Cron 表达式）、`every`（固定秒数）、`at`（一次性）三种调度，带超时、指数退避重试和一次性任务自动停用。

## 环境变量

```
TICKDB_API_KEY=         TickDB API 密钥
MINIMAX_API_KEY=        MiniMax API 密钥
MINIMAX_MODEL=          模型名称（默认 MiniMax-M2.7）
MINIMAX_API_URL=        MiniMax API 地址
USE_MOCK_DATA=          true/false，是否使用模拟数据（默认 false）
```

## 当前边界

- 部分股票代码在 TickDB 免费计划中受限（如 000001.SZ），受限时换用其他代码或升级套餐
- `factor_2`（残差历史）和 `factor_6`/`factor_9` 的 z-score 标准化依赖高标池截面数据，单只股票无法计算时显示为 `None`
- Agent 自然语言对话每次会请求 6 个大盘指数数据，已做 5 分钟缓存
- 批量检查（`/batch`）完全串行，20 只股票约需 60 次顺序请求，注意 TickDB 配额消耗
