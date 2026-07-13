from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USAGE_QUOTA_DB_PATH = ROOT / "data" / "relaydeck" / "usage.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_uuid() -> str:
    return str(uuid.uuid4())


def _normalize_record(record: Any) -> dict[str, Any]:
    if is_dataclass(record):
        return asdict(record)
    if isinstance(record, Mapping):
        return dict(record)
    raise TypeError(f"unsupported record type: {type(record)!r}")


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class UsageEventRecord:
    source: str
    request_id: str | None = None
    public_model_name: str | None = None
    litellm_model_name: str | None = None
    upstream_model: str | None = None
    relay_label: str | None = None
    api_base_hash: str | None = None
    custom_llm_provider: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None
    actual_cost: float | None = None
    currency: str = "USD"
    latency_ms: int | None = None
    status: str = "success"
    error_code: str | None = None
    id: str = field(default_factory=new_uuid)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class ProviderBalanceSnapshotRecord:
    provider_key: str
    adapter: str
    status: str
    relay_label: str | None = None
    api_key_env: str | None = None
    currency: str | None = None
    balance_total: float | None = None
    balance_used: float | None = None
    balance_remaining: float | None = None
    period_start: str | None = None
    period_end: str | None = None
    raw_summary: str | dict[str, Any] | list[Any] | None = None
    id: str = field(default_factory=new_uuid)
    checked_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class RechargeRecord:
    provider_key: str
    paid_amount_cny: float
    paid_at: str = field(default_factory=utc_now_iso)
    relay_label: str | None = None
    api_key_env: str | None = None
    credited_amount: float | None = None
    credited_currency: str | None = None
    bonus_amount: float | None = None
    bonus_currency: str | None = None
    balance_after_recharge: float | None = None
    note: str | None = None
    id: str = field(default_factory=new_uuid)
    created_at: str = field(default_factory=utc_now_iso)


