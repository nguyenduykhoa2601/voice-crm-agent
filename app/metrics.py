"""Lightweight metrics: append-only JSONL events plus an aggregate summary.

Event kinds written to the JSONL file (one JSON object per line):
- session_start  {session_id, model}
- turn           {session_id, first_audio_ms, turn_ms, tool_ms, source}
- tool           {session_id, name, ms, ok}
- outcome        {session_id, completed, saves, corrections, interventions, turns, tool_errors}
- session_end    {session_id, duration_s, reason}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear-interpolated percentile (pct in 0..100). None for empty input."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(data) - 1)
    frac = rank - lo
    return data[lo] + (data[hi] - data[lo]) * frac


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class MetricsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"ts": round(time.time(), 3), "kind": kind, **fields}
        line = json.dumps(event, default=str, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:  # metrics must never break a session
            log.warning("metrics write failed: %s", exc)
        return event

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def summary(self) -> dict[str, Any]:
        return summarize(self.events())


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [e for e in events if e.get("kind") == "turn"]
    tools = [e for e in events if e.get("kind") == "tool"]
    outcomes = [e for e in events if e.get("kind") == "outcome"]
    sessions = [e for e in events if e.get("kind") == "session_start"]

    first_audio = [e["first_audio_ms"] for e in turns if e.get("first_audio_ms") is not None]
    turn_ms = [e["turn_ms"] for e in turns if e.get("turn_ms") is not None]

    per_tool: dict[str, dict[str, Any]] = {}
    for name in sorted({str(e.get("name")) for e in tools}):
        rows = [e for e in tools if str(e.get("name")) == name]
        ms = [e["ms"] for e in rows if e.get("ms") is not None]
        per_tool[name] = {
            "calls": len(rows),
            "ok_rate": _round(100.0 * sum(1 for e in rows if e.get("ok")) / len(rows)),
            "p50_ms": _round(percentile(ms, 50)),
            "p90_ms": _round(percentile(ms, 90)),
            "p95_ms": _round(percentile(ms, 95)),
        }

    # Only sessions that attempted something count toward completion rate.
    attempted = [o for o in outcomes if o.get("turns", 0) > 0 or o.get("saves", 0) > 0]
    completed = [o for o in attempted if o.get("completed")]

    return {
        "sessions": len(sessions),
        "turns": len(turns),
        "first_audio_ms": {
            "p50": _round(percentile(first_audio, 50)),
            "p90": _round(percentile(first_audio, 90)),
            "p95": _round(percentile(first_audio, 95)),
            "n": len(first_audio),
        },
        "turn_ms": {
            "p50": _round(percentile(turn_ms, 50)),
            "p90": _round(percentile(turn_ms, 90)),
            "p95": _round(percentile(turn_ms, 95)),
            "n": len(turn_ms),
        },
        "tools": per_tool,
        "task_completion": {
            "attempted": len(attempted),
            "completed": len(completed),
            "rate": _round(100.0 * len(completed) / len(attempted)) if attempted else None,
        },
        "corrections_per_session": _round(
            sum(o.get("corrections", 0) for o in attempted) / len(attempted)
        )
        if attempted
        else None,
        "interventions_total": sum(o.get("interventions", 0) for o in attempted),
        "interventions_per_session": _round(
            sum(o.get("interventions", 0) for o in attempted) / len(attempted)
        )
        if attempted
        else None,
    }


_store: MetricsStore | None = None


def get_store() -> MetricsStore:
    global _store
    if _store is None:
        from app.config import settings

        _store = MetricsStore(settings.metrics_path)
    return _store
