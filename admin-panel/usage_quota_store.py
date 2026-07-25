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


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    platform_key: str | None = None
    provider_key: str | None = None
    api_profile_id: str | None = None
    model_route_id: str | None = None
    route_binding_id: str | None = None
    pricing_version_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None
    actual_cost: float | None = None
    cash_cost_cny: float | None = None
    cost_attribution_status: str = "unattributed"
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
    billing_mode: str | None = None
    quota_items: list[dict[str, Any]] | str | None = None
    period_start: str | None = None
    period_end: str | None = None
    raw_summary: str | dict[str, Any] | list[Any] | None = None
    id: str = field(default_factory=new_uuid)
    checked_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class RechargeRecord:
    provider_key: str
    paid_amount_cny: float | None = None
    paid_at: str = field(default_factory=utc_now_iso)
    relay_label: str | None = None
    api_key_env: str | None = None
    credited_amount: float | None = None
    credited_currency: str | None = None
    bonus_amount: float | None = None
    bonus_currency: str | None = None
    balance_after_recharge: float | None = None
    balance_before_recharge: float | None = None
    balance_delta: float | None = None
    detected_at: str | None = None
    transition_key: str | None = None
    billing_type: str = "topup"
    service_start: str | None = None
    service_end: str | None = None
    allocation_mode: str = "weighted-credit"
    note: str | None = None
    id: str = field(default_factory=new_uuid)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class PricingVersionRecord:
    platform_key: str
    api_profile_id: str
    upstream_model: str
    base_input_per_1m: float
    base_output_per_1m: float
    multiplier: float
    input_per_1m: float
    output_per_1m: float
    currency: str
    effective_from: str
    fingerprint: str
    group_name: str | None = None
    effective_to: str | None = None
    status: str = "candidate"
    source: str = "manual"
    confidence: str = "exact"
    note: str | None = None
    observation_count: int = 1
    first_observed_at: str = field(default_factory=utc_now_iso)
    last_observed_at: str = field(default_factory=utc_now_iso)
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
            self._migrate_existing_tables(conn)
            conn.executescript(_SCHEMA_SQL)
        return self.db_path

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection,
        table_name: str,
        declarations: Mapping[str, str],
    ) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not exists:
            return
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}
        for column, declaration in declarations.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {declaration}")

    def _migrate_existing_tables(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "usage_events",
            {
                "public_model_name": "TEXT",
                "litellm_model_name": "TEXT",
                "upstream_model": "TEXT",
                "relay_label": "TEXT",
                "api_base_hash": "TEXT",
                "custom_llm_provider": "TEXT",
                "platform_key": "TEXT",
                "provider_key": "TEXT",
                "api_profile_id": "TEXT",
                "model_route_id": "TEXT",
                "route_binding_id": "TEXT",
                "pricing_version_id": "TEXT",
                "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
                "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
                "total_tokens": "INTEGER NOT NULL DEFAULT 0",
                "estimated_cost": "REAL",
                "actual_cost": "REAL",
                "cash_cost_cny": "REAL",
                "cost_attribution_status": "TEXT NOT NULL DEFAULT 'unattributed'",
                "currency": "TEXT NOT NULL DEFAULT 'USD'",
                "latency_ms": "INTEGER",
                "status": "TEXT NOT NULL DEFAULT 'success'",
                "error_code": "TEXT",
            },
        )
        self._ensure_columns(
            conn,
            "recharge_records",
            {
                "relay_label": "TEXT",
                "api_key_env": "TEXT",
                "credited_amount": "REAL",
                "credited_currency": "TEXT",
                "bonus_amount": "REAL",
                "bonus_currency": "TEXT",
                "balance_after_recharge": "REAL",
                "balance_before_recharge": "REAL",
                "balance_delta": "REAL",
                "detected_at": "TEXT",
                "transition_key": "TEXT",
                "billing_type": "TEXT NOT NULL DEFAULT 'topup'",
                "service_start": "TEXT",
                "service_end": "TEXT",
                "allocation_mode": "TEXT NOT NULL DEFAULT 'weighted-credit'",
                "note": "TEXT",
            },
        )
        self._make_recharge_payment_nullable(conn)
        self._ensure_columns(
            conn,
            "provider_balance_snapshots",
            {
                "billing_mode": "TEXT NOT NULL DEFAULT 'balance'",
                "quota_items": "TEXT",
            },
        )

    @staticmethod
    def _make_recharge_payment_nullable(conn: sqlite3.Connection) -> None:
        """Migrate the original NOT NULL payment column without losing records."""
        columns = list(conn.execute("PRAGMA table_info(recharge_records)"))
        payment_column = next((row for row in columns if str(row[1]) == "paid_amount_cny"), None)
        if not payment_column or not int(payment_column[3] or 0):
            return

        table_columns = [
            "id", "created_at", "paid_at", "provider_key", "relay_label", "api_key_env",
            "paid_amount_cny", "credited_amount", "credited_currency", "bonus_amount",
            "bonus_currency", "balance_after_recharge", "balance_before_recharge",
            "balance_delta", "detected_at", "transition_key", "billing_type",
            "service_start", "service_end", "allocation_mode", "note",
        ]
        existing_names = {str(row[1]) for row in columns}
        copy_columns = [name for name in table_columns if name in existing_names]
        conn.execute("DROP INDEX IF EXISTS idx_recharge_records_provider_paid_at")
        conn.execute("DROP INDEX IF EXISTS idx_recharge_records_api_key_paid_at")
        conn.execute("ALTER TABLE recharge_records RENAME TO recharge_records_legacy")
        conn.execute(
            """
            CREATE TABLE recharge_records (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                paid_at TEXT NOT NULL,
                provider_key TEXT NOT NULL,
                relay_label TEXT,
                api_key_env TEXT,
                paid_amount_cny REAL,
                credited_amount REAL,
                credited_currency TEXT,
                bonus_amount REAL,
                bonus_currency TEXT,
                balance_after_recharge REAL,
                balance_before_recharge REAL,
                balance_delta REAL,
                detected_at TEXT,
                transition_key TEXT,
                billing_type TEXT NOT NULL DEFAULT 'topup',
                service_start TEXT,
                service_end TEXT,
                allocation_mode TEXT NOT NULL DEFAULT 'weighted-credit',
                note TEXT
            )
            """
        )
        columns_sql = ", ".join(copy_columns)
        conn.execute(
            f"INSERT INTO recharge_records ({columns_sql}) SELECT {columns_sql} FROM recharge_records_legacy"
        )
        conn.execute("DROP TABLE recharge_records_legacy")

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
            "platform_key",
            "provider_key",
            "api_profile_id",
            "model_route_id",
            "route_binding_id",
            "pricing_version_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
            "actual_cost",
            "cash_cost_cny",
            "cost_attribution_status",
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
        payload["cost_attribution_status"] = payload["cost_attribution_status"] or "unattributed"
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

    def insert_usage_event_once(
        self,
        record: UsageEventRecord | Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        payload = _normalize_record(record)
        source = str(payload.get("source") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()
        if request_id:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM usage_events WHERE source = ? AND request_id = ? ORDER BY created_at DESC LIMIT 1",
                    (source, request_id),
                ).fetchone()
            if row:
                return dict(row), False
        return self.insert_usage_event(payload), True

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
            "billing_mode",
            "quota_items",
            "period_start",
            "period_end",
            "raw_summary",
        ]
        payload = {key: row.get(key) for key in columns}
        payload["id"] = payload["id"] or new_uuid()
        payload["checked_at"] = payload["checked_at"] or utc_now_iso()
        payload["billing_mode"] = str(payload["billing_mode"] or "balance").strip().lower() or "balance"
        payload["quota_items"] = _json_text(payload["quota_items"])
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
            "balance_before_recharge",
            "balance_delta",
            "detected_at",
            "transition_key",
            "billing_type",
            "service_start",
            "service_end",
            "allocation_mode",
            "note",
        ]
        payload = {key: row.get(key) for key in columns}
        payload["id"] = payload["id"] or new_uuid()
        payload["created_at"] = payload["created_at"] or utc_now_iso()
        payload["paid_at"] = payload["paid_at"] or utc_now_iso()
        payload["billing_type"] = str(payload["billing_type"] or "topup").strip().lower()
        if payload["billing_type"] not in {"topup", "subscription"}:
            raise ValueError("invalid recharge billing_type")
        default_allocation = "daily-amortized" if payload["billing_type"] == "subscription" else "weighted-credit"
        payload["allocation_mode"] = str(payload["allocation_mode"] or default_allocation).strip().lower()
        if not payload["provider_key"]:
            raise ValueError("recharge_records.provider_key is required")
        if payload["paid_amount_cny"] is not None:
            payload["paid_amount_cny"] = float(payload["paid_amount_cny"])
            if payload["paid_amount_cny"] < 0:
                raise ValueError("recharge paid amount cannot be negative")

        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO recharge_records ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [payload[column] for column in columns],
            )
        return payload

    def record_balance_increase(
        self,
        *,
        provider_key: str,
        relay_label: str = "",
        previous_balance: float,
        current_balance: float,
        currency: str,
        detected_at: str | None = None,
        previous_checked_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Create one pending top-up batch for a newly observed balance increase."""
        previous = float(previous_balance)
        current = float(current_balance)
        delta = round(current - previous, 12)
        if delta <= 0:
            return None, False
        transition_key = "|".join(
            (
                str(provider_key or "").strip(),
                f"{previous:.12g}",
                f"{current:.12g}",
                str(previous_checked_at or "").strip(),
            )
        )
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM recharge_records WHERE provider_key = ? AND transition_key = ? LIMIT 1",
                (provider_key, transition_key),
            ).fetchone()
        if existing:
            return dict(existing), False

        detected = detected_at or utc_now_iso()
        return self.insert_recharge_record(
            {
                "provider_key": provider_key,
                "relay_label": relay_label,
                "api_key_env": "",
                "paid_amount_cny": None,
                "credited_amount": delta,
                "credited_currency": str(currency or "").strip().upper() or None,
                "balance_before_recharge": previous,
                "balance_after_recharge": current,
                "balance_delta": delta,
                "detected_at": detected,
                "transition_key": transition_key,
                "paid_at": detected,
                "billing_type": "topup",
                "allocation_mode": "credit-ledger",
                "note": "自动检测到余额增加，等待补充实际支出",
            }
        ), True

    def get_recharge_record(self, recharge_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recharge_records WHERE id = ?",
                (recharge_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_recharge_record(
        self,
        recharge_id: str,
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self.get_recharge_record(recharge_id)
        if not existing:
            raise ValueError("recharge record not found")
        allowed = {
            "paid_at",
            "paid_amount_cny",
            "credited_amount",
            "credited_currency",
            "bonus_amount",
            "bonus_currency",
            "balance_after_recharge",
            "service_start",
            "service_end",
            "allocation_mode",
            "note",
        }
        changes = {key: value for key, value in dict(updates or {}).items() if key in allowed}
        if "paid_amount_cny" in changes:
            paid_amount = float(changes["paid_amount_cny"] or 0)
            if paid_amount < 0:
                raise ValueError("recharge paid amount cannot be negative")
            changes["paid_amount_cny"] = paid_amount
        if not changes:
            return existing
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE recharge_records SET {assignments} WHERE id = ?",
                [*changes.values(), recharge_id],
            )
        return self.get_recharge_record(recharge_id) or {**existing, **changes}

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

    def insert_pricing_version(self, record: PricingVersionRecord | Mapping[str, Any]) -> dict[str, Any]:
        row = _normalize_record(record)
        columns = [
            "id",
            "created_at",
            "platform_key",
            "api_profile_id",
            "upstream_model",
            "group_name",
            "base_input_per_1m",
            "base_output_per_1m",
            "multiplier",
            "input_per_1m",
            "output_per_1m",
            "currency",
            "effective_from",
            "effective_to",
            "status",
            "source",
            "confidence",
            "note",
            "fingerprint",
            "observation_count",
            "first_observed_at",
            "last_observed_at",
        ]
        payload = {key: row.get(key) for key in columns}
        now = utc_now_iso()
        payload["id"] = payload["id"] or new_uuid()
        payload["created_at"] = payload["created_at"] or now
        payload["platform_key"] = str(payload["platform_key"] or "").strip().lower()
        payload["api_profile_id"] = str(payload["api_profile_id"] or "").strip()
        payload["upstream_model"] = str(payload["upstream_model"] or "*").strip() or "*"
        payload["currency"] = str(payload["currency"] or "USD").strip().upper()
        payload["status"] = str(payload["status"] or "candidate").strip().lower()
        payload["source"] = str(payload["source"] or "manual").strip().lower()
        payload["confidence"] = str(payload["confidence"] or "exact").strip().lower()
        payload["observation_count"] = max(1, int(payload["observation_count"] or 1))
        payload["first_observed_at"] = payload["first_observed_at"] or now
        payload["last_observed_at"] = payload["last_observed_at"] or now
        for key in (
            "base_input_per_1m",
            "base_output_per_1m",
            "multiplier",
            "input_per_1m",
            "output_per_1m",
        ):
            payload[key] = float(payload[key] or 0)
        if not payload["platform_key"] or not payload["api_profile_id"]:
            raise ValueError("pricing_versions platform_key and api_profile_id are required")
        if not payload["effective_from"] or not payload["fingerprint"]:
            raise ValueError("pricing_versions effective_from and fingerprint are required")
        if payload["status"] not in {"candidate", "active", "rejected", "superseded"}:
            raise ValueError("invalid pricing version status")

        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO pricing_versions ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [payload[column] for column in columns],
            )
        return payload

    def get_pricing_version(self, version_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pricing_versions WHERE id = ?", (version_id,)).fetchone()
        return dict(row) if row else None

    def list_pricing_versions(
        self,
        *,
        platform_key: str,
        api_profile_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pricing_versions WHERE platform_key = ?"
        params: list[Any] = [str(platform_key or "").strip().lower()]
        if api_profile_id:
            query += " AND api_profile_id = ?"
            params.append(api_profile_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY effective_from DESC, created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_active_pricing_version(self, api_profile_id: str, upstream_model: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pricing_versions
                WHERE api_profile_id = ? AND status = 'active' AND upstream_model IN (?, '*')
                ORDER BY CASE WHEN upstream_model = ? THEN 0 ELSE 1 END, effective_from DESC
                LIMIT 1
                """,
                (api_profile_id, upstream_model, upstream_model),
            ).fetchone()
        return dict(row) if row else None

    def get_effective_pricing_version(
        self,
        api_profile_id: str,
        upstream_model: str,
        occurred_at: str,
    ) -> dict[str, Any] | None:
        target = _parse_iso(occurred_at)
        if target is None:
            return self.get_active_pricing_version(api_profile_id, upstream_model)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pricing_versions
                WHERE api_profile_id = ? AND upstream_model IN (?, '*')
                  AND status IN ('active', 'superseded')
                ORDER BY CASE WHEN upstream_model = ? THEN 0 ELSE 1 END, effective_from DESC
                """,
                (api_profile_id, upstream_model, upstream_model),
            ).fetchall()
        for row in rows:
            item = dict(row)
            starts_at = _parse_iso(item.get("effective_from"))
            ends_at = _parse_iso(item.get("effective_to"))
            if starts_at is None:
                continue
            if starts_at <= target and (ends_at is None or target < ends_at):
                return item
        return None

    def activate_pricing_version(self, version_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pricing_versions WHERE id = ?", (version_id,)).fetchone()
            if not row:
                raise ValueError("pricing version not found")
            item = dict(row)
            conn.execute(
                """
                UPDATE pricing_versions
                SET status = 'superseded', effective_to = ?
                WHERE platform_key = ? AND api_profile_id = ? AND upstream_model = ?
                  AND status = 'active' AND id <> ?
                """,
                (
                    item["effective_from"],
                    item["platform_key"],
                    item["api_profile_id"],
                    item["upstream_model"],
                    item["id"],
                ),
            )
            conn.execute(
                "UPDATE pricing_versions SET status = 'active', effective_to = NULL WHERE id = ?",
                (item["id"],),
            )
        return self.get_pricing_version(version_id) or item

    def reject_pricing_version(self, version_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE pricing_versions SET status = 'rejected' WHERE id = ? AND status = 'candidate'",
                (version_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("pricing candidate not found")
        return self.get_pricing_version(version_id) or {}

    def observe_pricing_candidate(self, record: PricingVersionRecord | Mapping[str, Any]) -> dict[str, Any]:
        payload = _normalize_record(record)
        platform_key = str(payload.get("platform_key") or "").strip().lower()
        api_profile_id = str(payload.get("api_profile_id") or "").strip()
        upstream_model = str(payload.get("upstream_model") or "*").strip() or "*"
        fingerprint = str(payload.get("fingerprint") or "").strip()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pricing_versions
                WHERE platform_key = ? AND api_profile_id = ? AND upstream_model = ?
                  AND fingerprint = ? AND status = 'candidate'
                ORDER BY created_at DESC LIMIT 1
                """,
                (platform_key, api_profile_id, upstream_model, fingerprint),
            ).fetchone()
            if row:
                item = dict(row)
                conn.execute(
                    """
                    UPDATE pricing_versions
                    SET observation_count = observation_count + 1, last_observed_at = ?
                    WHERE id = ?
                    """,
                    (utc_now_iso(), item["id"]),
                )
                version_id = item["id"]
            else:
                version_id = ""
        if version_id:
            return self.get_pricing_version(version_id) or {}
        return self.insert_pricing_version({**payload, "status": "candidate"})


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
    platform_key TEXT,
    provider_key TEXT,
    api_profile_id TEXT,
    model_route_id TEXT,
    route_binding_id TEXT,
    pricing_version_id TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL,
    actual_cost REAL,
    cash_cost_cny REAL,
    cost_attribution_status TEXT NOT NULL DEFAULT 'unattributed',
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
CREATE INDEX IF NOT EXISTS idx_usage_events_api_profile_created_at
    ON usage_events (api_profile_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_binding_created_at
    ON usage_events (route_binding_id, created_at DESC);

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
    billing_mode TEXT NOT NULL DEFAULT 'balance',
    quota_items TEXT,
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
    paid_amount_cny REAL,
    credited_amount REAL,
    credited_currency TEXT,
    bonus_amount REAL,
    bonus_currency TEXT,
    balance_after_recharge REAL,
    balance_before_recharge REAL,
    balance_delta REAL,
    detected_at TEXT,
    transition_key TEXT,
    billing_type TEXT NOT NULL DEFAULT 'topup',
    service_start TEXT,
    service_end TEXT,
    allocation_mode TEXT NOT NULL DEFAULT 'weighted-credit',
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_recharge_records_provider_paid_at
    ON recharge_records (provider_key, paid_at DESC);
CREATE INDEX IF NOT EXISTS idx_recharge_records_api_key_paid_at
    ON recharge_records (api_key_env, paid_at DESC);

CREATE TABLE IF NOT EXISTS pricing_versions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    platform_key TEXT NOT NULL,
    api_profile_id TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    group_name TEXT,
    base_input_per_1m REAL NOT NULL DEFAULT 0,
    base_output_per_1m REAL NOT NULL DEFAULT 0,
    multiplier REAL NOT NULL DEFAULT 1,
    input_per_1m REAL NOT NULL DEFAULT 0,
    output_per_1m REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    note TEXT,
    fingerprint TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pricing_versions_scope_effective
    ON pricing_versions (platform_key, api_profile_id, upstream_model, effective_from DESC);
CREATE INDEX IF NOT EXISTS idx_pricing_versions_status
    ON pricing_versions (platform_key, status, created_at DESC);
"""


__all__ = [
    "DEFAULT_USAGE_QUOTA_DB_PATH",
    "ProviderBalanceSnapshotRecord",
    "PricingVersionRecord",
    "RechargeRecord",
    "UsageEventRecord",
    "UsageQuotaStore",
    "get_usage_quota_store",
    "init_usage_quota_store",
]
