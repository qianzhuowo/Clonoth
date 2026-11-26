"""Persist token usage; price historical requests at the current model rates."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {}
for _models, _rates in (
    (("claude-opus-4-6", "claude-opus-4", "claude-opus"), (15.0, 75.0, 1.5, 18.75)),
    (("claude-sonnet-4-6", "claude-sonnet-4", "claude-3-7-sonnet", "claude-3-5-sonnet", "claude-sonnet"), (3.0, 15.0, 0.3, 3.75)),
    (("claude-haiku-4-5", "claude-3-5-haiku", "claude-haiku"), (0.8, 4.0, 0.08, 1.0)),
    (("gpt-5.4", "gpt-5", "gpt-6", "gpt"), (2.5, 10.0, 0.25, 2.5)),
    (("gpt-5-mini",), (0.25, 2.0, 0.025, 0.25)),
    (("gpt-4.1", "gpt-4o"), (2.0, 8.0, 0.5, 2.0)),
    (("gpt-4.1-mini", "gpt-4o-mini"), (0.4, 1.6, 0.1, 0.4)),
    (("o3", "o4-mini"), (1.1, 4.4, 0.275, 1.1)),
    (("gemini-3.1-pro", "gemini-3-pro", "gemini-2.5-pro", "gemini-pro"), (1.25, 10.0, 0.3125, 1.25)),
    (("gemini-3-flash", "gemini-2.5-flash", "gemini-flash", "gemini"), (0.3, 2.5, 0.075, 0.3)),
    (("deepseek-chat", "deepseek-v3", "deepseek-v4", "deepseek"), (0.27, 1.10, 0.07, 0.27)),
    (("deepseek-reasoner", "deepseek-r1"), (0.55, 2.19, 0.14, 0.55)),
):
    for _model in _models:
        DEFAULT_MODEL_PRICING[_model] = dict(zip(("input", "output", "cache_read", "cache_write"), _rates))

TOKEN_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens",
    "cache_write_prompt_tokens", "reasoning_tokens",
)
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")


class UsageStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS llm_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, session_id TEXT, task_id TEXT,
                    llm_request_id TEXT, node_id TEXT, is_system_task INTEGER DEFAULT 0,
                    provider TEXT, model TEXT,
                    prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0, cached_prompt_tokens INTEGER DEFAULT 0,
                    cache_write_prompt_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
                    elapsed_ms REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS usage_ts ON llm_usage_logs(ts);
                CREATE INDEX IF NOT EXISTS usage_model ON llm_usage_logs(model);
                CREATE TABLE IF NOT EXISTS model_pricing_overrides (
                    model TEXT PRIMARY KEY, input_price REAL NOT NULL,
                    output_price REAL NOT NULL, cache_read_price REAL NOT NULL,
                    cache_write_price REAL NOT NULL, updated_at REAL NOT NULL
                );
            """)
        self._backfill_from_conversations()

    def _backfill_from_conversations(self) -> None:
        conv_dir = self.path.parent / "conversations"
        if not conv_dir.is_dir():
            return
        with self._connect() as db:
            if db.execute("SELECT COUNT(*) FROM llm_usage_logs").fetchone()[0] > 0:
                return
        rows_to_insert = []
        for jf in sorted(conv_dir.glob("*.jsonl")):
            if jf.name.startswith("branch_"):
                continue
            session_id = jf.stem
            for line in jf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                meta = rec.get("meta") or {}
                usage = meta.get("usage") if isinstance(meta, dict) else None
                if rec.get("role") != "assistant" or not isinstance(usage, dict) or not usage.get("prompt_tokens"):
                    continue
                ts_str = meta.get("timestamp") or rec.get("created_at") or ""
                ts = datetime.fromisoformat(ts_str).timestamp() if ts_str else jf.stat().st_mtime
                node_id = str(rec.get("source_node_id") or "")
                provider = str(meta.get("provider") or "")
                model = str(meta.get("model") or provider or "unknown")
                rows_to_insert.append((
                    ts, session_id, str(rec.get("source_task_id") or ""),
                    str(meta.get("llm_request_id") or ""), node_id,
                    int(node_id.startswith("system.")), provider, model,
                    *(int(usage.get(k) or 0) for k in TOKEN_KEYS), 0.0,
                ))
        if rows_to_insert:
            cols = ("ts", "session_id", "task_id", "llm_request_id", "node_id",
                    "is_system_task", "provider", "model", *TOKEN_KEYS, "elapsed_ms")
            with self._connect() as db:
                db.executemany(
                    f"INSERT INTO llm_usage_logs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    rows_to_insert,
                )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, session_id: str, payload: dict[str, Any]) -> None:
        usage = payload["usage"]
        node_id = payload.get("node_id", "")
        columns = ("ts", "session_id", "task_id", "llm_request_id", "node_id",
                   "is_system_task", "provider", "model", *TOKEN_KEYS, "elapsed_ms")
        provider = payload.get("provider") or ""
        model = payload.get("model") or provider or "unknown"
        values = (
            payload.get("ts", time.time()), session_id, payload.get("task_id", ""),
            payload.get("llm_request_id", ""), node_id,
            int(payload.get("is_system_task", node_id.startswith("system."))),
            provider, model,
            *(usage.get(key, 0) for key in TOKEN_KEYS), payload.get("elapsed_ms", 0.0),
        )
        with self._connect() as db:
            db.execute(f"INSERT INTO llm_usage_logs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)

    def get_overrides(self) -> dict[str, dict[str, float]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM model_pricing_overrides ORDER BY model").fetchall()
        return {r["model"]: {key: r[f"{key}_price"] for key in PRICE_KEYS} for r in rows}

    def get_pricing(self) -> dict[str, Any]:
        overrides = self.get_overrides()
        return {"ok": True, "defaults": DEFAULT_MODEL_PRICING, "overrides": overrides,
                "effective": {**DEFAULT_MODEL_PRICING, **overrides}}

    @staticmethod
    def _validate_pricing(model: str, pricing: dict[str, float]) -> dict[str, float]:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(pricing, dict) or set(pricing) != set(PRICE_KEYS):
            raise ValueError("pricing must contain input, output, cache_read and cache_write")
        if any(type(pricing[k]) not in (int, float) or not math.isfinite(pricing[k]) or pricing[k] < 0 for k in PRICE_KEYS):
            raise ValueError("prices must be finite non-negative numbers")
        return {k: float(pricing[k]) for k in PRICE_KEYS}

    def set_pricing(self, model: str, pricing: dict[str, float] | None) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        rates = self._validate_pricing(model, pricing) if pricing is not None else None
        with self._connect() as db:
            if rates is None:
                db.execute("DELETE FROM model_pricing_overrides WHERE model = ?", (model,))
            else:
                db.execute("INSERT OR REPLACE INTO model_pricing_overrides VALUES (?, ?, ?, ?, ?, ?)",
                           (model, *(rates[k] for k in PRICE_KEYS), time.time()))

    def replace_overrides(self, overrides: dict[str, dict[str, float]]) -> None:
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        rows = []
        for model, pricing in overrides.items():
            rates = self._validate_pricing(model, pricing)
            rows.append((model, *(rates[k] for k in PRICE_KEYS), time.time()))
        with self._connect() as db:
            db.execute("DELETE FROM model_pricing_overrides")
            db.executemany("INSERT INTO model_pricing_overrides VALUES (?, ?, ?, ?, ?, ?)", rows)

    def resolve_pricing(self, model: str, overrides: dict | None = None) -> dict[str, Any]:
        if overrides is None:
            overrides = self.get_overrides()
        candidates = (model.lower(), model.rsplit("/", 1)[-1].lower())
        for table, is_override in ((overrides, True), (DEFAULT_MODEL_PRICING, False)):
            for exact in (True, False):
                for key in sorted(table, key=len, reverse=True):
                    if any(name == key.lower() if exact else name.startswith(key.lower()) for name in candidates):
                        return {**table[key], "is_override": is_override, "matched_key": key}
        return {**dict.fromkeys(PRICE_KEYS, 0.0), "is_override": False, "matched_key": ""}

    @staticmethod
    def _cost(row: dict, pricing: dict) -> float:
        uncached = max(0, row["prompt_tokens"] - row["cached_prompt_tokens"] - row["cache_write_prompt_tokens"])
        return (uncached * pricing["input"] + row["cached_prompt_tokens"] * pricing["cache_read"]
                + row["cache_write_prompt_tokens"] * pricing["cache_write"]
                + row["completion_tokens"] * pricing["output"]) / 1_000_000.0

    def get_stats(self, time_range: str = "today", tz_offset: int = 480) -> dict[str, Any]:
        if time_range not in ("today", "7d", "30d", "all"):
            raise ValueError("range must be today, 7d, 30d or all")
        tz = timezone(timedelta(minutes=tz_offset))
        now = datetime.now(tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days = {"today": 1, "7d": 7, "30d": 30}.get(time_range)
        start = midnight - timedelta(days=days - 1) if days else None
        with self._connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM llm_usage_logs WHERE ts >= ? AND ts <= ? ORDER BY ts DESC, id DESC",
                (start.timestamp() if start else 0, now.timestamp()))]
        overrides = self.get_overrides()
        pricing = {model: self.resolve_pricing(model, overrides) for model in {r["model"] for r in rows}}

        def aggregate(records):
            count = len(records)
            result = {"request_count": count, **{k: sum(r[k] for r in records) for k in TOKEN_KEYS}}
            elapsed = sum(r["elapsed_ms"] for r in records)
            result.update(
                cache_hit_rate=result["cached_prompt_tokens"] / result["prompt_tokens"] if result["prompt_tokens"] else 0.0,
                avg_elapsed_ms=elapsed / count if count else 0.0,
                avg_tps=result["completion_tokens"] * 1000 / elapsed if elapsed else 0.0,
                cost_usd=sum(r["cost_usd"] for r in records),
            )
            return result

        def grouped(keys):
            groups = {}
            for row in rows:
                key = tuple(row[k] for k in keys)
                groups.setdefault(key, []).append(row)
            out = [{**dict(zip(keys, key)), **aggregate(records)} for key, records in groups.items()]
            out.sort(key=lambda g: (g["cost_usd"], g["total_tokens"]), reverse=True)
            return out

        buckets = {}
        if time_range == "today":
            buckets = {f"{hour:02d}:00": [] for hour in range(24)}
        elif days:
            buckets = {(start + timedelta(days=i)).strftime("%Y-%m-%d"): [] for i in range(days)}
        for row in rows:
            created = datetime.fromtimestamp(row["ts"], tz)
            row["created_at"] = created.isoformat()
            row["cost_usd"] = self._cost(row, pricing[row["model"]])
            row["tps"] = row["completion_tokens"] * 1000 / row["elapsed_ms"] if row["elapsed_ms"] else 0.0
            bucket = created.strftime("%H:00" if time_range == "today" else "%Y-%m-%d")
            buckets.setdefault(bucket, []).append(row)
        summary = aggregate(rows)
        summary["total_cost_usd"] = summary.pop("cost_usd")
        by_model = grouped(("model", "provider"))
        for group in by_model:
            group["pricing"] = pricing[group["model"]]
        timeseries_keys = ("request_count", "prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                           "cache_write_prompt_tokens", "reasoning_tokens", "cost_usd")
        timeseries = []
        for bucket, records in sorted(buckets.items()):
            totals = aggregate(records)
            timeseries.append({"bucket": bucket, **{k: totals[k] for k in timeseries_keys}})
        by_provider = grouped(("provider",))
        by_node = grouped(("node_id",))
        for group in by_provider + by_node:
            del group["avg_elapsed_ms"], group["avg_tps"]
        for group in by_node:
            del group["cache_write_prompt_tokens"]
        recent = [{k: v for k, v in row.items() if k != "llm_request_id"} for row in rows[:50]]
        return {"ok": True, "range": time_range, "summary": summary, "timeseries": timeseries,
                "by_model": by_model, "by_provider": by_provider,
                "by_node": by_node, "recent": recent}