class UsageQuotaStore:
    def __init__(self, db_path: str | Path = DEFAULT_USAGE_QUOTA_DB_PATH) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> Path:
        with self.connect() as conn:
            conn.executescript(_SCHEMA_SQL)
        return self.db_path

    def insert_usage_event(self, record: UsageEventRecord | Mapping[str, Any]) -> dict[str, Any]:
        row = _normalize_record(record)
        columns = [
            "id",
            "created_at",
            "source",
            "request_id",
            "public_model_name",
            "litellm_model_name",
            "upstream_model",
            "relay_label",
            "api_base_hash",
            "custom_llm_provider",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
            "actual_cost",
            "currency",
            "latency_ms",
            "status",
            "error_code",
        ]
        payload = {key: row.get(key) for key in columns}
        payload["id"] = payload["id"] or new_uuid()
        payload["created_at"] = payload["created_at"] or utc_now_iso()
        payload["currency"] = payload["currency"] or "USD"
        payload["status"] = payload["status"] or "success"
        payload["prompt_tokens"] = int(payload["prompt_tokens"] or 0)
        payload["completion_tokens"] = int(payload["completion_tokens"] or 0)
        payload["total_tokens"] = int(payload["total_tokens"] or 0)
        if not payload["source"]:
            raise ValueError("usage_events.source is required")

        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO usage_events ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [payload[column] for column in columns],
            )
        return payload

    def get_usage_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM usage_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_usage_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        source: str | None = None,
        status: str | None = None,
        relay_label: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM usage_events"
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if relay_label:
            clauses.append("relay_label = ?")
            params.append(relay_label)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(limit, 1), max(offset, 0)])
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def insert_provider_balance_snapshot(
        self,
        record: ProviderBalanceSnapshotRecord | Mapping[str, Any],
    ) -> dict[str, Any]:
        row = _normalize_record(record)
        columns = [
            "id",
            "checked_at",
            "provider_key",
            "relay_label",
            "api_key_env",
            "adapter",
            "status",
            "currency",
            "balance_total",
            "balance_used",
            "balance_remaining",
            "period_start",
            "period_end",
            "raw_summary",
        ]
        payload = {key: row.get(key) for key in columns}
        payload["id"] = payload["id"] or new_uuid()
        payload["checked_at"] = payload["checked_at"] or utc_now_iso()
        payload["raw_summary"] = _json_text(payload["raw_summary"])
        if not payload["provider_key"]:
            raise ValueError("provider_balance_snapshots.provider_key is required")
        if not payload["adapter"]:
            raise ValueError("provider_balance_snapshots.adapter is required")
        if not payload["status"]:
            raise ValueError("provider_balance_snapshots.status is required")

        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO provider_balance_snapshots ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [payload[column] for column in columns],
            )
        return payload

    def get_provider_balance_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_balance_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_provider_balance_snapshot(
        self,
        provider_key: str,
        *,
        api_key_env: str | None = None,
    ) -> dict[str, Any] | None:
        query = """
            SELECT * FROM provider_balance_snapshots
            WHERE provider_key = ?
        """
        params: list[Any] = [provider_key]
        if api_key_env:
            query += " AND api_key_env = ?"
            params.append(api_key_env)
        query += " ORDER BY checked_at DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def list_provider_balance_snapshots(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        provider_key: str | None = None,
        relay_label: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM provider_balance_snapshots"
        clauses: list[str] = []
        params: list[Any] = []
        if provider_key:
            clauses.append("provider_key = ?")
            params.append(provider_key)
        if relay_label:
            clauses.append("relay_label = ?")
            params.append(relay_label)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY checked_at DESC LIMIT ? OFFSET ?"
        params.extend([max(limit, 1), max(offset, 0)])
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def insert_recharge_record(self, record: RechargeRecord | Mapping[str, Any]) -> dict[str, Any]:
        row = _normalize_record(record)
        columns = [
            "id",
            "created_at",
            "paid_at",
            "provider_key",
            "relay_label",
            "api_key_env",
            "paid_amount_cny",
            "credited_amount",
            "credited_currency",
            "bonus_amount",
            "bonus_currency",
            "balance_after_recharge",
            "note",
        ]
        payload = {key: row.get(key) for key in columns}
        payload["id"] = payload["id"] or new_uuid()
        payload["created_at"] = payload["created_at"] or utc_now_iso()
        payload["paid_at"] = payload["paid_at"] or utc_now_iso()
        if not payload["provider_key"]:
            raise ValueError("recharge_records.provider_key is required")
        if payload["paid_amount_cny"] is None:
            raise ValueError("recharge_records.paid_amount_cny is required")

        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO recharge_records ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [payload[column] for column in columns],
            )
        return payload

    def get_recharge_record(self, recharge_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recharge_records WHERE id = ?",
                (recharge_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_recharge_records(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        provider_key: str | None = None,
        relay_label: str | None = None,
        api_key_env: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM recharge_records"
        clauses: list[str] = []
        params: list[Any] = []
        if provider_key:
            clauses.append("provider_key = ?")
            params.append(provider_key)
        if relay_label:
            clauses.append("relay_label = ?")
            params.append(relay_label)
        if api_key_env:
            clauses.append("api_key_env = ?")
            params.append(api_key_env)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY paid_at DESC, created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(limit, 1), max(offset, 0)])
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


@lru_cache(maxsize=None)
def get_usage_quota_store(db_path: str | Path = DEFAULT_USAGE_QUOTA_DB_PATH) -> UsageQuotaStore:
    store = UsageQuotaStore(db_path)
    store.initialize()
    return store


def init_usage_quota_store(db_path: str | Path = DEFAULT_USAGE_QUOTA_DB_PATH) -> Path:
    return get_usage_quota_store(db_path).db_path


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    request_id TEXT,
    public_model_name TEXT,
    litellm_model_name TEXT,
    upstream_model TEXT,
    relay_label TEXT,
    api_base_hash TEXT,
    custom_llm_provider TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL,
    actual_cost REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    latency_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'success',
    error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_events_created_at
    ON usage_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_source_created_at
    ON usage_events (source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_relay_label_created_at
    ON usage_events (relay_label, created_at DESC);

CREATE TABLE IF NOT EXISTS provider_balance_snapshots (
    id TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    relay_label TEXT,
    api_key_env TEXT,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    currency TEXT,
    balance_total REAL,
    balance_used REAL,
    balance_remaining REAL,
    period_start TEXT,
    period_end TEXT,
    raw_summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_provider_balance_snapshots_provider_checked_at
    ON provider_balance_snapshots (provider_key, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_balance_snapshots_api_key_checked_at
    ON provider_balance_snapshots (api_key_env, checked_at DESC);

CREATE TABLE IF NOT EXISTS recharge_records (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    relay_label TEXT,
    api_key_env TEXT,
    paid_amount_cny REAL NOT NULL,
    credited_amount REAL,
    credited_currency TEXT,
    bonus_amount REAL,
    bonus_currency TEXT,
    balance_after_recharge REAL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_recharge_records_provider_paid_at
    ON recharge_records (provider_key, paid_at DESC);
CREATE INDEX IF NOT EXISTS idx_recharge_records_api_key_paid_at
    ON recharge_records (api_key_env, paid_at DESC);
"""


__all__ = [
    "DEFAULT_USAGE_QUOTA_DB_PATH",
    "ProviderBalanceSnapshotRecord",
    "RechargeRecord",
    "UsageEventRecord",
    "UsageQuotaStore",
    "get_usage_quota_store",
    "init_usage_quota_store",
]
