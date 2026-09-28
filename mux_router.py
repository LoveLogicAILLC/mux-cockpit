"""
mux_router.py
MUX layer: host-driven multiplexing for a single Mac Mini M4.

3 channels: INPUT (0), CONTEXT (1), TOOL (2)
- Per-worker, per-channel queues with strict priority TOOL > INPUT > CONTEXT
- Backpressure: when a worker is full, tasks SPILL to disk (never dropped)
- Park: flushes a worker's queue to ./memory/parked/<id>.jsonl and stops dispatch
- Unpark: reloads spilled + parked tasks in original order
- Token bucket: per-worker budget that refills continuously (tokens/hour)
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional


class Channel(IntEnum):
    INPUT = 0    # user goals, research
    CONTEXT = 1  # memory, RAG, review
    TOOL = 2     # implementation / tool work


# Dequeue priority (host-driven): tool work unblocks the most, context review the least.
PRIORITY_ORDER = (Channel.TOOL, Channel.INPUT, Channel.CONTEXT)

WORKER_IDS = ("planner", "builder", "critic", "security", "devops", "researcher", "xr")


@dataclass
class Task:
    id: str
    goal: str
    priority: str = "high"  # critical, high, medium, low
    channel_hint: Channel = Channel.INPUT
    created: float = field(default_factory=time.time)
    retries: int = 0
    parent: Optional[str] = None
    action: str = "research"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["channel_hint"] = int(self.channel_hint)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["channel_hint"] = Channel(int(d.get("channel_hint", 0)))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class TokenBucket:
    """Continuous-refill token bucket. capacity tokens, refilled at capacity/hour."""

    def __init__(self, capacity: int, per_seconds: float = 3600.0):
        self.capacity = capacity
        self.rate = capacity / per_seconds
        self._tokens = float(capacity)
        self._t = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.rate)
        self._t = now

    @property
    def available(self) -> int:
        self._refill()
        return int(self._tokens)

    def consume(self, n: int) -> None:
        self._refill()
        self._tokens = max(0.0, self._tokens - n)


class MuxWorkerQueue:
    def __init__(self, worker_id: str, max_depth: int = 32):
        self.worker_id = worker_id
        self.max_depth = max_depth
        self.queues: Dict[Channel, asyncio.Queue] = {ch: asyncio.Queue() for ch in Channel}
        self._ready = asyncio.Event()
        self.parked = False

    def depth(self) -> int:
        return sum(q.qsize() for q in self.queues.values())

    def channel_depth(self, ch: Channel) -> int:
        return self.queues[ch].qsize()

    def full(self) -> bool:
        return self.depth() >= self.max_depth

    def enqueue_nowait(self, task: Task, channel: Channel) -> None:
        self.queues[channel].put_nowait(task)
        self._ready.set()

    async def dequeue(self) -> Task:
        """Strict priority dequeue. Waits on a single event (no leaked getters)."""
        while True:
            if not self.parked:
                for ch in PRIORITY_ORDER:
                    q = self.queues[ch]
                    if not q.empty():
                        return q.get_nowait()
            self._ready.clear()
            await self._ready.wait()

    def wake(self) -> None:
        self._ready.set()

    def drain(self) -> List[tuple]:
        """Remove everything (for park). Returns [(channel, task)] in priority order."""
        out = []
        for ch in PRIORITY_ORDER:
            q = self.queues[ch]
            while not q.empty():
                out.append((ch, q.get_nowait()))
        return out


class MuxRouter:
    """
    Unified-memory-aware routing:
    - Routes based on channel + priority, falls back to least-loaded worker
    - Park mechanism spills to NVMe to cap RAM pressure
    """

    def __init__(self, root: str | Path = "./memory", max_depth: int = 32,
                 tokens_per_hour: int = 8000, min_tokens: int = 500):
        self.root = Path(root)
        self.parked_dir = self.root / "parked"
        self.parked_dir.mkdir(parents=True, exist_ok=True)
        self.min_tokens = min_tokens
        self.workers: Dict[str, MuxWorkerQueue] = {
            wid: MuxWorkerQueue(wid, max_depth) for wid in WORKER_IDS
        }
        self.token_bucket: Dict[str, TokenBucket] = {
            wid: TokenBucket(tokens_per_hour) for wid in WORKER_IDS
        }
        self.routed = 0
        self.spilled = 0

    # ---------------- policy ----------------
    def select_worker(self, channel: Channel, priority: str) -> str:
        if channel == Channel.INPUT:
            preferred = "planner" if priority in ("critical", "high") else "researcher"
        elif channel == Channel.TOOL:
            preferred = "security" if priority == "critical" else "builder"
        else:
            preferred = "critic"
        wq = self.workers[preferred]
        if not wq.parked and not wq.full():
            return preferred
        # Load-aware fallback among unparked workers
        live = [w for w in self.workers.values() if not w.parked]
        return min(live, key=lambda w: w.depth()).worker_id if live else preferred

    # ---------------- routing ----------------
    async def route(self, task: Task | dict, worker_id: str, channel: Channel) -> str:
        """Returns 'queued' | 'spilled'. Never drops a task."""
        if isinstance(task, dict):
            task = Task(
                id=str(task.get("id", "t")),
                goal=str(task.get("desc") or task.get("goal") or task),
                action=str(task.get("action", "research")),
                parent=task.get("parent"),
                priority=str(task.get("priority", "high")),
            )
        task.channel_hint = channel

        wq = self.workers.get(worker_id)
        if wq is None:
            raise ValueError(f"Unknown worker {worker_id}")

        if wq.parked or wq.full() or self.token_bucket[worker_id].available < self.min_tokens:
            self._spill(worker_id, channel, task)
            self.spilled += 1
            return "spilled"

        wq.enqueue_nowait(task, channel)
        self.routed += 1
        return "queued"

    def _spill(self, worker_id: str, channel: Channel, task: Task) -> None:
        with open(self.parked_dir / f"{worker_id}.jsonl", "a") as f:
            f.write(json.dumps({"channel": int(channel), "task": task.to_dict()}) + "\n")

    async def park(self, worker_id: str) -> int:
        """Flush queue to disk and stop dispatch. Returns #tasks flushed."""
        wq = self.workers.get(worker_id)
        if wq is None:
            raise ValueError(f"Unknown worker {worker_id}")
        wq.parked = True
        items = wq.drain()
        for ch, t in items:
            self._spill(worker_id, ch, t)
        return len(items)

    async def unpark(self, worker_id: str) -> int:
        """Reload spilled tasks (up to max_depth) and resume dispatch."""
        wq = self.workers.get(worker_id)
        if wq is None:
            raise ValueError(f"Unknown worker {worker_id}")
        path = self.parked_dir / f"{worker_id}.jsonl"
        restored = 0
        if path.exists():
            lines = path.read_text().splitlines()
            keep = []
            for line in lines:
                if not line.strip():
                    continue
                if wq.depth() < wq.max_depth:
                    try:
                        rec = json.loads(line)
                        wq.enqueue_nowait(Task.from_dict(rec["task"]), Channel(rec["channel"]))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        # A single truncated/corrupted line (e.g. a write cut short by a crash
                        # mid-append) must not poison the whole restore -- that would leave
                        # every OTHER valid spilled task stuck and the worker permanently
                        # parked. Drop just this line, keep going.
                        continue
                    restored += 1
                else:
                    keep.append(line)
            path.write_text("\n".join(keep) + ("\n" if keep else ""))
        wq.parked = False
        wq.wake()
        return restored

    def spilled_count(self, worker_id: str) -> int:
        p = self.parked_dir / f"{worker_id}.jsonl"
        return sum(1 for l in p.read_text().splitlines() if l.strip()) if p.exists() else 0

    # ---------------- telemetry ----------------
    def channel_stats(self) -> List[dict]:
        primary = {Channel.INPUT: "planner", Channel.CONTEXT: "critic", Channel.TOOL: "builder"}
        out = []
        for ch in Channel:
            depth = sum(w.channel_depth(ch) for w in self.workers.values())
            pw = self.workers[primary[ch]]
            if pw.parked:
                state = "parked"
            elif pw.depth() >= int(pw.max_depth * 0.75):
                state = "backpressure"
            elif depth:
                state = "active"
            else:
                state = "idle"
            out.append({"channel": ch.name, "queue": depth, "worker": primary[ch], "state": state})
        return out

    def total_depth(self) -> int:
        return sum(w.depth() for w in self.workers.values())
