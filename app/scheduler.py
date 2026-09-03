from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

from croniter import croniter

logger = logging.getLogger(__name__)

ScheduleKind = Literal["cron", "every", "at"]
SessionTarget = Literal["main", "isolated"]
DeliveryMode = Literal["announce", "webhook", "none"]
Executor = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


@dataclass
class Job:
    id: str
    name: str
    task: str
    schedule_kind: ScheduleKind
    schedule_value: str
    session_target: SessionTarget = "isolated"
    delivery: DeliveryMode = "none"
    enabled: bool = True
    timezone_name: str = "UTC"
    timeout_seconds: int = 3600
    retry_limit: int = 2
    retry_count: int = 0
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_status: str = "pending"
    last_error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def load(self) -> list[Job]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Job(**item) for item in data if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError, TypeError) as error:
            logger.error("Unable to load cron jobs: %s", error)
            return []

    async def save(self, jobs: list[Job]) -> None:
        async with self._lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps([job.as_dict() for job in jobs], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


class Scheduler:
    def __init__(self, store: JobStore, executor: Executor):
        self.store = store
        self.executor = executor
        self.jobs: dict[str, Job] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.jobs = {job.id: job for job in await self.store.load()}
        now = datetime.now(timezone.utc)
        changed = False
        for job in self.jobs.values():
            if job.enabled and (not job.next_run_at or self._parse_time(job.next_run_at) <= now):
                job.next_run_at = self.next_run(job, now).isoformat()
                changed = True
        if changed:
            await self.persist()
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="ming-assistant-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def persist(self) -> None:
        await self.store.save(list(self.jobs.values()))

    def list(self) -> list[dict[str, object]]:
        return [job.as_dict() for job in sorted(self.jobs.values(), key=lambda item: item.created_at)]

    async def add(self, job: Job) -> Job:
        if job.id in self.jobs:
            raise ValueError("任务 ID 已存在")
        job.next_run_at = self.next_run(job, datetime.now(timezone.utc)).isoformat() if job.enabled else None
        self.jobs[job.id] = job
        await self.persist()
        return job

    async def update(self, job_id: str, changes: dict[str, object]) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        for key, value in changes.items():
            if key in {"name", "task", "schedule_kind", "schedule_value", "session_target", "delivery", "enabled", "timezone_name", "timeout_seconds", "retry_limit"}:
                setattr(job, key, value)
        job.next_run_at = self.next_run(job, datetime.now(timezone.utc)).isoformat() if job.enabled else None
        await self.persist()
        return job

    async def remove(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        del self.jobs[job_id]
        await self.persist()

    async def run_now(self, job_id: str) -> dict[str, object]:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return await self._execute(job)

    def next_run(self, job: Job, now: datetime) -> datetime:
        if job.schedule_kind == "at":
            target = datetime.fromtimestamp(float(job.schedule_value), tz=timezone.utc) if job.schedule_value.isdigit() else datetime.fromisoformat(job.schedule_value.replace("Z", "+00:00"))
            return target
        if job.schedule_kind == "every":
            seconds = max(1, int(job.schedule_value))
            anchor = datetime.fromtimestamp(int(job.id[:8], 16) % seconds, tz=timezone.utc)
            elapsed = max(0, int((now - anchor).total_seconds()))
            return anchor + timedelta(seconds=(elapsed // seconds + 1) * seconds)
        base = croniter(job.schedule_value, now).get_next(datetime)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return base.astimezone(timezone.utc) + timedelta(seconds=self._jitter(job.id))

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            due = [job for job in self.jobs.values() if job.enabled and job.next_run_at and self._parse_time(job.next_run_at) <= now]
            for job in due:
                await self._execute(job)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _execute(self, job: Job) -> dict[str, object]:
        started = datetime.now(timezone.utc)
        job.last_run_at = started.isoformat()
        try:
            result = await asyncio.wait_for(self.executor(job.as_dict()), timeout=job.timeout_seconds)
            job.last_status = "success"
            job.last_error = None
            job.retry_count = 0
            output = {"status": "success", "job_id": job.id, "result": result}
        except Exception as error:
            job.last_status = "failed"
            job.last_error = str(error)
            job.retry_count += 1
            output = {"status": "failed", "job_id": job.id, "error": str(error)}
            if job.retry_count <= job.retry_limit:
                job.next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=min(300, 30 * (2 ** (job.retry_count - 1))))).isoformat()
                await self.persist()
                return output
        if job.schedule_kind == "at":
            job.enabled = False
            job.next_run_at = None
        else:
            job.next_run_at = self.next_run(job, datetime.now(timezone.utc)).isoformat() if job.enabled else None
        await self.persist()
        return output

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _jitter(job_id: str) -> int:
        return int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % 300
