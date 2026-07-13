from __future__ import annotations

import json
import logging
import os
import re
import threading
import subprocess
import tempfile
import time
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx
import psutil
import shutil
import sqlite3
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from usage_quota_store import get_usage_quota_store, init_usage_quota_store


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "litellm.yaml"
STATE_PATH = ROOT / "config" / "relaydeck-state.json"
ENV_PATH = ROOT / ".env"
SCRIPTS_DIR = ROOT / "scripts"
STATIC_DIR = ROOT / "admin-panel" / "static"
LOG_DIR = ROOT / "logs"
ADMIN_PANEL_DIR = ROOT / "admin-panel"
CONDA_ENV_ROOT = Path(r"D:/conda/envs/llm-stack-local")
CCSWITCH_WEB_DATA = Path(
    os.environ.get(
        "CCSWITCH_WEB_DATA",
        str(Path.home() / "AppData/Local/com.ccswitch.desktop/EBWebView/Default/Web Data"),
    )
)

LITELLM_EXE = CONDA_ENV_ROOT / "Scripts" / "litellm.exe"
OPEN_WEBUI_EXE = CONDA_ENV_ROOT / "Scripts" / "open-webui.exe"
PYTHON_EXE = CONDA_ENV_ROOT / "python.exe"
QUOTA_ADAPTERS_DIR = ROOT / "quota-adapters"
BALANCE_REFRESH_INTERVAL_SEC = max(60, int(os.environ.get("BALANCE_REFRESH_INTERVAL_SEC", "300") or 300))
logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
KNOWN_PORTAL_HOST_ADAPTERS = {
    "lingsuan.top": "lingsuan-web",
    "auto-code.net": "autocode-web",
}


def parse_env_lines() -> list[tuple[str | None, str]]:
    rows: list[tuple[str | None, str]] = []
    if not ENV_PATH.exists():
        return rows
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rows.append((None, line))
            continue
        key, value = line.split("=", 1)
        rows.append((key.strip(), value))
    return rows


def parse_env_file() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV_PATH.exists():
        return data
    for key, value in parse_env_lines():
        if key:
            data[key] = value
    return data


def ensure_quota_adapters_dir() -> Path:
    QUOTA_ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    return QUOTA_ADAPTERS_DIR


def list_quota_adapter_scripts() -> list[dict[str, Any]]:
    root = ensure_quota_adapters_dir()
    items: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".py", ".ps1", ".cmd", ".bat"}:
            continue
        items.append(
            {
                "name": path.name,
                "stem": path.stem,
                "path": str(path),
                "type": path.suffix.lower().lstrip("."),
            }
        )
    return items


def resolve_quota_adapter_script(script_name: str | None) -> Path | None:
    text = str(script_name or "").strip()
    if not text:
        return None
    root = ensure_quota_adapters_dir().resolve()
    candidate = Path(text)
    if not candidate.suffix:
        for suffix in (".py", ".ps1", ".cmd", ".bat"):
            named = root / f"{text}{suffix}"
            if named.exists() and named.is_file():
                return named
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except Exception:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    if resolved.suffix.lower() not in {".py", ".ps1", ".cmd", ".bat"}:
        return None
    return resolved


def write_env_file(env_map: dict[str, str]) -> None:
    remaining = dict(env_map)
    output: list[str] = []
    for key, raw_value in parse_env_lines():
        if key is None:
            output.append(raw_value)
            continue
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(f"{key}={raw_value}")
    for key in sorted(remaining):
        output.append(f"{key}={remaining[key]}")
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def backup_file(path: Path) -> None:
    if not path.exists():
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{ts}")
    backup_path.write_bytes(path.read_bytes())


def save_config(config: dict[str, Any]) -> None:
    backup_file(CONFIG_PATH)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def read_env_var_name(api_key_value: str | None) -> str:
    if not api_key_value:
        return ""
    prefix = "os.environ/"
    if api_key_value.startswith(prefix):
        return api_key_value[len(prefix) :]
    return ""


def resolve_secret(ref: str, env_map: dict[str, str]) -> str:
    if ref.startswith("os.environ/"):
        return env_map.get(ref.split("/", 1)[1], "")
    return ref


def get_ports(env_map: dict[str, str]) -> tuple[int, int, int]:
    litellm_port = int(env_map.get("LITELLM_PORT", "4100"))
    open_webui_port = int(env_map.get("OPEN_WEBUI_PORT", "8090"))
    admin_port = int(env_map.get("ADMIN_PANEL_PORT", "8091"))
    return litellm_port, open_webui_port, admin_port


def looks_like_env_var_name(value: str | None) -> bool:
    return bool(ENV_VAR_NAME_RE.fullmatch((value or "").strip()))


def env_varize(value: str | None) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_").upper()
    if not text:
        text = "API_KEY"
    if text[0].isdigit():
        text = f"API_{text}"
    return text


def default_api_key_env_name(profile_seed: str, label: str = "", provider_name: str = "openai") -> str:
    suffix = env_varize(profile_seed or label or provider_name or "API_KEY")
    return f"RELAYDECK_API_KEY_{suffix}"


def extract_api_key_fields(row: dict[str, Any], profile_seed: str) -> tuple[str, str]:
    api_key_env = str(row.get("api_key_env") or "").strip()
    api_key_value = str(row.get("api_key_value") or "").strip()
    if api_key_env and not looks_like_env_var_name(api_key_env):
        if not api_key_value:
            api_key_value = api_key_env
        api_key_env = ""
    if api_key_value and not api_key_env:
        api_key_env = default_api_key_env_name(
            profile_seed,
            str(row.get("label") or row.get("relay_label") or "").strip(),
            normalize_provider_name(row.get("custom_llm_provider")),
        )
    return api_key_env, api_key_value


def hydrate_api_profiles_with_secrets(api_profiles: list[dict[str, Any]], env_map: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in api_profiles:
        key_name = str(row.get("api_key_env") or "").strip()
        key_value = str(row.get("api_key_value") or "").strip()
        if key_name and not key_value:
            key_value = env_map.get(key_name, "").strip()
        items.append({**row, "api_key_value": key_value})
    return items


def merge_api_profile_secrets_into_env(env_map: dict[str, str], api_profiles: list[dict[str, Any]]) -> dict[str, str]:
    merged = dict(env_map)
    for row in api_profiles:
        key_name = str(row.get("api_key_env") or "").strip()
        key_value = str(row.get("api_key_value") or "").strip()
        if key_value and not key_name:
            key_name = default_api_key_env_name(
                str(row.get("id") or ""),
                str(row.get("label") or ""),
                normalize_provider_name(row.get("custom_llm_provider")),
            )
            row["api_key_env"] = key_name
        if key_name:
            merged[key_name] = key_value
    return merged


def ensure_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_provider_name(custom_llm_provider: str | None) -> str:
    return (custom_llm_provider or "openai").strip().lower() or "openai"


def quota_has_manual_values(quota: dict[str, Any]) -> bool:
    return any(quota.get(key) not in (None, "") for key in ("limit", "current_balance", "used_amount"))


def infer_portal_quota_adapter(profile: dict[str, Any]) -> str | None:
    quota = ensure_mapping(profile.get("quota"))
    candidates = [str(quota.get("portal_base") or "").strip(), str(profile.get("api_base") or "").strip()]
    for value in candidates:
        parsed = urlparse(value)
        host = (parsed.netloc or parsed.path or "").strip().lower()
        if not host:
            continue
        for known_host, adapter in KNOWN_PORTAL_HOST_ADAPTERS.items():
            if host == known_host or host.endswith(f".{known_host}"):
                return adapter
    return None


def effective_quota_adapter(profile: dict[str, Any]) -> str:
    quota = ensure_mapping(profile.get("quota"))
    adapter = str(quota.get("adapter") or "").strip().lower()
    if adapter and adapter != "manual":
        return adapter
    if adapter == "manual" and quota_has_manual_values(quota):
        return adapter
    inferred_adapter = infer_portal_quota_adapter(profile)
    if inferred_adapter:
        return inferred_adapter
    return adapter or "manual"


def resolve_provider_api_base(api_base: str | None, custom_llm_provider: str | None) -> str:
    value = (api_base or "").strip().rstrip("/")
    if value:
        return value
    return DEFAULT_PROVIDER_BASES.get(normalize_provider_name(custom_llm_provider), "")


def api_base_mismatch_hint(custom_llm_provider: str | None, api_base: str | None, status_code: int | None = None) -> str:
    provider_name = normalize_provider_name(custom_llm_provider)
    value = (api_base or "").strip().rstrip("/")
    if provider_name == "anthropic" and value.endswith("/v1"):
        base = value[:-3].rstrip("/") or value
        return (
            "Provider=anthropic 时系统会自动补 /v1/models 和 /v1/messages；"
            f"当前 API Base 已包含 /v1，实际可能会请求成 /v1/v1/...。"
            f"请把 API Base 改成不带 /v1 的根地址，例如 {base}"
        )
    if status_code == 404 and provider_name == "anthropic":
        return "当前接口不像原生 Anthropic 路径；如果这是 OpenAI 兼容中转站，请把 Provider 类型改成 openai 再试。"
    return ""


def now_iso_local() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def month_start_iso_local() -> str:
    now = datetime.now().astimezone()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


def api_base_hash(api_base: str | None) -> str:
    return hashlib.sha256((api_base or "").strip().encode("utf-8")).hexdigest()[:16]


def profile_provider_key(profile: dict[str, Any]) -> str:
    supplier_id = str(profile.get("supplier_id") or "").strip()
    profile_id = str(profile.get("id") or "").strip()
    api_key_env = str(profile.get("api_key_env") or "").strip()
    api_base = str(profile.get("api_base") or "").strip()
    provider_name = normalize_provider_name(profile.get("custom_llm_provider"))
    return make_stable_id("billing", supplier_id or "no-supplier", profile_id or api_base, api_key_env or provider_name)


def supplier_provider_key(supplier_id: str | None, provider_name: str | None = None) -> str:
    supplier_key = str(supplier_id or "").strip() or "no-supplier"
    provider_key = normalize_provider_name(provider_name)
    return make_stable_id("billing-supplier", supplier_key, provider_key or "openai")


def parse_window_start(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def provider_progress_tone(percent_used: float | None) -> str:
    if percent_used is None:
        return "unknown"
    if percent_used >= 95:
        return "critical"
    if percent_used >= 80:
        return "warning"
    return "ok"


def extract_usage_from_response(body: Any) -> dict[str, int]:
    if not isinstance(body, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def estimate_cost_from_profile(profile: dict[str, Any], prompt_tokens: int, completion_tokens: int) -> tuple[float | None, str]:
    pricing = ensure_mapping(profile.get("pricing"))
    input_per_1m = pricing.get("input_per_1m")
    output_per_1m = pricing.get("output_per_1m")
    currency = str(pricing.get("currency") or "USD")
    if input_per_1m in (None, "") and output_per_1m in (None, ""):
        return None, currency
    input_cost = (prompt_tokens / 1_000_000) * float(input_per_1m or 0)
    output_cost = (completion_tokens / 1_000_000) * float(output_per_1m or 0)
    return input_cost + output_cost, currency


def normalize_model_key(value: Any) -> str:
    return str(value or "").strip().lower()


def usage_event_matches_binding(item: dict[str, Any], route: dict[str, Any], binding: dict[str, Any]) -> bool:
    route_key = normalize_model_key(route.get("public_model_name"))
    binding_upstream_key = normalize_model_key(binding.get("upstream_model"))
    event_public_key = normalize_model_key(item.get("public_model_name"))
    event_alias_key = normalize_model_key(item.get("litellm_model_name"))
    event_upstream_key = normalize_model_key(item.get("upstream_model"))

    route_matches = bool(route_key and (event_public_key == route_key or event_alias_key == route_key))
    upstream_matches = bool(binding_upstream_key and event_upstream_key == binding_upstream_key)

    if route_key and binding_upstream_key:
        return route_matches and upstream_matches
    if binding_upstream_key:
        return upstream_matches
    if route_key:
        return route_matches
    return False


def summarize_profile_usage(profile: dict[str, Any], usage_events: list[dict[str, Any]], recharge_records: list[dict[str, Any]], latest_balance: dict[str, Any] | None) -> dict[str, Any]:
    provider_key = profile_provider_key(profile)
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    estimated_cost = 0.0
    actual_cost = 0.0
    actual_cost_count = 0
    latencies: list[int] = []
    success_count = 0
    total_requests = 0
    for item in usage_events:
        if item.get("status") == "success":
            success_count += 1
        total_requests += 1
        total_prompt += int(item.get("prompt_tokens") or 0)
        total_completion += int(item.get("completion_tokens") or 0)
        total_tokens += int(item.get("total_tokens") or 0)
        if item.get("estimated_cost") not in (None, ""):
            estimated_cost += float(item.get("estimated_cost") or 0)
        if item.get("actual_cost") not in (None, ""):
            actual_cost += float(item.get("actual_cost") or 0)
            actual_cost_count += 1
        if item.get("latency_ms") not in (None, ""):
            latencies.append(int(item.get("latency_ms") or 0))

    total_paid_cny = sum(float(item.get("paid_amount_cny") or 0) for item in recharge_records)
    effective_cny_per_1m_tokens = None
    tokens_per_cny = None
    if total_paid_cny > 0 and total_tokens > 0:
        effective_cny_per_1m_tokens = total_paid_cny / total_tokens * 1_000_000
        tokens_per_cny = total_tokens / total_paid_cny

    quota = ensure_mapping(profile.get("quota"))
    accounting = ensure_mapping(profile.get("accounting"))
    usage_conf = ensure_mapping(profile.get("usage"))
    quota_breakdown = extract_snapshot_quota_breakdown(latest_balance)

    manual_limit = quota.get("limit")
    currency = str((latest_balance or {}).get("currency") or quota.get("currency") or accounting.get("base_currency") or "CNY")
    balance_total = (latest_balance or {}).get("balance_total")
    balance_used = (latest_balance or {}).get("balance_used")
    balance_remaining = (latest_balance or {}).get("balance_remaining")

    if balance_total in (None, "") and manual_limit not in (None, ""):
        balance_total = float(manual_limit)
    if balance_used in (None, "") and balance_total not in (None, "") and balance_remaining not in (None, ""):
        balance_used = float(balance_total) - float(balance_remaining)
    if balance_remaining in (None, "") and balance_total not in (None, "") and balance_used not in (None, ""):
        balance_remaining = float(balance_total) - float(balance_used)

    cycle_adjusted = apply_latest_recharge_cycle(
        recharge_records,
        balance_total=balance_total,
        balance_used=balance_used,
        balance_remaining=balance_remaining,
    )
    balance_total = cycle_adjusted.get("balance_total")
    balance_used = cycle_adjusted.get("balance_used")
    balance_remaining = cycle_adjusted.get("balance_remaining")

    percent_used = None
    if balance_total not in (None, "", 0) and balance_used not in (None, ""):
        try:
            percent_used = max(0.0, min(100.0, float(balance_used) / float(balance_total) * 100))
        except ZeroDivisionError:
            percent_used = None

    return {
        "provider_key": provider_key,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "estimated_cost": round(estimated_cost, 6),
        "actual_cost": round(actual_cost, 6) if actual_cost_count else None,
        "actual_cost_count": actual_cost_count,
        "currency": currency,
        "success_count": success_count,
        "success_rate": round(success_count / total_requests, 4) if total_requests else None,
        "latency_sum_ms": sum(latencies),
        "latency_count": len(latencies),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "total_requests": total_requests,
        "balance_total": float(balance_total) if balance_total not in (None, "") else None,
        "balance_used": float(balance_used) if balance_used not in (None, "") else None,
        "balance_remaining": float(balance_remaining) if balance_remaining not in (None, "") else None,
        "percent_used": round(percent_used, 2) if percent_used is not None else None,
        "progress_tone": provider_progress_tone(percent_used),
        "recharge_paid_cny": round(total_paid_cny, 4),
        "effective_cny_per_1m_tokens": round(effective_cny_per_1m_tokens, 4) if effective_cny_per_1m_tokens is not None else None,
        "tokens_per_cny": round(tokens_per_cny, 4) if tokens_per_cny is not None else None,
        "quota_enabled": bool(quota.get("enabled", False)),
        "usage_track_enabled": bool(usage_conf.get("track_enabled", True)),
        "quota_breakdown": quota_breakdown,
        "cycle_recharge_record": cycle_adjusted.get("cycle_recharge_record"),
        "cycle_quota_message": cycle_adjusted.get("quota_message") or "",
    }


def summarize_binding_model_usage(
    route: dict[str, Any],
    binding: dict[str, Any],
    profile: dict[str, Any],
    supplier: dict[str, Any],
    usage_events: list[dict[str, Any]],
) -> dict[str, Any]:
    matched_events = [item for item in usage_events if usage_event_matches_binding(item, route, binding)]
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    estimated_cost = 0.0
    actual_cost = 0.0
    actual_cost_count = 0
    success_count = 0
    total_requests = 0
    latency_sum = 0
    latency_count = 0
    last_used_at = ""

    for item in matched_events:
        if item.get("status") == "success":
            success_count += 1
        total_requests += 1
        total_prompt += int(item.get("prompt_tokens") or 0)
        total_completion += int(item.get("completion_tokens") or 0)
        total_tokens += int(item.get("total_tokens") or 0)
        if item.get("estimated_cost") not in (None, ""):
            estimated_cost += float(item.get("estimated_cost") or 0)
        if item.get("actual_cost") not in (None, ""):
            actual_cost += float(item.get("actual_cost") or 0)
            actual_cost_count += 1
        if item.get("latency_ms") not in (None, ""):
            latency_sum += int(item.get("latency_ms") or 0)
            latency_count += 1
        created_at = str(item.get("created_at") or "")
        if created_at and created_at > last_used_at:
            last_used_at = created_at

    price_currency = str(ensure_mapping(profile.get("pricing")).get("currency") or "USD")
    cost_currency = "USD"
    effective_cost_per_1m = None
    tokens_per_cost_unit = None
    cost_source = "none"
    if total_tokens > 0 and actual_cost_count > 0 and actual_cost > 0:
        effective_cost_per_1m = actual_cost / total_tokens * 1_000_000
        tokens_per_cost_unit = total_tokens / actual_cost
        cost_currency = str(matched_events[-1].get("currency") or price_currency or "USD")
        cost_source = "actual"
    elif total_tokens > 0 and estimated_cost > 0:
        effective_cost_per_1m = estimated_cost / total_tokens * 1_000_000
        tokens_per_cost_unit = total_tokens / estimated_cost
        cost_currency = price_currency or "USD"
        cost_source = "estimated"

    return {
        "binding_id": str(binding.get("id") or ""),
        "model_route_id": str(route.get("id") or ""),
        "public_model_name": str(route.get("public_model_name") or ""),
        "display_name": str(route.get("display_name") or route.get("public_model_name") or ""),
        "api_profile_id": str(profile.get("id") or ""),
        "api_label": str(profile.get("label") or ""),
        "supplier_id": str(supplier.get("id") or ""),
        "supplier_name": str(supplier.get("name") or supplier.get("label") or ""),
        "provider": normalize_provider_name(profile.get("custom_llm_provider")),
        "api_base": str(profile.get("api_base") or ""),
        "upstream_model": str(binding.get("upstream_model") or ""),
        "priority": int(binding.get("priority", 100) or 100),
        "enabled": bool(binding.get("enabled", True)),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "estimated_cost": round(estimated_cost, 6),
        "actual_cost": round(actual_cost, 6) if actual_cost_count else None,
        "actual_cost_count": actual_cost_count,
        "cost_currency": cost_currency,
        "effective_cost_per_1m": round(effective_cost_per_1m, 6) if effective_cost_per_1m is not None else None,
        "tokens_per_cost_unit": round(tokens_per_cost_unit, 4) if tokens_per_cost_unit is not None else None,
        "cost_source": cost_source,
        "success_rate": round(success_count / total_requests, 4) if total_requests else None,
        "avg_latency_ms": round(latency_sum / latency_count) if latency_count else None,
        "total_requests": total_requests,
        "last_used_at": last_used_at or None,
    }


def latest_balance_for_profile(store: Any, profile: dict[str, Any]) -> dict[str, Any] | None:
    return store.get_latest_provider_balance_snapshot(
        profile_provider_key(profile),
        api_key_env=str(profile.get("api_key_env") or "").strip() or None,
    )


def parse_snapshot_raw_summary(latest_balance: dict[str, Any] | None) -> dict[str, Any]:
    if not latest_balance:
        return {}
    raw_summary = latest_balance.get("raw_summary")
    if isinstance(raw_summary, dict):
        return raw_summary
    if isinstance(raw_summary, str) and raw_summary.strip():
        try:
            parsed = json.loads(raw_summary)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_snapshot_quota_breakdown(latest_balance: dict[str, Any] | None) -> dict[str, Any]:
    raw_summary = parse_snapshot_raw_summary(latest_balance)
    if not raw_summary:
        return {}
    direct = raw_summary.get("quota_breakdown")
    if isinstance(direct, dict):
        return direct
    script_output = raw_summary.get("script_output")
    if isinstance(script_output, dict):
        nested = script_output.get("quota_breakdown")
        if isinstance(nested, dict):
            return nested
    nested_raw_summary = raw_summary.get("raw_summary")
    if isinstance(nested_raw_summary, dict):
        nested_direct = nested_raw_summary.get("quota_breakdown")
        if isinstance(nested_direct, dict):
            return nested_direct
        nested_script_output = nested_raw_summary.get("script_output")
        if isinstance(nested_script_output, dict):
            nested = nested_script_output.get("quota_breakdown")
            if isinstance(nested, dict):
                return nested
    return {}


def quota_display_message(profile: dict[str, Any], latest_balance: dict[str, Any] | None) -> str:
    if latest_balance:
        raw_summary = parse_snapshot_raw_summary(latest_balance)
        script_output = raw_summary.get("script_output") if isinstance(raw_summary.get("script_output"), dict) else {}
        for key in ("message", "error", "detail"):
            value = raw_summary.get(key)
            if value:
                return str(value)
            nested_value = script_output.get(key)
            if nested_value:
                return str(nested_value)
        status = str(latest_balance.get("status") or "").strip()
        if status == "ok":
            return "额度已刷新。"
        if status == "unsupported":
            return "当前接口暂未识别出可用额度字段。"

    quota = ensure_mapping(profile.get("quota"))
    if not bool(quota.get("enabled", False)):
        return "未启用额度进度。"

    adapter = effective_quota_adapter(profile)
    if adapter == "manual":
        has_any_value = quota_has_manual_values(quota)
        return "手动额度模式：请填写总额 / 已用 / 剩余。" if not has_any_value else "手动额度模式：可点击刷新额度更新进度条。"

    if adapter in {"custom-script", "custom_script", "script"}:
        if not str(quota.get("script_name") or quota.get("script_path") or "").strip():
            return "自定义脚本模式：请填写 script_name，再点击刷新额度。"
        return "自定义脚本模式：将按 quota-adapters 目录里的脚本抓取额度。"

    if adapter in {"lingsuan-web", "autocode-web", "portal_web_token"}:
        if not str(quota.get("auth_token") or "").strip():
            return "自动额度模式：缺少网站 auth_token。"
        return "自动额度模式：请点击刷新额度。"

    if adapter in {"lingsuan-admin", "autocode-admin", "portal_admin_key"}:
        if not str(quota.get("admin_api_key") or "").strip():
            return "自动额度模式：缺少 admin_api_key。"
        if not str(quota.get("user_id") or "").strip():
            return "自动额度模式：建议补充 user_id，再点击刷新额度。"
        return "自动额度模式：请点击刷新额度。"

    return f"额度模式 {adapter} 尚未刷新。"


def recharge_records_for_profile(store: Any, profile: dict[str, Any]) -> list[dict[str, Any]]:
    return store.list_recharge_records(
        limit=500,
        provider_key=profile_provider_key(profile),
        api_key_env=str(profile.get("api_key_env") or "").strip() or None,
    )


def recharge_records_for_supplier(
    store: Any,
    supplier_id: str | None,
    *,
    provider_name: str | None = None,
) -> list[dict[str, Any]]:
    return store.list_recharge_records(
        limit=500,
        provider_key=supplier_provider_key(supplier_id, provider_name),
    )


def apply_latest_recharge_cycle(
    recharge_records: list[dict[str, Any]] | None,
    *,
    balance_total: Any,
    balance_used: Any,
    balance_remaining: Any,
    quota_message: str = "",
) -> dict[str, Any]:
    current_remaining = numeric_value(balance_remaining)
    current_total = numeric_value(balance_total)
    current_used = numeric_value(balance_used)
    if current_remaining is None:
        return {
            "balance_total": current_total,
            "balance_used": current_used,
            "balance_remaining": current_remaining,
            "quota_message": quota_message,
            "cycle_recharge_record": None,
        }

    records = recharge_records or []
    for item in records:
        credited_amount = numeric_value(item.get("credited_amount"))
        bonus_amount = numeric_value(item.get("bonus_amount")) or 0.0
        cycle_total = (credited_amount or 0.0) + bonus_amount
        balance_after_recharge = numeric_value(item.get("balance_after_recharge"))
        if cycle_total <= 0 or balance_after_recharge is None:
            continue
        if balance_after_recharge + 1e-6 < current_remaining:
            continue

        carryover_before_recharge = balance_after_recharge - cycle_total
        if carryover_before_recharge < 0:
            carryover_before_recharge = 0.0

        cycle_remaining = current_remaining - carryover_before_recharge
        if cycle_remaining < 0:
            cycle_remaining = 0.0
        if cycle_remaining - cycle_total > 1e-6:
            continue

        cycle_remaining = min(cycle_total, cycle_remaining)
        cycle_used = max(0.0, cycle_total - cycle_remaining)
        cycle_message = (
            f"按最近充值轮次展示：本轮到账 {cycle_total:.2f}，"
            f"结转旧余额 {carryover_before_recharge:.2f}，当前本轮剩余 {cycle_remaining:.2f}。"
        )
        if quota_message:
            cycle_message = f"{quota_message} {cycle_message}".strip()
        return {
            "balance_total": round(cycle_total, 6),
            "balance_used": round(cycle_used, 6),
            "balance_remaining": round(cycle_remaining, 6),
            "quota_message": cycle_message,
            "cycle_recharge_record": item,
        }

    return {
        "balance_total": current_total,
        "balance_used": current_used,
        "balance_remaining": current_remaining,
        "quota_message": quota_message,
        "cycle_recharge_record": None,
    }


def usage_events_for_profile(store: Any, profile: dict[str, Any], start_at: str | None = None) -> list[dict[str, Any]]:
    events = store.list_usage_events(limit=5000)
    base_hash = api_base_hash(profile.get("api_base"))
    provider_name = normalize_provider_name(profile.get("custom_llm_provider"))
    relay_candidates = {
        str(profile.get("label") or "").strip(),
        str(profile.get("api_base") or "").strip(),
        str(profile.get("id") or "").strip(),
    }
    relay_candidates = {item for item in relay_candidates if item}
    filtered: list[dict[str, Any]] = []
    start_dt = iso_to_dt(start_at)
    for item in events:
        if item.get("api_base_hash") != base_hash:
            continue
        if normalize_provider_name(item.get("custom_llm_provider")) != provider_name:
            continue
        if relay_candidates and str(item.get("relay_label") or "").strip() not in relay_candidates:
            continue
        if start_dt is not None:
            created_dt = iso_to_dt(item.get("created_at"))
            if created_dt and created_dt < start_dt:
                continue
        filtered.append(item)
    return filtered


def quota_message_from_snapshot(latest_balance: dict[str, Any] | None) -> str:
    if not latest_balance:
        return ""
    raw_summary = parse_snapshot_raw_summary(latest_balance)
    script_output = raw_summary.get("script_output") if isinstance(raw_summary.get("script_output"), dict) else {}
    for key in ("message", "error", "detail"):
        value = raw_summary.get(key)
        if value:
            return str(value)
        nested_value = script_output.get(key)
        if nested_value:
            return str(nested_value)
    status = str(latest_balance.get("status") or "").strip()
    if status == "ok":
        return "额度已刷新。"
    if status == "unsupported":
        return "当前接口暂未识别出可用额度字段。"
    return ""


def aggregate_supplier_usage_rows(
    store: Any,
    summary_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in summary_rows:
        supplier_id = str(row.get("supplier_id") or "").strip()
        provider_name = normalize_provider_name(row.get("provider"))
        group_key = (supplier_id, provider_name)
        group = grouped.get(group_key)
        if group is None:
            group = {
                "supplier_id": supplier_id,
                "supplier_name": row.get("supplier_name"),
                "provider": provider_name,
                "rows": [],
                "api_labels": [],
                "latest_balance": None,
                "latest_balance_dt": None,
                "quota_message": "",
            }
            grouped[group_key] = group

        group["rows"].append(row)
        api_label = str(row.get("api_label") or "").strip()
        if api_label and api_label not in group["api_labels"]:
            group["api_labels"].append(api_label)

        checked_at = row.get("balance_checked_at")
        latest_balance_dt = iso_to_dt(checked_at)
        if checked_at and latest_balance_dt is not None:
            current_latest_dt = group.get("latest_balance_dt")
            if current_latest_dt is None or latest_balance_dt > current_latest_dt:
                supplier_records = recharge_records_for_supplier(
                    store,
                    supplier_id,
                    provider_name=provider_name,
                )
                group["latest_balance"] = {
                    "checked_at": checked_at,
                    "status": row.get("balance_status"),
                    "adapter": row.get("balance_source"),
                    "currency": row.get("currency"),
                    "balance_total": row.get("balance_total"),
                    "balance_used": row.get("balance_used"),
                    "balance_remaining": row.get("balance_remaining"),
                    "raw_summary": row.get("balance_raw_summary") or "",
                    "supplier_recharge_records": supplier_records,
                }
                group["latest_balance_dt"] = latest_balance_dt
                group["quota_message"] = str(row.get("quota_message") or "")

    supplier_rows: list[dict[str, Any]] = []
    for group in grouped.values():
        rows = group["rows"]
        total_prompt = sum(int(item.get("prompt_tokens") or 0) for item in rows)
        total_completion = sum(int(item.get("completion_tokens") or 0) for item in rows)
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in rows)
        total_requests = sum(int(item.get("total_requests") or 0) for item in rows)
        success_count = sum(int(item.get("success_count") or 0) for item in rows)
        latency_sum = sum(int(item.get("latency_sum_ms") or 0) for item in rows)
        latency_count = sum(int(item.get("latency_count") or 0) for item in rows)
        estimated_cost = sum(float(item.get("estimated_cost") or 0) for item in rows)

        actual_cost = sum(float(item.get("actual_cost") or 0) for item in rows if item.get("actual_cost") is not None)
        actual_cost_count = sum(int(item.get("actual_cost_count") or 0) for item in rows)

        latest_balance = group.get("latest_balance") or {}
        supplier_recharge_records = list(latest_balance.get("supplier_recharge_records") or [])
        all_recharge_records = {str(item.get("id") or ""): item for item in supplier_recharge_records if item.get("id")}
        total_paid_cny = 0.0
        for item in rows:
            total_paid_cny += float(item.get("recharge_paid_cny") or 0)
        total_paid_cny += sum(
            float(item.get("paid_amount_cny") or 0)
            for item in supplier_recharge_records
            if not item.get("api_key_env")
        )

        effective_cny_per_1m_tokens = None
        tokens_per_cny = None
        if total_paid_cny > 0 and total_tokens > 0:
            effective_cny_per_1m_tokens = total_paid_cny / total_tokens * 1_000_000
            tokens_per_cny = total_tokens / total_paid_cny

        balance_total = latest_balance.get("balance_total")
        balance_used = latest_balance.get("balance_used")
        balance_remaining = latest_balance.get("balance_remaining")
        quota_message = quota_message_from_snapshot(latest_balance) or str(group.get("quota_message") or "")
        cycle_adjusted = apply_latest_recharge_cycle(
            supplier_recharge_records,
            balance_total=balance_total,
            balance_used=balance_used,
            balance_remaining=balance_remaining,
            quota_message=quota_message,
        )
        balance_total = cycle_adjusted.get("balance_total")
        balance_used = cycle_adjusted.get("balance_used")
        balance_remaining = cycle_adjusted.get("balance_remaining")
        percent_used = None
        if balance_total not in (None, "", 0) and balance_used not in (None, ""):
            try:
                percent_used = max(0.0, min(100.0, float(balance_used) / float(balance_total) * 100))
            except ZeroDivisionError:
                percent_used = None

        balance_source = str(latest_balance.get("adapter") or "")
        quota_message = cycle_adjusted.get("quota_message") or quota_message
        quota_breakdown = extract_snapshot_quota_breakdown(latest_balance)
        for item in rows:
            candidate = item.get("quota_breakdown")
            if isinstance(candidate, dict) and candidate:
                quota_breakdown = candidate
                break
        supplier_rows.append(
            {
                "supplier_id": group["supplier_id"],
                "supplier_name": group["supplier_name"] or "未分组供应商",
                "provider": group["provider"],
                "api_count": len(rows),
                "api_labels": group["api_labels"],
                "api_bases": [str(item.get("api_base") or "") for item in rows if str(item.get("api_base") or "").strip()],
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "estimated_cost": round(estimated_cost, 6),
                "actual_cost": round(actual_cost, 6) if actual_cost_count else None,
                "currency": str(latest_balance.get("currency") or rows[0].get("currency") or "CNY"),
                "success_rate": round(success_count / total_requests, 4) if total_requests else None,
                "avg_latency_ms": round(latency_sum / latency_count) if latency_count else None,
                "total_requests": total_requests,
                "balance_total": float(balance_total) if balance_total not in (None, "") else None,
                "balance_used": float(balance_used) if balance_used not in (None, "") else None,
                "balance_remaining": float(balance_remaining) if balance_remaining not in (None, "") else None,
                "percent_used": round(percent_used, 2) if percent_used is not None else None,
                "progress_tone": provider_progress_tone(percent_used),
                "recharge_paid_cny": round(total_paid_cny, 4),
                "effective_cny_per_1m_tokens": round(effective_cny_per_1m_tokens, 4) if effective_cny_per_1m_tokens is not None else None,
                "tokens_per_cny": round(tokens_per_cny, 4) if tokens_per_cny is not None else None,
                "balance_source": balance_source or ("manual" if any(item.get("quota_enabled") for item in rows) else "unknown"),
                "balance_checked_at": latest_balance.get("checked_at"),
                "balance_status": str(latest_balance.get("status") or "unknown"),
                "quota_message": quota_message or "按供应商共享额度聚合展示。",
                "quota_breakdown": quota_breakdown,
            }
        )
    return supplier_rows


def aggregate_model_usage_rows(management_state: dict[str, Any], store: Any) -> list[dict[str, Any]]:
    suppliers = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    profiles = {
        str(item.get("id") or ""): item
        for item in management_state.get("api_profiles", [])
        if isinstance(item, dict) and item.get("id")
    }
    routes = {
        str(item.get("id") or ""): item
        for item in management_state.get("model_routes", [])
        if isinstance(item, dict) and item.get("id")
    }
    bindings = [item for item in management_state.get("route_bindings", []) if isinstance(item, dict) and item.get("id")]
    usage_events_cache = {
        profile_id: usage_events_for_profile(store, profile, month_start_iso_local())
        for profile_id, profile in profiles.items()
    }

    rows: list[dict[str, Any]] = []
    for binding in bindings:
        route = routes.get(str(binding.get("model_route_id") or ""))
        profile = profiles.get(str(binding.get("api_profile_id") or ""))
        if not route or not profile:
            continue
        supplier = suppliers.get(str(profile.get("supplier_id") or ""), {})
        rows.append(
            summarize_binding_model_usage(
                route,
                binding,
                profile,
                supplier,
                usage_events_cache.get(str(profile.get("id") or ""), []),
            )
        )

    return sorted(
        rows,
        key=lambda item: (
            item.get("effective_cost_per_1m") if item.get("effective_cost_per_1m") is not None else float("inf"),
            -int(item.get("total_tokens") or 0),
            str(item.get("supplier_name") or ""),
            str(item.get("public_model_name") or ""),
            str(item.get("upstream_model") or ""),
        ),
    )


def usage_dashboard_snapshot(management_state: dict[str, Any]) -> dict[str, Any]:
    store = get_usage_quota_store()
    profiles = management_state.get("api_profiles", [])
    suppliers = {item["id"]: item for item in management_state.get("suppliers", []) if isinstance(item, dict) and item.get("id")}
    summary_rows: list[dict[str, Any]] = []
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_estimated_cost = 0.0
    total_actual_cost = 0.0
    total_paid_cny = 0.0
    alerts: list[dict[str, Any]] = []
    for profile in profiles:
        usage_events = usage_events_for_profile(store, profile, month_start_iso_local())
        recharge_records = recharge_records_for_profile(store, profile)
        latest_balance = latest_balance_for_profile(store, profile)
        row = summarize_profile_usage(profile, usage_events, recharge_records, latest_balance)
        supplier = suppliers.get(str(profile.get("supplier_id") or "").strip(), {})
        row.update(
            {
                "api_profile_id": profile.get("id"),
                "api_label": profile.get("label"),
                "supplier_id": profile.get("supplier_id"),
                "supplier_name": supplier.get("name") or supplier.get("label") or "未分组供应商",
                "provider": normalize_provider_name(profile.get("custom_llm_provider")),
                "api_base": str(profile.get("api_base") or ""),
                "known_models_count": len(profile.get("known_models") or []),
                "balance_source": (latest_balance or {}).get("adapter") or ("manual" if profile.get("quota") else "unknown"),
                "balance_checked_at": (latest_balance or {}).get("checked_at"),
                "balance_status": (latest_balance or {}).get("status") or "unknown",
                "balance_raw_summary": (latest_balance or {}).get("raw_summary"),
                "quota_message": row.get("cycle_quota_message") or quota_display_message(profile, latest_balance),
            }
        )
        if row.get("percent_used") is not None and row["percent_used"] >= 80:
            alerts.append(
                {
                    "level": "critical" if row["percent_used"] >= 95 else "warning",
                    "code": "quota-high",
                    "api_profile_id": profile.get("id"),
                    "message": f"{profile.get('label') or profile.get('id')} 额度使用 {row['percent_used']}%",
                }
            )
        total_prompt += row["prompt_tokens"]
        total_completion += row["completion_tokens"]
        total_tokens += row["total_tokens"]
        total_estimated_cost += float(row.get("estimated_cost") or 0)
        total_actual_cost += float(row.get("actual_cost") or 0)
        total_paid_cny += float(row.get("recharge_paid_cny") or 0)
        summary_rows.append(row)

    supplier_rows = aggregate_supplier_usage_rows(store, summary_rows)
    model_rows = aggregate_model_usage_rows(management_state, store)
    comparison_rows = sorted(
        model_rows,
        key=lambda item: (
            item.get("effective_cost_per_1m") if item.get("effective_cost_per_1m") is not None else float("inf"),
            -(item.get("success_rate") or 0),
        ),
    )
    return {
        "generated_at": now_iso_local(),
        "window": {
            "preset": "month_to_date",
            "start_at": month_start_iso_local(),
            "end_at": now_iso_local(),
        },
        "totals": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "estimated_cost": round(total_estimated_cost, 6),
            "actual_cost": round(total_actual_cost, 6) if total_actual_cost else None,
            "recharge_paid_cny": round(total_paid_cny, 4),
            "providers": len(summary_rows),
            "suppliers": len(supplier_rows),
            "api_profiles": len(summary_rows),
        },
        "providers": supplier_rows,
        "api_profiles": summary_rows,
        "model_rows": model_rows,
        "comparison": comparison_rows,
        "alerts": alerts,
    }


def portal_base_for_profile(profile: dict[str, Any]) -> str:
    quota = ensure_mapping(profile.get("quota"))
    portal_base = str(quota.get("portal_base") or "").strip().rstrip("/")
    if portal_base:
        return portal_base
    api_base = str(profile.get("api_base") or "").strip()
    parsed = urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def normalize_session_cookie_header(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "=" not in text and ";" not in text:
        return f"session={text}"
    return text


def recursive_find_key(obj: Any, aliases: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in aliases:
                return value
        for value in obj.values():
            found = recursive_find_key(value, aliases)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find_key(item, aliases)
            if found is not None:
                return found
    return None


def extract_portal_user_id(payload: Any) -> str:
    user_id = recursive_find_key(payload, {"id", "user_id"})
    return str(user_id or "").strip()


def normalize_portal_data(body: Any) -> Any:
    if isinstance(body, dict) and body.get("code") == 0 and "data" in body:
        return body.get("data")
    return body


def looks_like_html_document(body: Any) -> bool:
    if not isinstance(body, str):
        return False
    text = body.lstrip().lower()
    return text.startswith("<!doctype html") or text.startswith("<html")


def pick_numeric_from_payload(payload: Any, aliases: set[str]) -> float | None:
    value = recursive_find_key(payload, aliases)
    return numeric_value(value)


def extract_balance_snapshot_from_payloads(payloads: dict[str, Any], default_currency: str = "CNY") -> dict[str, Any]:
    prioritized = [
        "subscriptions_summary",
        "subscriptions_active",
        "subscriptions_progress",
        "balance",
        "platform_quotas",
        "usage",
        "balance_credits",
        "balance_history",
    ]
    total = used = remaining = None
    currency = default_currency
    for name in prioritized:
        payload = payloads.get(name)
        if payload is None:
            continue
        if total is None:
            total = pick_numeric_from_payload(
                payload,
                {
                    "total",
                    "limit",
                    "quota_total",
                    "total_quota",
                    "balance_total",
                    "total_amount",
                    "granted",
                    "credit_total",
                },
            )
        if remaining is None:
            remaining = pick_numeric_from_payload(
                payload,
                {
                    "remaining",
                    "available",
                    "balance",
                    "remaining_balance",
                    "current_balance",
                    "available_balance",
                    "credit_balance",
                },
            )
        if used is None:
            used = pick_numeric_from_payload(
                payload,
                {
                    "used",
                    "spent",
                    "consumed",
                    "usage",
                    "balance_used",
                    "used_amount",
                    "cost_used",
                },
            )
        found_currency = recursive_find_key(payload, {"currency", "unit"})
        if isinstance(found_currency, str) and found_currency.strip():
            currency = found_currency.strip()
        if total is not None and remaining is not None and used is not None:
            break

    if used is None and total is not None and remaining is not None:
        used = total - remaining
    if remaining is None and total is not None and used is not None:
        remaining = total - used

    return {
        "balance_total": total,
        "balance_used": used,
        "balance_remaining": remaining,
        "currency": currency or default_currency,
    }


def probe_custom_quota_script(profile: dict[str, Any]) -> dict[str, Any]:
    quota = ensure_mapping(profile.get("quota"))
    script_name = str(quota.get("script_name") or quota.get("script_path") or "").strip()
    timeout_sec = int(quota.get("script_timeout_sec") or 60)
    script_path = resolve_quota_adapter_script(script_name)
    adapter_name = "custom-script"
    if not script_name:
        return {
            "status": "unsupported",
            "adapter": adapter_name,
            "message": "缺少 script_name，请先在 quota-adapters 目录里放置脚本并填写脚本名。",
            "raw_summary": {"quota_adapters_dir": str(ensure_quota_adapters_dir())},
        }
    if script_path is None:
        return {
            "status": "unsupported",
            "adapter": adapter_name,
            "message": f"未找到额度脚本 {script_name}，请检查 quota-adapters 目录。",
            "raw_summary": {
                "quota_adapters_dir": str(ensure_quota_adapters_dir()),
                "requested_script": script_name,
            },
        }

    context = {
        "profile": {
            "id": str(profile.get("id") or ""),
            "label": str(profile.get("label") or ""),
            "supplier_id": str(profile.get("supplier_id") or ""),
            "api_base": str(profile.get("api_base") or ""),
            "api_key_env": str(profile.get("api_key_env") or ""),
            "api_key_value": str(profile.get("api_key_value") or ""),
            "custom_llm_provider": normalize_provider_name(profile.get("custom_llm_provider")),
            "quota": quota,
        },
        "window": {
            "start_at": month_start_iso_local(),
            "end_at": now_iso_local(),
        },
    }
    command: list[str]
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        python_exe = str(PYTHON_EXE if PYTHON_EXE.exists() else "python")
        command = [python_exe, str(script_path)]
    elif suffix == ".ps1":
        command = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    else:
        command = [str(script_path)]

    env = os.environ.copy()
    env["RELAYDECK_QUOTA_CONTEXT"] = json.dumps(context, ensure_ascii=False)
    # Force Python quota adapters to emit UTF-8 on Windows so non-ASCII fields
    # such as plan titles do not get mojibake when captured by the parent process.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(context, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5, timeout_sec),
            cwd=str(ensure_quota_adapters_dir()),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": f"额度脚本执行超时（>{timeout_sec}s）。",
            "raw_summary": {"script": str(script_path), "timeout_sec": timeout_sec},
        }
    except Exception as exc:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": f"额度脚本执行失败：{exc}",
            "raw_summary": {"script": str(script_path)},
        }

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    raw_summary: dict[str, Any] = {
        "script": str(script_path),
        "exit_code": completed.returncode,
        "stderr": stderr[:2000],
    }
    if completed.returncode != 0:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": f"额度脚本退出码异常：{completed.returncode}",
            "raw_summary": {**raw_summary, "stdout": stdout[:2000]},
        }
    if not stdout:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": "额度脚本没有输出 JSON 结果。",
            "raw_summary": raw_summary,
        }
    try:
        payload = json.loads(stdout)
    except Exception:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": "额度脚本输出不是合法 JSON。",
            "raw_summary": {**raw_summary, "stdout": stdout[:2000]},
        }
    if not isinstance(payload, dict):
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": "额度脚本输出必须是 JSON object。",
            "raw_summary": {**raw_summary, "stdout": stdout[:2000]},
        }

    return {
        "status": str(payload.get("status") or "unsupported"),
        "adapter": str(payload.get("adapter") or adapter_name),
        "message": str(payload.get("message") or ""),
        "balance_total": numeric_value(payload.get("balance_total")),
        "balance_used": numeric_value(payload.get("balance_used")),
        "balance_remaining": numeric_value(payload.get("balance_remaining")),
        "currency": str(payload.get("currency") or quota.get("currency") or "CNY"),
        "raw_summary": {
            **raw_summary,
            "script_output": payload.get("raw_summary") if isinstance(payload.get("raw_summary"), (dict, list, str)) else payload,
        },
    }


def probe_openai_compatible_balance(profile: dict[str, Any]) -> dict[str, Any]:
    api_key = str(profile.get("api_key_value") or "").strip()
    api_base = resolve_provider_api_base(profile.get("api_base"), profile.get("custom_llm_provider")).rstrip("/")
    quota = ensure_mapping(profile.get("quota"))
    default_currency = str(quota.get("currency") or "USD")

    if normalize_provider_name(profile.get("custom_llm_provider")) != "openai":
        return {
            "status": "unsupported",
            "adapter": "openai_compatible_balance",
            "message": "当前 provider 不是 openai 兼容类型。",
            "raw_summary": {"api_base": api_base},
        }
    if not api_base:
        return {
            "status": "unsupported",
            "adapter": "openai_compatible_balance",
            "message": "缺少 API Base，无法探测兼容余额接口。",
            "raw_summary": {},
        }
    if not api_key:
        return {
            "status": "unsupported",
            "adapter": "openai_compatible_balance",
            "message": "缺少 API Key，无法探测兼容余额接口。",
            "raw_summary": {"api_base": api_base},
        }

    start_date = month_start_iso_local()[:10]
    end_date = now_iso_local()[:10]
    raw_summary: dict[str, Any] = {
        "api_base": api_base,
        "adapter": "openai_compatible_balance",
        "window": {"start_date": start_date, "end_date": end_date},
    }
    payloads: dict[str, Any] = {}
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    endpoint_specs: list[tuple[str, str, dict[str, Any] | None]] = [
        ("billing_subscription", "/dashboard/billing/subscription", None),
        ("billing_usage", "/dashboard/billing/usage", {"start_date": start_date, "end_date": end_date}),
        ("balance", "/balance", None),
        ("models_probe", "/models", None),
        ("usage", "/api/v1/usage", None),
        ("subscriptions_summary", "/api/v1/subscriptions/summary", None),
    ]

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for name, path, params in endpoint_specs:
            url = f"{api_base}{path}"
            try:
                resp = client.get(url, headers=headers, params=params)
                content_type = str(resp.headers.get("content-type") or "").lower()
                try:
                    body = resp.json()
                except Exception:
                    body_text = resp.text[:500]
                    body = {"raw_text": body_text, "content_type": content_type}
                raw_summary[name] = {
                    "url": str(resp.request.url),
                    "status_code": resp.status_code,
                    "content_type": content_type,
                    "body": body,
                }
                if resp.status_code < 400:
                    payloads[name] = body
            except Exception as exc:
                raw_summary[name] = {"url": url, "error": str(exc)}

    subscription = ensure_mapping(payloads.get("billing_subscription"))
    usage_payload = ensure_mapping(payloads.get("billing_usage"))
    balance_payload = ensure_mapping(payloads.get("balance"))

    total = pick_numeric_from_payload(
        subscription,
        {"hard_limit_usd", "soft_limit_usd", "system_hard_limit_usd", "total", "limit", "quota_total"},
    )
    used = pick_numeric_from_payload(usage_payload, {"total_usage", "used", "usage", "spent", "consumed"})
    remaining = pick_numeric_from_payload(
        balance_payload,
        {"remaining", "available", "balance", "remaining_balance", "current_balance", "available_balance"},
    )

    if remaining is None and total is not None and used is not None:
        remaining = total - used
    if used is None and total is not None and remaining is not None:
        used = total - remaining

    fallback_snapshot = extract_balance_snapshot_from_payloads(payloads, default_currency)
    if total is None:
        total = fallback_snapshot.get("balance_total")
    if used is None:
        used = fallback_snapshot.get("balance_used")
    if remaining is None:
        remaining = fallback_snapshot.get("balance_remaining")

    currency = str(
        recursive_find_key(subscription, {"currency", "unit"})
        or recursive_find_key(usage_payload, {"currency", "unit"})
        or recursive_find_key(balance_payload, {"currency", "unit"})
        or fallback_snapshot.get("currency")
        or default_currency
    ).strip() or default_currency
    status = "ok" if any(value is not None for value in (total, used, remaining)) else "unsupported"
    message = ""
    if status != "ok":
        billing_subscription_body = ensure_mapping(raw_summary.get("billing_subscription", {}).get("body"))
        billing_usage_body = ensure_mapping(raw_summary.get("billing_usage", {}).get("body"))
        models_probe_body = ensure_mapping(raw_summary.get("models_probe", {}).get("body"))
        portal_usage_body = ensure_mapping(raw_summary.get("usage", {}).get("body"))
        subscriptions_summary_body = ensure_mapping(raw_summary.get("subscriptions_summary", {}).get("body"))

        html_shell_detected = any(
            looks_like_html_document(str(ensure_mapping(raw_summary.get(name, {}).get("body")).get("raw_text") or ""))
            for name in ("billing_subscription", "billing_usage", "balance")
        )
        model_probe_code = str(models_probe_body.get("code") or "")
        model_probe_message = str(models_probe_body.get("message") or "")
        portal_usage_code = str(portal_usage_body.get("code") or "")
        subscriptions_code = str(subscriptions_summary_body.get("code") or "")

        if model_probe_code == "GROUP_DELETED" or "分组已删除" in model_probe_message:
            message = "当前 API Key 所属分组已删除，请先更换灵算站内仍然有效的 API Key。"
            if html_shell_detected or portal_usage_code == "INVALID_TOKEN" or subscriptions_code == "INVALID_TOKEN":
                message += " 另外，灵算额度接口不是标准 OpenAI 账单 JSON，通常还需要站点后台 auth_token。"
        elif portal_usage_code == "INVALID_TOKEN" or subscriptions_code == "INVALID_TOKEN":
            message = "灵算的额度接口需要站点后台 auth_token，不能直接用 API Key 查询余额。"
        elif html_shell_detected:
            message = "灵算返回的是前台 HTML 页面，不是可直接读取的账单 JSON；这条链路通常要改用站点后台 auth_token。"
        else:
            message = "API Key 可访问兼容账单端点，但暂未识别出统一余额字段。"

    return {
        "status": status,
        "adapter": "openai_compatible_balance",
        "message": message,
        "raw_summary": raw_summary,
        "balance_total": total,
        "balance_used": used,
        "balance_remaining": remaining,
        "currency": currency,
    }


def probe_portal_quota(profile: dict[str, Any]) -> dict[str, Any]:
    quota = ensure_mapping(profile.get("quota"))
    adapter = effective_quota_adapter(profile)
    portal_base = portal_base_for_profile(profile)
    default_currency = str(quota.get("currency") or "CNY")
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = normalize_session_cookie_header(quota.get("session_cookie"))
    admin_api_key = str(quota.get("admin_api_key") or "").strip()
    configured_user_id = str(quota.get("user_id") or "").strip()

    if not portal_base:
        return {
            "status": "unsupported",
            "adapter": adapter or "portal_web_token",
            "message": "缺少 portal_base，无法定位网站后台接口。",
            "raw_summary": {},
        }

    headers: dict[str, str] = {"Accept": "application/json"}
    auth_mode = ""
    if adapter in {"openai-compatible", "openai_compatible_balance", "provider_api"}:
        return probe_openai_compatible_balance(profile)
    if adapter in {"lingsuan-web", "autocode-web", "portal_web_token"}:
        if not auth_token and not session_cookie:
            return {
                "status": "unsupported",
                "adapter": adapter,
                "message": "缺少网站后台 auth_token / session_cookie。请先在浏览器登录站点后台，再把其中一项填到面板里。",
                "raw_summary": {"portal_base": portal_base},
            }
        auth_mode = "website_token"
    elif adapter in {"lingsuan-admin", "autocode-admin", "portal_admin_key"}:
        if not admin_api_key:
            return {
                "status": "unsupported",
                "adapter": adapter,
                "message": "缺少后台 admin API key。",
                "raw_summary": {"portal_base": portal_base},
            }
        headers["x-api-key"] = admin_api_key
        auth_mode = "admin_api_key"
    else:
        return {
            "status": "unsupported",
            "adapter": adapter or "manual",
            "message": "当前 adapter 不是自动额度探测类型。",
            "raw_summary": {"portal_base": portal_base},
        }

    raw_summary: dict[str, Any] = {
        "portal_base": portal_base,
        "auth_mode": auth_mode,
        "adapter": adapter,
        "session_cookie_configured": bool(session_cookie),
    }

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        user_id = configured_user_id
        if auth_mode == "website_token":
            me_url = f"{portal_base}/api/v1/auth/me"
            auth_attempts: list[tuple[str, dict[str, str]]] = []
            if auth_token:
                auth_attempts.append(("website_token", {**headers, "Authorization": f"Bearer {auth_token}"}))
            if session_cookie:
                auth_attempts.append(("website_session_cookie", {**headers, "Cookie": session_cookie}))

            selected_headers: dict[str, str] | None = None
            last_error_message = ""
            for attempt_mode, attempt_headers in auth_attempts:
                try:
                    me_resp = client.get(me_url, headers=attempt_headers)
                    try:
                        me_body = me_resp.json()
                    except Exception:
                        me_body = {"raw_text": me_resp.text[:500]}
                    raw_summary[f"auth_me_{attempt_mode}"] = {
                        "status_code": me_resp.status_code,
                        "body": me_body,
                    }
                    if me_resp.status_code < 400:
                        auth_mode = attempt_mode
                        selected_headers = attempt_headers
                        user_id = user_id or extract_portal_user_id(normalize_portal_data(me_body))
                        break
                    if me_resp.status_code == 401:
                        last_error_message = f"{attempt_mode} returned HTTP 401"
                    else:
                        last_error_message = f"{attempt_mode} returned HTTP {me_resp.status_code}"
                except Exception as exc:
                    raw_summary[f"auth_me_{attempt_mode}"] = {
                        "error": str(exc),
                    }
                    last_error_message = str(exc)

            if selected_headers is None:
                hint = "请重新获取登录态。"
                if session_cookie:
                    hint = "auth_token 和 session_cookie 都失效了，请重新登录后获取新的登录态。"
                return {
                    "status": "error",
                    "adapter": adapter,
                    "message": f"后台登录态无效：{last_error_message or 'HTTP 401'} {hint}".strip(),
                    "raw_summary": raw_summary,
                }
            headers = selected_headers
            raw_summary["auth_mode"] = auth_mode

        endpoint_specs: list[tuple[str, str, dict[str, Any] | None]] = [
            ("subscriptions_summary", "/api/v1/subscriptions/summary", None),
            ("subscriptions_active", "/api/v1/subscriptions/active", None),
            ("subscriptions_progress", "/api/v1/subscriptions/progress", None),
            ("usage", "/api/v1/usage", None),
            ("balance", "/api/v1/balance", None),
        ]

        if user_id:
            endpoint_specs.extend(
                [
                    ("admin_usage", f"/api/v1/admin/users/{user_id}/usage", {"period": "month"}),
                    ("balance_history", f"/api/v1/admin/users/{user_id}/balance-history", {"page": 1, "page_size": 20}),
                    ("balance_credits", f"/api/v1/admin/users/{user_id}/balance-credits", {"limit": 20}),
                    ("platform_quotas", f"/api/v1/admin/users/{user_id}/platform-quotas", None),
                ]
            )

        payloads: dict[str, Any] = {}
        for name, path, params in endpoint_specs:
            url = f"{portal_base}{path}"
            try:
                resp = client.get(url, headers=headers, params=params)
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw_text": resp.text[:500]}
                raw_summary[name] = {
                    "url": url,
                    "status_code": resp.status_code,
                    "body": body,
                }
                if resp.status_code < 400:
                    payloads[name] = normalize_portal_data(body)
            except Exception as exc:
                raw_summary[name] = {
                    "url": url,
                    "error": str(exc),
                }

    snapshot = extract_balance_snapshot_from_payloads(payloads, default_currency)
    status = "ok" if any(snapshot.get(key) is not None for key in ("balance_total", "balance_used", "balance_remaining")) else "unsupported"
    message = "" if status == "ok" else "接口可访问，但暂未识别出统一的余额字段。可以继续根据该站点真实返回结构做专门适配。"
    if auth_mode == "admin_api_key" and not configured_user_id:
        message = "admin key 模式下建议同时填写 user_id，否则很多用户级余额接口无法调用。"
        status = "unsupported"

    return {
        "status": status,
        "adapter": adapter,
        "message": message,
        "user_id": user_id,
        "raw_summary": raw_summary,
        **snapshot,
    }


def portal_request_headers(profile: dict[str, Any]) -> tuple[dict[str, str], str]:
    quota = ensure_mapping(profile.get("quota"))
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = normalize_session_cookie_header(quota.get("session_cookie"))
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "RelayDeck-Local/1.0",
    }
    if auth_token:
        return {**headers, "Authorization": f"Bearer {auth_token}"}, "website_token"
    if session_cookie:
        return {**headers, "Cookie": session_cookie}, "website_session_cookie"
    return headers, ""


def request_portal_json_with_fallback(
    client: httpx.Client,
    profile: dict[str, Any],
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    quota = ensure_mapping(profile.get("quota"))
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = normalize_session_cookie_header(quota.get("session_cookie"))
    attempts: list[tuple[str, dict[str, str]]] = []
    base_headers = {
        "Accept": "application/json",
        "User-Agent": "RelayDeck-Local/1.0",
    }
    if auth_token:
        attempts.append(("website_token", {**base_headers, "Authorization": f"Bearer {auth_token}"}))
    if session_cookie:
        attempts.append(("website_session_cookie", {**base_headers, "Cookie": session_cookie}))
    if not attempts:
        return None, {
            "status": "error",
            "message": "缺少 auth_token 或 session_cookie。",
            "attempts": [],
        }

    trace: list[dict[str, Any]] = []
    for auth_mode, headers in attempts:
        try:
            resp = client.get(url, headers=headers, params=params)
            try:
                body = resp.json()
            except Exception:
                body = {"raw_text": resp.text[:500]}
            trace.append(
                {
                    "auth_mode": auth_mode,
                    "status_code": resp.status_code,
                    "body": body,
                }
            )
            raw_text = ""
            if isinstance(body, dict):
                raw_text = str(body.get("raw_text") or "")
            if looks_like_html_document(raw_text):
                continue
            if resp.status_code < 400:
                return normalize_portal_data(body), {
                    "status": "ok",
                    "auth_mode": auth_mode,
                    "attempts": trace,
                }
        except Exception as exc:
            trace.append(
                {
                    "auth_mode": auth_mode,
                    "error": str(exc),
                }
            )
    return None, {
        "status": "error",
        "message": "站点登录态无效，或订单接口返回了前台登录页而不是 JSON。",
        "attempts": trace,
    }


def normalize_portal_order_items(payload: Any) -> list[dict[str, Any]]:
    normalized = normalize_portal_data(payload)
    if isinstance(normalized, list):
        return [item for item in normalized if isinstance(item, dict)]
    if isinstance(normalized, dict):
        for key in ("items", "list", "orders", "records", "data"):
            value = normalized.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested_data = normalized.get("data")
        if isinstance(nested_data, dict):
            for key in ("items", "list", "orders", "records"):
                value = nested_data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
    return []


def portal_order_status_text(order: dict[str, Any]) -> str:
    for key in ("status", "order_status", "payment_status"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def portal_order_is_completed(order: dict[str, Any]) -> bool:
    text = portal_order_status_text(order).upper()
    return text in {"COMPLETED", "SUCCESS", "PAID", "FINISHED"} or text in {"completed", "success", "paid", "finished"}


def portal_order_trade_no(order: dict[str, Any]) -> str:
    for key in ("outTradeNo", "out_trade_no", "trade_no", "tradeNo", "order_no", "orderNo"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value).strip()
    order_id = order.get("id")
    return str(order_id).strip() if order_id not in (None, "") else ""


def portal_order_paid_at(order: dict[str, Any]) -> str:
    for key in ("paid_at", "completed_at", "created_at", "createdAt", "paidAt"):
        value = order.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
            except Exception:
                continue
        return str(value).strip()
    return now_iso_local()


def portal_order_paid_amount(order: dict[str, Any]) -> float | None:
    for key in ("payAmount", "pay_amount", "amount", "paid_amount", "total_amount"):
        value = numeric_value(order.get(key))
        if value is not None:
            return value
    return None


def sync_supplier_orders_from_portal(
    store: Any,
    supplier: dict[str, Any],
    profile: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    portal_base = portal_base_for_profile(profile)
    if not portal_base:
        return {
            "ok": False,
            "message": "缺少 portal_base，无法定位订单接口。",
            "inserted": 0,
            "skipped": 0,
            "items": [],
        }

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        payload, trace = request_portal_json_with_fallback(
            client,
            profile,
            f"{portal_base}/payment/orders/my",
            params={"page": 1, "page_size": max(1, min(limit, 100))},
        )

    if payload is None:
        return {
            "ok": False,
            "message": trace.get("message") or "订单接口不可访问。",
            "inserted": 0,
            "skipped": 0,
            "items": [],
            "trace": trace,
        }

    orders = normalize_portal_order_items(payload)
    existing_records = recharge_records_for_supplier(
        store,
        str(supplier.get("id") or ""),
        provider_name=normalize_provider_name(supplier.get("custom_llm_provider")),
    )
    existing_notes = {str(item.get("note") or "") for item in existing_records}

    inserted = 0
    skipped = 0
    synced_items: list[dict[str, Any]] = []
    provider_name = normalize_provider_name(supplier.get("custom_llm_provider"))

    for order in orders:
        if not portal_order_is_completed(order):
            skipped += 1
            continue
        paid_amount = portal_order_paid_amount(order)
        if paid_amount is None or paid_amount <= 0:
            skipped += 1
            continue
        trade_no = portal_order_trade_no(order)
        status_text = portal_order_status_text(order) or "COMPLETED"
        note = f"[portal-order-sync] trade_no={trade_no or '-'} status={status_text}"
        if note in existing_notes:
            skipped += 1
            continue

        payload_row = {
            "provider_key": supplier_provider_key(supplier.get("id"), provider_name),
            "relay_label": str(supplier.get("name") or supplier.get("label") or ""),
            "api_key_env": "",
            "paid_at": portal_order_paid_at(order),
            "paid_amount_cny": paid_amount,
            "credited_amount": None,
            "credited_currency": "CNY",
            "bonus_amount": None,
            "bonus_currency": None,
            "balance_after_recharge": None,
            "note": note,
        }
        record = store.insert_recharge_record(payload_row)
        inserted += 1
        existing_notes.add(note)
        synced_items.append({"order": order, "record": record})

    return {
        "ok": True,
        "message": f"订单同步完成：新增 {inserted} 条，跳过 {skipped} 条。",
        "inserted": inserted,
        "skipped": skipped,
        "items": synced_items,
        "trace": trace,
    }


def cookie_header_from_client(client: httpx.Client) -> str:
    parts: list[str] = []
    for key, value in client.cookies.items():
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if not key_text:
            continue
        parts.append(f"{key_text}={value_text}")
    return "; ".join(parts)


def save_management_state_profile_quota_fields(
    profile_id: str,
    quota_updates: dict[str, Any],
) -> None:
    management_state = load_management_state()
    api_profiles = list(management_state.get("api_profiles", []))
    updated = False
    for item in api_profiles:
        if str(item.get("id") or "").strip() != str(profile_id or "").strip():
            continue
        quota = ensure_mapping(item.get("quota"))
        quota.update({key: value for key, value in quota_updates.items() if value not in (None, "")})
        item["quota"] = quota
        updated = True
        break
    if not updated:
        return
    save_management_state(
        management_state.get("suppliers", []),
        api_profiles,
        management_state.get("model_routes", []),
        management_state.get("route_bindings", []),
        management_state.get("model_families", []),
        management_state.get("router_settings", {}),
        management_state.get("litellm_settings", {}),
        management_state.get("routing_view_mode", "by_model"),
    )


def repair_portal_login_state(
    profile: dict[str, Any],
    *,
    totp_code: str = "",
) -> dict[str, Any]:
    quota = ensure_mapping(profile.get("quota"))
    portal_base = portal_base_for_profile(profile)
    if not portal_base:
        return {
            "ok": False,
            "message": "缺少 portal_base，无法发起自动登录修复。",
        }

    login_email = str(quota.get("login_email") or "").strip()
    login_password = str(quota.get("login_password") or "").strip()
    if not login_email or not login_password:
        return {
            "ok": False,
            "message": "请先填写登录邮箱和登录密码，再执行自动登录修复。",
        }

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        public_settings_url = f"{portal_base}/api/v1/settings/public"
        public_settings_resp = client.get(public_settings_url, headers={"Accept": "application/json"})
        public_settings_body: dict[str, Any]
        try:
            public_settings_body = public_settings_resp.json()
        except Exception:
            public_settings_body = {"raw_text": public_settings_resp.text[:500]}
        normalized_public_settings = normalize_portal_data(public_settings_body)
        if ensure_mapping(normalized_public_settings).get("turnstile_enabled") is True:
            return {
                "ok": False,
                "message": "当前站点登录启用了验证码，自动登录修复暂不支持无人工介入完成。",
                "trace": {
                    "public_settings": public_settings_body,
                },
            }

        login_payload = {
            "email": login_email,
            "password": login_password,
        }
        login_resp = client.post(
            f"{portal_base}/api/v1/auth/login",
            json=login_payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            login_body = login_resp.json()
        except Exception:
            login_body = {"raw_text": login_resp.text[:500]}

        if login_resp.status_code >= 400:
            return {
                "ok": False,
                "message": f"自动登录失败：HTTP {login_resp.status_code}",
                "trace": {
                    "login": {
                        "status_code": login_resp.status_code,
                        "body": login_body,
                    }
                },
            }

        response_payload = normalize_portal_data(login_body) if isinstance(login_body, dict) else {}
        if not isinstance(response_payload, dict):
            response_payload = {}
        access_token = str(response_payload.get("access_token") or "").strip()
        refresh_token = str(response_payload.get("refresh_token") or "").strip()
        user_email_masked = str(response_payload.get("user_email_masked") or "").strip()
        requires_2fa = bool(response_payload.get("requires_2fa"))

        if requires_2fa:
            temp_token = str(response_payload.get("temp_token") or "").strip()
            if not temp_token:
                return {
                    "ok": False,
                    "message": "站点要求二步验证，但没有返回 temp_token。",
                    "trace": {
                        "login": {
                            "status_code": login_resp.status_code,
                            "body": login_body,
                        }
                    },
                }
            if not str(totp_code or "").strip():
                return {
                    "ok": False,
                    "message": f"站点要求二步验证，请补充 TOTP 验证码。账号：{user_email_masked or login_email}",
                    "requires_2fa": True,
                    "temp_token": temp_token,
                    "user_email_masked": user_email_masked,
                }
            login2fa_resp = client.post(
                f"{portal_base}/api/v1/auth/login/2fa",
                json={"temp_token": temp_token, "totp_code": str(totp_code or "").strip()},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            try:
                login2fa_body = login2fa_resp.json()
            except Exception:
                login2fa_body = {"raw_text": login2fa_resp.text[:500]}
            if login2fa_resp.status_code >= 400:
                return {
                    "ok": False,
                    "message": f"二步验证失败：HTTP {login2fa_resp.status_code}",
                    "trace": {
                        "login": {
                            "status_code": login_resp.status_code,
                            "body": login_body,
                        },
                        "login_2fa": {
                            "status_code": login2fa_resp.status_code,
                            "body": login2fa_body,
                        },
                    },
                }
            response_payload = normalize_portal_data(login2fa_body) if isinstance(login2fa_body, dict) else {}
            if not isinstance(response_payload, dict):
                response_payload = {}
            access_token = str(response_payload.get("access_token") or "").strip()
            refresh_token = str(response_payload.get("refresh_token") or "").strip()

        session_cookie = cookie_header_from_client(client)
        if not access_token and not session_cookie:
            return {
                "ok": False,
                "message": "自动登录完成，但没有拿到新的 auth_token 或 session_cookie。",
                "trace": {
                    "login": {
                        "status_code": login_resp.status_code,
                        "body": login_body,
                    }
                },
            }

        me_headers = {"Accept": "application/json"}
        if access_token:
            me_headers["Authorization"] = f"Bearer {access_token}"
        elif session_cookie:
            me_headers["Cookie"] = session_cookie
        me_resp = client.get(f"{portal_base}/api/v1/auth/me", headers=me_headers)
        try:
            me_body = me_resp.json()
        except Exception:
            me_body = {"raw_text": me_resp.text[:500]}

        quota_updates = {
            "login_email": login_email,
            "login_password": login_password,
            "auth_token": access_token,
            "session_cookie": session_cookie,
        }
        if refresh_token:
            quota_updates["refresh_token"] = refresh_token
        save_management_state_profile_quota_fields(str(profile.get("id") or ""), quota_updates)

        return {
            "ok": True,
            "message": "自动登录修复完成，已更新 auth_token / session_cookie。",
            "updated": {
                "auth_token": bool(access_token),
                "session_cookie": bool(session_cookie),
                "refresh_token": bool(refresh_token),
            },
            "trace": {
                "login": {
                    "status_code": login_resp.status_code,
                    "body": login_body,
                },
                "auth_me": {
                    "status_code": me_resp.status_code,
                    "body": me_body,
                },
            },
        }


def refresh_balances_v2(payload: "BalanceRefreshPayload") -> dict[str, Any]:
    management_state = load_management_state()
    profiles = [item for item in management_state.get("api_profiles", []) if isinstance(item, dict)]
    if payload.api_profile_ids:
        allowed = set(payload.api_profile_ids)
        profiles = [item for item in profiles if item.get("id") in allowed]

    store = get_usage_quota_store()
    results: list[dict[str, Any]] = []

    for profile in profiles:
        quota = ensure_mapping(profile.get("quota"))
        adapter = effective_quota_adapter(profile)
        snapshot_adapter = adapter
        currency = str(quota.get("currency") or "CNY")
        auto_probe: dict[str, Any] | None = None
        provider_name = normalize_provider_name(profile.get("custom_llm_provider"))
        can_probe_openai_compatible = (
            provider_name == "openai"
            and str(profile.get("api_key_value") or "").strip()
            and str(profile.get("api_base") or "").strip()
        )
        should_probe_openai_compatible = adapter in {"openai-compatible", "openai_compatible_balance", "provider_api"} or (
            adapter == "manual"
            and can_probe_openai_compatible
            and not quota_has_manual_values(quota)
        )

        if adapter in {"custom-script", "custom_script", "script"}:
            auto_probe = probe_custom_quota_script(profile)
            snapshot_adapter = str(auto_probe.get("adapter") or "custom-script")
            limit = auto_probe.get("balance_total")
            current_balance = auto_probe.get("balance_remaining")
            used_amount = auto_probe.get("balance_used")
            currency = str(auto_probe.get("currency") or currency)
            status = str(auto_probe.get("status") or "unsupported")
            message = str(auto_probe.get("message") or "")
        elif should_probe_openai_compatible:
            auto_probe = probe_openai_compatible_balance(profile)
            snapshot_adapter = str(auto_probe.get("adapter") or "openai_compatible_balance")
            limit = auto_probe.get("balance_total")
            current_balance = auto_probe.get("balance_remaining")
            used_amount = auto_probe.get("balance_used")
            currency = str(auto_probe.get("currency") or currency)
            status = str(auto_probe.get("status") or "unsupported")
            message = str(auto_probe.get("message") or "")
        elif adapter in {"lingsuan-web", "lingsuan-admin", "autocode-web", "autocode-admin", "portal_web_token", "portal_admin_key"}:
            auto_probe = probe_portal_quota(profile)
            snapshot_adapter = str(auto_probe.get("adapter") or adapter)
            limit = auto_probe.get("balance_total")
            current_balance = auto_probe.get("balance_remaining")
            used_amount = auto_probe.get("balance_used")
            currency = str(auto_probe.get("currency") or currency)
            status = str(auto_probe.get("status") or "unsupported")
            message = str(auto_probe.get("message") or "")
        else:
            limit = quota.get("limit")
            current_balance = quota.get("current_balance")
            used_amount = quota.get("used_amount")
            if current_balance in (None, "") and limit not in (None, "") and used_amount not in (None, ""):
                current_balance = float(limit) - float(used_amount)
            if used_amount in (None, "") and limit not in (None, "") and current_balance not in (None, ""):
                used_amount = float(limit) - float(current_balance)
            status = "ok" if limit not in (None, "") or current_balance not in (None, "") else "unsupported"
            message = "" if status == "ok" else "尚未配置可刷新的额度数据，当前仅支持手动额度。"

        snapshot = store.insert_provider_balance_snapshot(
            {
                "provider_key": profile_provider_key(profile),
                "relay_label": str(profile.get("label") or ""),
                "api_key_env": str(profile.get("api_key_env") or ""),
                "adapter": snapshot_adapter,
                "status": status,
                "currency": currency,
                "balance_total": float(limit) if limit not in (None, "") else None,
                "balance_used": float(used_amount) if used_amount not in (None, "") else None,
                "balance_remaining": float(current_balance) if current_balance not in (None, "") else None,
                "period_start": str(quota.get("period_start") or ""),
                "period_end": str(quota.get("period_end") or ""),
                "raw_summary": (
                    {
                        "message": str(auto_probe.get("message") or ""),
                        "status": str(auto_probe.get("status") or status),
                        "adapter": str(auto_probe.get("adapter") or snapshot_adapter),
                        "raw_summary": auto_probe.get("raw_summary"),
                    }
                    if auto_probe is not None
                    else {
                        "source": "manual-quota-config",
                        "force": payload.force,
                    }
                ),
            }
        )

        results.append(
            {
                "api_profile_id": profile.get("id"),
                "label": profile.get("label"),
                "status": status,
                "source": "auto" if auto_probe is not None else "manual",
                "adapter": snapshot_adapter,
                "currency": snapshot.get("currency"),
                "balance_total": snapshot.get("balance_total"),
                "balance_used": snapshot.get("balance_used"),
                "balance_remaining": snapshot.get("balance_remaining"),
                "period_start": snapshot.get("period_start"),
                "period_end": snapshot.get("period_end"),
                "checked_at": snapshot.get("checked_at"),
                "message": message,
            }
        )

    return {"ok": True, "refreshed_at": now_iso_local(), "results": results}


def model_name_for_direct_request(upstream_model: str, custom_llm_provider: str | None) -> str:
    provider = normalize_provider_name(custom_llm_provider)
    if "/" in upstream_model:
        prefix, suffix = upstream_model.split("/", 1)
        if prefix.lower() == provider and suffix:
            return suffix
    return upstream_model


def provider_models_probe(provider: dict[str, Any], env_map: dict[str, str]) -> tuple[str, dict[str, str]]:
    provider_name = normalize_provider_name(provider.get("custom_llm_provider"))
    api_base = resolve_provider_api_base(provider.get("api_base"), provider_name)
    api_key = env_map.get(provider.get("api_key_env", ""), "").strip()
    if provider_name == "anthropic":
        return (
            f"{api_base}/v1/models",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
    return (
        f"{api_base}/models",
        {
            "Authorization": f"Bearer {api_key}",
        },
    )


def build_direct_request(
    api_base: str,
    api_key: str,
    custom_llm_provider: str,
    upstream_model: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    provider_name = normalize_provider_name(custom_llm_provider)
    model_name = model_name_for_direct_request(upstream_model, provider_name)
    if provider_name == "anthropic":
        return (
            f"{api_base}/v1/messages",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            {
                "model": model_name,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    return (
        f"{api_base}/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
        },
    )


def build_preflight_report(
    providers: list[dict[str, Any]],
    env_map: dict[str, str],
    router_settings: dict[str, Any],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not providers:
        items.append({"level": "error", "code": "no-providers", "message": "当前没有任何已配置模型。"})

    enabled = [item for item in providers if item.get("enabled", True)]
    if not enabled:
        items.append({"level": "error", "code": "no-enabled-providers", "message": "当前没有启用中的模型路由。"})

    if is_placeholder_secret(env_map.get("LITELLM_MASTER_KEY")):
        items.append(
            {
                "level": "error",
                "code": "weak-master-key",
                "message": "LITELLM_MASTER_KEY 仍是默认占位值，正式使用前必须更换。",
            }
        )
    if is_placeholder_secret(env_map.get("OPEN_WEBUI_SECRET_KEY")):
        items.append(
            {
                "level": "error",
                "code": "weak-webui-secret",
                "message": "OPEN_WEBUI_SECRET_KEY 仍是默认占位值，正式使用前必须更换。",
            }
        )

    cors_value = (env_map.get("CORS_ALLOW_ORIGIN") or "").strip()
    if cors_value == "*":
        items.append(
            {
                "level": "warning",
                "code": "wildcard-cors",
                "message": "CORS_ALLOW_ORIGIN 当前为 '*'，本地自托管建议收敛到明确来源。",
            }
        )

    if not (env_map.get("USER_AGENT") or "").strip():
        items.append(
            {
                "level": "warning",
                "code": "missing-user-agent",
                "message": "未设置 USER_AGENT；外部请求排障与识别会较弱。",
            }
        )

    seen_priorities: set[tuple[str, int]] = set()
    for provider in enabled:
        model_name = str(provider.get("model_name", "")).strip()
        upstream_model = str(provider.get("upstream_model", "")).strip()
        key_name = str(provider.get("api_key_env", "")).strip()
        key_value = env_map.get(key_name, "").strip()
        priority = int(provider.get("priority", 100))

        if not model_name or not upstream_model:
            items.append(
                {
                    "level": "error",
                    "code": "invalid-provider",
                    "message": f"模型 {model_name or '(未命名)'} 缺少 model_name 或 upstream_model。",
                }
            )
        if not key_name:
            items.append(
                {
                    "level": "error",
                    "code": "missing-api-key-env",
                    "message": f"模型 {model_name or upstream_model} 缺少 API Key 环境变量名。",
                }
            )
        elif not key_value:
            items.append(
                {
                    "level": "error",
                    "code": "missing-api-key-value",
                    "message": f"模型 {model_name or upstream_model} 的环境变量 {key_name} 为空。",
                }
            )

        priority_key = (model_name, priority)
        if priority_key in seen_priorities:
            items.append(
                {
                    "level": "warning",
                    "code": "duplicate-priority",
                    "message": f"模型 {model_name} 存在重复 priority={priority}，回退顺序会不稳定。",
                }
            )
        seen_priorities.add(priority_key)

    if router_settings.get("routing_strategy") != "simple-shuffle":
        items.append(
            {
                "level": "info",
                "code": "routing-strategy",
                "message": f"当前 routing_strategy={router_settings.get('routing_strategy')}",
            }
        )

    return items


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return text or "relay"


def make_stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part).strip() for part in parts if str(part).strip())
    return f"{prefix}_{slugify(raw) or 'item'}"


def guess_supplier_name(relay_label: str, custom_llm_provider: str, api_base: str) -> str:
    if relay_label.strip():
        return relay_label.strip()
    if api_base.strip():
        parsed = urlparse(api_base.strip())
        if parsed.netloc:
            return parsed.netloc
    provider_name = normalize_provider_name(custom_llm_provider)
    return provider_name or "supplier"


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def normalize_suppliers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        name = str(row.get("name") or row.get("label") or f"Supplier {index + 1}").strip()
        supplier_id = str(row.get("id") or make_stable_id("supplier", name, index)).strip()
        if supplier_id in seen:
            supplier_id = make_stable_id("supplier", supplier_id, index)
        seen.add(supplier_id)
        items.append(
            {
                **row,
                "id": supplier_id,
                "name": name,
                "label": str(row.get("label") or name).strip(),
                "custom_llm_provider": normalize_provider_name(row.get("custom_llm_provider")),
                "enabled": bool(row.get("enabled", True)),
            }
        )
    return items


def normalize_api_profiles(rows: list[dict[str, Any]], suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    supplier_ids = {item["id"] for item in suppliers}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = str(row.get("label") or row.get("relay_label") or f"API {index + 1}").strip()
        supplier_id = str(row.get("supplier_id") or "").strip()
        if supplier_id not in supplier_ids and suppliers:
            supplier_id = suppliers[0]["id"]
        profile_id = str(
            row.get("id")
            or make_stable_id("api", supplier_id or label, label, row.get("api_base", ""), normalize_provider_name(row.get("custom_llm_provider")), index)
        ).strip()
        api_key_env, api_key_value = extract_api_key_fields(row, profile_id)
        if profile_id in seen:
            profile_id = make_stable_id("api", profile_id, index)
        seen.add(profile_id)
        items.append(
            {
                **row,
                "id": profile_id,
                "supplier_id": supplier_id,
                "label": label,
                "api_base": str(row.get("api_base") or "").strip(),
                "api_key_env": api_key_env,
                "api_key_value": api_key_value,
                "custom_llm_provider": normalize_provider_name(row.get("custom_llm_provider")),
                "known_models": [str(model).strip() for model in ensure_list(row.get("known_models")) if str(model).strip()],
                "pricing": ensure_mapping(row.get("pricing")),
                "quota": ensure_mapping(row.get("quota")),
                "accounting": ensure_mapping(row.get("accounting")),
                "usage": ensure_mapping(row.get("usage")),
                "enabled": bool(row.get("enabled", True)),
            }
        )
    return items


DEFAULT_MODEL_FAMILIES = [
    "GPT 系列",
    "Claude 系列",
    "GLM 系列",
    "DeepSeek 系列",
    "Gemini 系列",
    "Image 系列",
    "Banana 系列",
    "Grok 系列",
    "Mimo 系列",
]


def normalize_model_families(rows: list[Any] | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in rows or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append(name)
    return items


def guess_model_family(model_name: str) -> str:
    name = str(model_name or "").strip().lower()
    if not name:
        return ""
    if name.startswith(("gpt-image", "image", "dall-e")):
        return "Image 系列"
    if name.startswith("banana"):
        return "Banana 系列"
    if name.startswith("grok"):
        return "Grok 系列"
    if name.startswith("mimo"):
        return "Mimo 系列"
    if name.startswith(("gpt", "chatgpt", "codex", "o1", "o3", "o4")):
        return "GPT 系列"
    if name.startswith(("claude", "opus", "sonnet", "haiku")):
        return "Claude 系列"
    if name.startswith("glm"):
        return "GLM 系列"
    if name.startswith("deepseek"):
        return "DeepSeek 系列"
    if name.startswith("gemini"):
        return "Gemini 系列"
    return ""


def normalize_model_routes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        public_model_name = str(row.get("public_model_name") or row.get("model_name") or row.get("display_name") or f"model-{index + 1}").strip()
        route_id = str(row.get("id") or make_stable_id("route", public_model_name)).strip()
        if route_id in seen:
            route_id = make_stable_id("route", route_id, index)
        seen.add(route_id)
        items.append(
            {
                **row,
                "id": route_id,
                "public_model_name": public_model_name,
                "display_name": str(row.get("display_name") or public_model_name).strip(),
                "model_family": str(row.get("model_family") or row.get("category") or guess_model_family(public_model_name)).strip(),
                "routing_strategy": str(row.get("routing_strategy") or "simple-shuffle").strip(),
                "enabled": bool(row.get("enabled", True)),
            }
        )
    return items


def normalize_route_bindings(
    rows: list[dict[str, Any]],
    api_profiles: list[dict[str, Any]],
    model_routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    api_profile_ids = {item["id"] for item in api_profiles}
    model_route_ids = {item["id"] for item in model_routes}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        model_route_id = str(row.get("model_route_id") or "").strip()
        api_profile_id = str(row.get("api_profile_id") or "").strip()
        if model_route_id not in model_route_ids or api_profile_id not in api_profile_ids:
            continue
        upstream_model = str(row.get("upstream_model") or "").strip()
        binding_id = str(
            row.get("id")
            or make_stable_id("binding", model_route_id, api_profile_id, upstream_model, row.get("priority", 100), index)
        ).strip()
        if binding_id in seen:
            binding_id = make_stable_id("binding", binding_id, index)
        seen.add(binding_id)
        items.append(
            {
                **row,
                "id": binding_id,
                "model_route_id": model_route_id,
                "api_profile_id": api_profile_id,
                "upstream_model": upstream_model,
                "priority": int(row.get("priority", 100) or 100),
                "rpm": int(row["rpm"]) if row.get("rpm") not in ("", None) else None,
                "enabled": bool(row.get("enabled", True)),
            }
        )
    return items


def management_state_from_providers(
    providers: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
    routing_view_mode: str = "by_model",
) -> dict[str, Any]:
    suppliers: list[dict[str, Any]] = []
    api_profiles: list[dict[str, Any]] = []
    model_routes: list[dict[str, Any]] = []
    route_bindings: list[dict[str, Any]] = []

    supplier_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    api_profile_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    route_by_name: dict[str, dict[str, Any]] = {}

    for index, provider in enumerate(providers):
        relay_label = str(provider.get("relay_label") or "").strip()
        api_base = str(provider.get("api_base") or "").strip()
        api_key_env = str(provider.get("api_key_env") or "").strip()
        custom_llm_provider = normalize_provider_name(provider.get("custom_llm_provider"))
        model_name = str(provider.get("model_name") or "").strip() or f"model-{index + 1}"
        upstream_model = str(provider.get("upstream_model") or "").strip()
        supplier_name = guess_supplier_name(relay_label, custom_llm_provider, api_base)

        supplier_key = (supplier_name, custom_llm_provider, relay_label or supplier_name)
        supplier = supplier_by_key.get(supplier_key)
        if supplier is None:
            supplier = {
                "id": make_stable_id("supplier", supplier_name, custom_llm_provider),
                "name": supplier_name,
                "label": relay_label or supplier_name,
                "custom_llm_provider": custom_llm_provider,
                "enabled": True,
            }
            supplier_by_key[supplier_key] = supplier
            suppliers.append(supplier)

        api_profile_key = (supplier["id"], api_base, api_key_env, custom_llm_provider)
        api_profile = api_profile_by_key.get(api_profile_key)
        if api_profile is None:
            profile_label = relay_label or supplier_name
            api_profile = {
                "id": make_stable_id("api", supplier["id"], api_base or custom_llm_provider, api_key_env or profile_label),
                "supplier_id": supplier["id"],
                "label": profile_label,
                "api_base": api_base,
                "api_key_env": api_key_env,
                "api_key_value": "",
                "custom_llm_provider": custom_llm_provider,
                "known_models": [],
                "enabled": True,
            }
            api_profile_by_key[api_profile_key] = api_profile
            api_profiles.append(api_profile)

        if upstream_model and upstream_model not in api_profile["known_models"]:
            api_profile["known_models"].append(upstream_model)

        route = route_by_name.get(model_name)
        if route is None:
            route = {
                "id": make_stable_id("route", model_name),
                "public_model_name": model_name,
                "display_name": model_name,
                "model_family": guess_model_family(model_name),
                "routing_strategy": router_settings.get("routing_strategy", "simple-shuffle"),
                "enabled": True,
            }
            route_by_name[model_name] = route
            model_routes.append(route)

        route_bindings.append(
            {
                "id": make_stable_id("binding", route["id"], api_profile["id"], upstream_model or model_name, provider.get("priority", 100), index),
                "model_route_id": route["id"],
                "api_profile_id": api_profile["id"],
                "upstream_model": upstream_model,
                "priority": int(provider.get("priority", 100) or 100),
                "rpm": int(provider["rpm"]) if provider.get("rpm") not in ("", None) else None,
                "enabled": bool(provider.get("enabled", True)),
            }
        )

    normalized_model_routes = normalize_model_routes(model_routes)
    normalized_model_families = normalize_model_families(
        [*DEFAULT_MODEL_FAMILIES, *[item.get("model_family", "") for item in normalized_model_routes]]
    )

    return {
        "routing_view_mode": routing_view_mode or "by_model",
        "suppliers": normalize_suppliers(suppliers),
        "api_profiles": normalize_api_profiles(api_profiles, normalize_suppliers(suppliers)),
        "model_routes": normalized_model_routes,
        "route_bindings": normalize_route_bindings(route_bindings, normalize_api_profiles(api_profiles, normalize_suppliers(suppliers)), normalized_model_routes),
        "model_families": normalized_model_families,
        "router_settings": router_settings or {},
        "litellm_settings": litellm_settings or {},
    }


def flatten_management_state_to_providers(
    suppliers: list[dict[str, Any]],
    api_profiles: list[dict[str, Any]],
    model_routes: list[dict[str, Any]],
    route_bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    supplier_map = {item["id"]: item for item in suppliers}
    api_profile_map = {item["id"]: item for item in api_profiles}
    route_map = {item["id"]: item for item in model_routes}

    providers: list[dict[str, Any]] = []
    for binding in sorted(route_bindings, key=lambda item: (str(item.get("model_route_id", "")), int(item.get("priority", 100)))):
        route = route_map.get(str(binding.get("model_route_id", "")).strip())
        api_profile = api_profile_map.get(str(binding.get("api_profile_id", "")).strip())
        if not route or not api_profile:
            continue
        supplier = supplier_map.get(str(api_profile.get("supplier_id", "")).strip(), {})
        relay_label = str(api_profile.get("label") or supplier.get("label") or supplier.get("name") or "").strip()
        providers.append(
            {
                "binding_id": binding.get("id", ""),
                "model_route_id": route.get("id", ""),
                "api_profile_id": api_profile.get("id", ""),
                "supplier_id": supplier.get("id", ""),
                "supplier_name": supplier.get("name", ""),
                "relay_label": relay_label,
                "model_name": route.get("public_model_name", ""),
                "upstream_model": str(binding.get("upstream_model") or "").strip(),
                "api_base": str(api_profile.get("api_base") or "").strip(),
                "api_key_env": str(api_profile.get("api_key_env") or "").strip(),
                "api_key_value": str(api_profile.get("api_key_value") or "").strip(),
                "custom_llm_provider": normalize_provider_name(api_profile.get("custom_llm_provider")),
                "rpm": int(binding["rpm"]) if binding.get("rpm") not in ("", None) else None,
                "priority": int(binding.get("priority", 100) or 100),
                "enabled": bool(binding.get("enabled", True)) and bool(route.get("enabled", True)) and bool(api_profile.get("enabled", True)),
                "known_models": ensure_list(api_profile.get("known_models")),
            }
        )
    return providers


def load_management_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if any(key in state for key in ("suppliers", "api_profiles", "model_routes", "route_bindings")):
            suppliers = normalize_suppliers([item for item in ensure_list(state.get("suppliers")) if isinstance(item, dict)])
            api_profiles = normalize_api_profiles(
                [item for item in ensure_list(state.get("api_profiles")) if isinstance(item, dict)],
                suppliers,
            )
            model_routes = normalize_model_routes([item for item in ensure_list(state.get("model_routes")) if isinstance(item, dict)])
            route_bindings = normalize_route_bindings(
                [item for item in ensure_list(state.get("route_bindings")) if isinstance(item, dict)],
                api_profiles,
                model_routes,
            )
            return {
                "routing_view_mode": str(state.get("routing_view_mode") or "by_model"),
                "suppliers": suppliers,
                "api_profiles": api_profiles,
                "model_routes": model_routes,
                "route_bindings": route_bindings,
                "model_families": normalize_model_families([
                    *DEFAULT_MODEL_FAMILIES,
                    *ensure_list(state.get("model_families")),
                    *[item.get("model_family", "") for item in model_routes],
                ]),
                "router_settings": state.get("router_settings", {}) or {},
                "litellm_settings": state.get("litellm_settings", {}) or {},
            }
        providers = [item for item in ensure_list(state.get("providers")) if isinstance(item, dict)]
        return management_state_from_providers(
            providers,
            state.get("router_settings", {}) or {},
            state.get("litellm_settings", {}) or {},
            str(state.get("routing_view_mode") or "by_model"),
        )

    config = load_config()
    providers: list[dict[str, Any]] = []
    priorities_by_model: dict[str, int] = {}
    for row in config.get("model_list", []):
        params = row.get("litellm_params", {}) or {}
        info = row.get("model_info", {}) or {}
        public_model_name = info.get("public_model_name") or row.get("model_name", "")
        priority = int(info.get("priority", priorities_by_model.get(public_model_name, 100)))
        priorities_by_model[public_model_name] = priority + 10
        providers.append(
            {
                "relay_label": info.get("relay_label", ""),
                "model_name": public_model_name,
                "upstream_model": params.get("model", ""),
                "api_base": params.get("api_base", ""),
                "api_key_env": read_env_var_name(params.get("api_key")),
                "custom_llm_provider": params.get("custom_llm_provider", "openai"),
                "rpm": params.get("rpm"),
                "priority": priority,
                "enabled": bool(info.get("enabled", True)),
            }
        )
    return management_state_from_providers(
        providers,
        config.get("router_settings", {}) or {},
        config.get("litellm_settings", {}) or {},
    )


def build_litellm_config_from_providers(
    providers: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for provider in providers:
        grouped.setdefault(provider["model_name"], []).append(provider)

    model_list: list[dict[str, Any]] = []
    fallbacks: list[dict[str, list[str]]] = []
    for public_model_name, rows in grouped.items():
        active_rows = [row for row in rows if row.get("enabled", True)]
        active_rows.sort(key=lambda row: (int(row.get("priority", 100)), row.get("relay_label", "")))
        if not active_rows:
            continue

        backup_aliases: list[str] = []
        for idx, provider in enumerate(active_rows):
            relay_slug = slugify(provider.get("relay_label", "")) or f"relay-{idx+1}"
            alias = public_model_name if idx == 0 else f"{public_model_name}__{idx+1}__{relay_slug}"
            if idx > 0:
                backup_aliases.append(alias)

            params: dict[str, Any] = {
                "model": provider["upstream_model"],
                "custom_llm_provider": provider.get("custom_llm_provider", "openai") or "openai",
            }
            if provider.get("api_base"):
                params["api_base"] = provider["api_base"]
            if provider.get("api_key_env"):
                params["api_key"] = f"os.environ/{provider['api_key_env']}"
            if provider.get("rpm"):
                params["rpm"] = provider["rpm"]

            model_list.append(
                {
                    "model_name": alias,
                    "litellm_params": params,
                    "model_info": {
                        "relay_label": slugify(provider.get("relay_label", "")) or str(provider.get("relay_label", "") or ""),
                        "public_model_name": public_model_name,
                        "priority": int(provider.get("priority", 100)),
                        "enabled": bool(provider.get("enabled", True)),
                    },
                }
            )

        if backup_aliases:
            fallbacks.append({public_model_name: backup_aliases})

    next_router = dict(router_settings or {})
    next_router["fallbacks"] = fallbacks
    next_router.pop("model_group_alias", None)

    return {
        "model_list": model_list,
        "router_settings": next_router,
        "litellm_settings": litellm_settings or {},
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def load_provider_state() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    state = load_management_state()
    return (
        flatten_management_state_to_providers(
            state.get("suppliers", []),
            state.get("api_profiles", []),
            state.get("model_routes", []),
            state.get("route_bindings", []),
        ),
        state.get("router_settings", {}) or {},
        state.get("litellm_settings", {}) or {},
    )


def save_management_state(
    suppliers: list[dict[str, Any]],
    api_profiles: list[dict[str, Any]],
    model_routes: list[dict[str, Any]],
    route_bindings: list[dict[str, Any]],
    model_families: list[str],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
    routing_view_mode: str = "by_model",
) -> None:
    normalized_suppliers = normalize_suppliers(suppliers)
    normalized_api_profiles = normalize_api_profiles(api_profiles, normalized_suppliers)
    normalized_model_routes = normalize_model_routes(model_routes)
    normalized_model_families = normalize_model_families([
        *DEFAULT_MODEL_FAMILIES,
        *(model_families or []),
        *[item.get("model_family", "") for item in normalized_model_routes],
    ])
    normalized_route_bindings = normalize_route_bindings(route_bindings, normalized_api_profiles, normalized_model_routes)
    providers = flatten_management_state_to_providers(
        normalized_suppliers,
        normalized_api_profiles,
        normalized_model_routes,
        normalized_route_bindings,
    )
    backup_file(STATE_PATH)
    STATE_PATH.write_text(
        json.dumps(
            {
                "routing_view_mode": routing_view_mode or "by_model",
                "suppliers": normalized_suppliers,
                "api_profiles": normalized_api_profiles,
                "model_routes": normalized_model_routes,
                "route_bindings": normalized_route_bindings,
                "model_families": normalized_model_families,
                "providers": providers,
                "router_settings": router_settings or {},
                "litellm_settings": litellm_settings or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def save_provider_state(
    providers: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
) -> None:
    current_state = load_management_state()
    next_state = management_state_from_providers(
        providers,
        router_settings,
        litellm_settings,
        current_state.get("routing_view_mode", "by_model"),
    )
    save_management_state(
        next_state.get("suppliers", []),
        next_state.get("api_profiles", []),
        next_state.get("model_routes", []),
        next_state.get("route_bindings", []),
        next_state.get("model_families", []),
        router_settings,
        litellm_settings,
        next_state.get("routing_view_mode", "by_model"),
    )


def build_litellm_config_from_management_state(
    suppliers: list[dict[str, Any]],
    api_profiles: list[dict[str, Any]],
    model_routes: list[dict[str, Any]],
    route_bindings: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
) -> dict[str, Any]:
    providers = flatten_management_state_to_providers(suppliers, api_profiles, model_routes, route_bindings)
    return build_litellm_config_from_providers(providers, router_settings, litellm_settings)


def extract_ccswitch_provider_candidates() -> list[dict[str, Any]]:
    if not CCSWITCH_WEB_DATA.exists():
        return []

    tmp_db = Path(tempfile.gettempdir()) / f"ccswitch-webdata-import-{int(time.time())}.db"
    shutil.copy2(CCSWITCH_WEB_DATA, tmp_db)

    NAME = "1108997039640870368"
    WEBSITE = "80584110603409727"
    BASE = "-7974198728941174057"
    MODEL = "-8886929033128719565"
    PROVIDER_ID = "5663282974585124475"

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT rowid, field_id, value, date_created, date_last_used
        FROM autofill_edge_field_values
        ORDER BY rowid
        """
    ).fetchall()
    conn.close()
    try:
        tmp_db.unlink(missing_ok=True)
    except Exception:
        pass

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        field_id = str(row["field_id"])
        parts = field_id.split("$")
        if len(parts) < 3:
            continue
        group_id = "$".join(parts[:2])
        suffix = parts[2]
        item = groups.setdefault(group_id, {"group": group_id, "rowid": row["rowid"]})
        item["rowid"] = row["rowid"]
        item["last_used"] = row["date_last_used"]
        value = str(row["value"]).strip()
        if suffix == NAME:
            item["name"] = value
        elif suffix == WEBSITE:
            item["website"] = value
        elif suffix == BASE:
            item["base"] = value
        elif suffix == MODEL:
            item["model"] = value
        elif suffix == PROVIDER_ID:
            item["provider_id"] = value

    candidates: list[dict[str, Any]] = []
    for item in groups.values():
        name = str(item.get("name", "")).strip()
        model = str(item.get("model", "")).strip()
        base = str(item.get("base", "")).strip()
        website = str(item.get("website", "")).strip()
        provider_text = " ".join([name, model, base, website]).lower()
        if "edge_default_dummy" in provider_text:
            continue
        if not any([name, model, base, website]):
            continue
        if not base.startswith("http"):
            continue
        if not model:
            continue
        if model.startswith("http") or name.startswith("http"):
            continue

        alias = model.strip()
        upstream_model = model.strip()
        if upstream_model and "/" not in upstream_model:
            upstream_model = f"openai/{upstream_model}"

        api_key_env = ""
        basis = f"{base} {website} {name} {model}".lower()
        if "packyapi" in basis:
            api_key_env = "PACKYAPI_KEY_1"
        elif "anthropic" in basis or "claude" in basis:
            api_key_env = "ANTHROPIC_AUTH_TOKEN"
        elif "openai" in basis or "gpt" in basis:
            api_key_env = "OPENAI_API_KEY"

        candidates.append(
            {
                "relay_label": name,
                "model_name": alias,
                "upstream_model": upstream_model or alias,
                "api_base": base,
                "api_key_env": api_key_env,
                "custom_llm_provider": "openai",
                "rpm": 120,
                "source": "ccswitch-webdata",
                "provider_id": item.get("provider_id", ""),
                "website": website,
                "last_used": item.get("last_used", 0),
            }
        )

    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in sorted(candidates, key=lambda x: (x.get("last_used", 0), x.get("relay_label", "")), reverse=True):
        key = (item["relay_label"], item["model_name"], item["api_base"], item["upstream_model"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def process_matches_path(proc: psutil.Process, target: Path | None = None, command_pattern: str = "") -> bool:
    try:
        exe = proc.exe()
        if target and exe and Path(exe).resolve() == target.resolve():
            return True
    except Exception:
        pass
    try:
        cmdline = " ".join(proc.cmdline())
        if command_pattern and command_pattern.lower() in cmdline.lower():
            return True
        if target:
            return str(target).lower() in cmdline.lower()
        return False
    except Exception:
        return False


def stop_processes_by_target(target: Path | None = None, command_pattern: str = "") -> None:
    for proc in psutil.process_iter(["pid", "name"]):
        if process_matches_path(proc, target=target, command_pattern=command_pattern):
            try:
                proc.kill()
            except Exception:
                continue


def service_status(port: int, target: Path | None = None, command_pattern: str = "") -> dict[str, Any]:
    pids = set()
    for conn in psutil.net_connections(kind="tcp"):
        if conn.laddr and conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
            if conn.pid:
                pids.add(conn.pid)
    processes = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            processes.append(
                {
                    "pid": pid,
                    "name": proc.name(),
                    "exe": proc.exe(),
                    "cmdline": proc.cmdline(),
                    "matches_target": process_matches_path(proc, target=target, command_pattern=command_pattern),
                }
            )
        except Exception:
            continue
    return {"port": port, "listening": bool(pids), "processes": processes}


def run_powershell_script(script_name: str) -> dict[str, str]:
    script = SCRIPTS_DIR / script_name
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def restart_litellm() -> None:
    stop_processes_by_target(target=LITELLM_EXE, command_pattern="litellm")
    time.sleep(1)
    run_powershell_script("start-litellm.ps1")


class ProviderRow(BaseModel):
    model_config = {"extra": "allow"}
    relay_label: str = ""
    model_name: str
    upstream_model: str
    api_base: str = ""
    api_key_env: str = ""
    api_key_value: str = ""
    custom_llm_provider: str = "openai"
    rpm: int | None = None
    priority: int = 100
    enabled: bool = True


class SupplierRow(BaseModel):
    model_config = {"extra": "allow"}
    id: str | None = None
    name: str
    label: str = ""
    custom_llm_provider: str = "openai"
    enabled: bool = True


class APIProfileRow(BaseModel):
    model_config = {"extra": "allow"}
    id: str | None = None
    supplier_id: str = ""
    label: str
    api_base: str = ""
    api_key_env: str = ""
    api_key_value: str = ""
    custom_llm_provider: str = "openai"
    known_models: list[str] = Field(default_factory=list)
    pricing: dict[str, Any] = Field(default_factory=dict)
    quota: dict[str, Any] = Field(default_factory=dict)
    accounting: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ModelRouteRow(BaseModel):
    model_config = {"extra": "allow"}
    id: str | None = None
    public_model_name: str
    display_name: str = ""
    model_family: str = ""
    routing_strategy: str = "simple-shuffle"
    enabled: bool = True


class RouteBindingRow(BaseModel):
    model_config = {"extra": "allow"}
    id: str | None = None
    model_route_id: str
    api_profile_id: str
    upstream_model: str
    priority: int = 100
    rpm: int | None = None
    enabled: bool = True


class APIProfileCheckRow(BaseModel):
    model_config = {"extra": "allow"}
    id: str = ""
    supplier_id: str = ""
    label: str
    api_base: str = ""
    api_key_env: str = ""
    api_key_value: str = ""
    custom_llm_provider: str = "openai"
    known_models: list[str] = Field(default_factory=list)
    enabled: bool = True


class ConfigPayload(BaseModel):
    model_config = {"extra": "allow"}
    providers: list[ProviderRow] = Field(default_factory=list)
    suppliers: list[SupplierRow] = Field(default_factory=list)
    api_profiles: list[APIProfileRow] = Field(default_factory=list)
    model_routes: list[ModelRouteRow] = Field(default_factory=list)
    route_bindings: list[RouteBindingRow] = Field(default_factory=list)
    model_families: list[str] = Field(default_factory=list)
    env: dict[str, str]
    router_settings: dict[str, Any] = Field(default_factory=dict)
    litellm_settings: dict[str, Any] = Field(default_factory=dict)
    routing_view_mode: str = "by_model"
    restart_litellm: bool = True


class RoutingDraftPayload(BaseModel):
    model_config = {"extra": "allow"}
    suppliers: list[SupplierRow] = Field(default_factory=list)
    api_profiles: list[APIProfileRow] = Field(default_factory=list)
    model_routes: list[ModelRouteRow] = Field(default_factory=list)
    route_bindings: list[RouteBindingRow] = Field(default_factory=list)
    model_families: list[str] = Field(default_factory=list)
    router_settings: dict[str, Any] = Field(default_factory=dict)
    litellm_settings: dict[str, Any] = Field(default_factory=dict)
    routing_view_mode: str = "by_model"


class ProviderCheckPayload(BaseModel):
    providers: list[ProviderRow]
    env: dict[str, str]


class APIProfileCheckPayload(BaseModel):
    api_profiles: list[APIProfileCheckRow]
    env: dict[str, str]


class RouteBindingCheckRow(BaseModel):
    model_config = {"extra": "allow"}
    binding_id: str = ""
    route_id: str = ""
    public_model_name: str = ""
    api_profile_id: str = ""
    label: str = ""
    supplier_id: str = ""
    api_base: str = ""
    api_key_env: str = ""
    api_key_value: str = ""
    custom_llm_provider: str = "openai"
    upstream_model: str = ""
    pricing: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RouteBindingCheckPayload(BaseModel):
    bindings: list[RouteBindingCheckRow]
    env: dict[str, str]
    prompt: str = "Reply with OK only."
    max_tokens: int = 32


class RouteBindingNetworkCheckPayload(BaseModel):
    bindings: list[RouteBindingCheckRow]
    env: dict[str, str]


class DirectTestPayload(BaseModel):
    api_base: str
    api_key: str
    upstream_model: str
    custom_llm_provider: str = "openai"
    api_profile_id: str = ""
    relay_label: str = ""
    public_model_name: str = ""
    pricing: dict[str, Any] = Field(default_factory=dict)
    prompt: str = "Reply with OK only."
    max_tokens: int = 32


class GatewayTestPayload(BaseModel):
    alias_model: str
    api_profile_id: str = ""
    relay_label: str = ""
    upstream_model: str = ""
    api_base: str = ""
    custom_llm_provider: str = "openai"
    public_model_name: str = ""
    pricing: dict[str, Any] = Field(default_factory=dict)
    prompt: str = "Reply with OK only."
    max_tokens: int = 32


class BalanceRefreshPayload(BaseModel):
    api_profile_ids: list[str] = Field(default_factory=list)
    force: bool = True


class RechargeRecordPayload(BaseModel):
    api_profile_id: str = ""
    supplier_id: str = ""
    paid_at: str = ""
    paid_amount_cny: float | None = None
    credited_amount: float | None = None
    credited_currency: str | None = None
    bonus_amount: float | None = None
    bonus_currency: str | None = None
    balance_after_recharge: float | None = None
    note: str | None = None


class SupplierOrderSyncPayload(BaseModel):
    supplier_id: str
    limit: int = 20


class PortalLoginRepairPayload(BaseModel):
    api_profile: APIProfileCheckRow
    totp_code: str = ""


for model_cls in (
    ProviderRow,
    SupplierRow,
    APIProfileRow,
    ModelRouteRow,
    RouteBindingRow,
    APIProfileCheckRow,
    ConfigPayload,
    RoutingDraftPayload,
    ProviderCheckPayload,
    APIProfileCheckPayload,
    DirectTestPayload,
    GatewayTestPayload,
    BalanceRefreshPayload,
    RechargeRecordPayload,
    SupplierOrderSyncPayload,
    PortalLoginRepairPayload,
):
    model_cls.model_rebuild()


app = FastAPI(title="RelayDeck Local Admin")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_balance_refresh_lock = threading.Lock()
_balance_refresh_stop_event = threading.Event()
_balance_refresh_thread: threading.Thread | None = None


def refresh_balances_in_background() -> None:
    with _balance_refresh_lock:
        refresh_balances_v2(BalanceRefreshPayload(force=True))


def balance_refresh_loop() -> None:
    while not _balance_refresh_stop_event.is_set():
        try:
            refresh_balances_in_background()
        except Exception:
            # Background refresh must never kill the admin process.
            logger.exception("balance auto refresh failed")
        if _balance_refresh_stop_event.wait(BALANCE_REFRESH_INTERVAL_SEC):
            break


def start_balance_refresh_worker() -> None:
    global _balance_refresh_thread
    if _balance_refresh_thread and _balance_refresh_thread.is_alive():
        return
    _balance_refresh_stop_event.clear()
    _balance_refresh_thread = threading.Thread(
        target=balance_refresh_loop,
        name="balance-refresh-worker",
        daemon=True,
    )
    _balance_refresh_thread.start()


def stop_balance_refresh_worker() -> None:
    global _balance_refresh_thread
    _balance_refresh_stop_event.set()
    thread = _balance_refresh_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _balance_refresh_thread = None


@app.on_event("startup")
def on_startup() -> None:
    init_usage_quota_store()
    start_balance_refresh_worker()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_balance_refresh_worker()


@app.get("/")
def index() -> FileResponse:
    response = FileResponse(STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "name": "RelayDeck Local Admin"}


@app.get("/api/quota-adapters")
def api_quota_adapters() -> dict[str, Any]:
    return {
        "ok": True,
        "directory": str(ensure_quota_adapters_dir()),
        "items": list_quota_adapter_scripts(),
    }


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    env_map = parse_env_file()
    management_state = load_management_state()
    api_profiles = hydrate_api_profiles_with_secrets(management_state.get("api_profiles", []), env_map)
    management_state = {**management_state, "api_profiles": api_profiles}
    providers = flatten_management_state_to_providers(
        management_state.get("suppliers", []),
        api_profiles,
        management_state.get("model_routes", []),
        management_state.get("route_bindings", []),
    )
    router_settings = management_state.get("router_settings", {}) or {}
    litellm_settings = management_state.get("litellm_settings", {}) or {}
    litellm_port, open_webui_port, admin_port = get_ports(env_map)
    return {
        "providers": providers,
        "routing_view_mode": management_state.get("routing_view_mode", "by_model"),
        "suppliers": management_state.get("suppliers", []),
        "api_profiles": api_profiles,
        "model_routes": management_state.get("model_routes", []),
        "route_bindings": management_state.get("route_bindings", []),
        "model_families": management_state.get("model_families", []),
        "env": env_map,
        "router_settings": router_settings,
        "litellm_settings": litellm_settings,
        "usage_dashboard": usage_dashboard_snapshot(management_state),
        "ports": {
            "litellm": litellm_port,
            "open_webui": open_webui_port,
            "admin_panel": admin_port,
        },
        "service_status": {
            "litellm": service_status(litellm_port, target=LITELLM_EXE, command_pattern="litellm"),
            "open_webui": service_status(open_webui_port, target=OPEN_WEBUI_EXE, command_pattern="open-webui"),
            "admin_panel": service_status(admin_port, target=PYTHON_EXE, command_pattern=str(ADMIN_PANEL_DIR)),
        },
    }


@app.post("/api/save")
def api_save(payload: ConfigPayload) -> dict[str, Any]:
    has_management_state = any([payload.suppliers, payload.api_profiles, payload.model_routes, payload.route_bindings])

    if has_management_state:
        suppliers = normalize_suppliers([item.model_dump() for item in payload.suppliers])
        api_profiles = normalize_api_profiles([item.model_dump() for item in payload.api_profiles], suppliers)
        model_routes = normalize_model_routes([item.model_dump() for item in payload.model_routes])
        route_bindings = normalize_route_bindings([item.model_dump() for item in payload.route_bindings], api_profiles, model_routes)
        config = build_litellm_config_from_management_state(
            suppliers,
            api_profiles,
            model_routes,
            route_bindings,
            payload.router_settings,
            payload.litellm_settings,
        )
    else:
        provider_dicts = [provider.model_dump() for provider in payload.providers]
        next_state = management_state_from_providers(
            provider_dicts,
            payload.router_settings,
            payload.litellm_settings,
            payload.routing_view_mode,
        )
        suppliers = next_state.get("suppliers", [])
        api_profiles = next_state.get("api_profiles", [])
        model_routes = next_state.get("model_routes", [])
        route_bindings = next_state.get("route_bindings", [])
        config = build_litellm_config_from_management_state(
            suppliers,
            api_profiles,
            model_routes,
            route_bindings,
            payload.router_settings,
            payload.litellm_settings,
        )

    next_env = merge_api_profile_secrets_into_env(payload.env, api_profiles)
    backup_file(ENV_PATH)
    save_config(config)
    save_management_state(
        suppliers,
        api_profiles,
        model_routes,
        route_bindings,
        payload.model_families,
        payload.router_settings,
        payload.litellm_settings,
        payload.routing_view_mode,
    )
    write_env_file(next_env)

    if payload.restart_litellm:
        restart_litellm()

    return {"ok": True}


@app.post("/api/save-routing-draft")
def api_save_routing_draft(payload: RoutingDraftPayload) -> dict[str, Any]:
    suppliers = normalize_suppliers([item.model_dump() for item in payload.suppliers])
    api_profiles = normalize_api_profiles([item.model_dump() for item in payload.api_profiles], suppliers)
    model_routes = normalize_model_routes([item.model_dump() for item in payload.model_routes])
    route_bindings = normalize_route_bindings([item.model_dump() for item in payload.route_bindings], api_profiles, model_routes)
    save_management_state(
        suppliers,
        api_profiles,
        model_routes,
        route_bindings,
        payload.model_families,
        payload.router_settings,
        payload.litellm_settings,
        payload.routing_view_mode,
    )
    return {
        "ok": True,
        "saved_to": str(STATE_PATH),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/usage/summary")
def api_usage_summary() -> dict[str, Any]:
    management_state = load_management_state()
    env_map = parse_env_file()
    api_profiles = hydrate_api_profiles_with_secrets(management_state.get("api_profiles", []), env_map)
    dashboard = usage_dashboard_snapshot({**management_state, "api_profiles": api_profiles})
    return {"ok": True, **dashboard}


@app.get("/api/providers/comparison")
def api_provider_comparison() -> dict[str, Any]:
    management_state = load_management_state()
    env_map = parse_env_file()
    api_profiles = hydrate_api_profiles_with_secrets(management_state.get("api_profiles", []), env_map)
    dashboard = usage_dashboard_snapshot({**management_state, "api_profiles": api_profiles})
    return {"ok": True, "generated_at": dashboard["generated_at"], "items": dashboard["comparison"]}


@app.get("/api/recharge-records")
def api_recharge_records(api_profile_id: str | None = None, supplier_id: str | None = None) -> dict[str, Any]:
    management_state = load_management_state()
    store = get_usage_quota_store()
    suppliers = {
        item["id"]: item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    api_profiles = {item["id"]: item for item in management_state.get("api_profiles", []) if isinstance(item, dict) and item.get("id")}
    profile_lookup = {
        (
            profile_provider_key(profile),
            str(profile.get("api_key_env") or "").strip(),
        ): profile
        for profile in api_profiles.values()
    }
    supplier_lookup = {
        supplier_provider_key(supplier.get("id"), normalize_provider_name(supplier.get("custom_llm_provider"))): supplier
        for supplier in suppliers.values()
    }
    items: list[dict[str, Any]] = []
    if api_profile_id:
        profile = api_profiles.get(api_profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="api_profile not found")
        items = recharge_records_for_profile(store, profile)
    elif supplier_id:
        supplier = suppliers.get(supplier_id)
        if not supplier:
            raise HTTPException(status_code=404, detail="supplier not found")
        items = recharge_records_for_supplier(
            store,
            supplier_id,
            provider_name=normalize_provider_name(supplier.get("custom_llm_provider")),
        )
    else:
        items = store.list_recharge_records(limit=500)
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        matched_profile = profile_lookup.get(
            (
                str(item.get("provider_key") or ""),
                str(item.get("api_key_env") or "").strip(),
            )
        )
        matched_supplier = supplier_lookup.get(str(item.get("provider_key") or ""))
        resolved_supplier = suppliers.get(str((matched_profile or {}).get("supplier_id") or "").strip()) if matched_profile else matched_supplier
        normalized_items.append(
            {
                **item,
                "api_profile_id": (matched_profile or {}).get("id"),
                "supplier_id": str((resolved_supplier or {}).get("id") or ""),
                "supplier_name": str((resolved_supplier or {}).get("name") or (resolved_supplier or {}).get("label") or ""),
            }
        )
    return {"ok": True, "items": normalized_items}


@app.post("/api/recharge-records")
def api_create_recharge_record(payload: RechargeRecordPayload) -> dict[str, Any]:
    management_state = load_management_state()
    suppliers = {
        item["id"]: item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    api_profiles = {item["id"]: item for item in management_state.get("api_profiles", []) if isinstance(item, dict) and item.get("id")}
    store = get_usage_quota_store()
    normalized_paid_amount = float(payload.paid_amount_cny or 0)
    record_payload: dict[str, Any]
    if str(payload.supplier_id or "").strip():
        supplier = suppliers.get(str(payload.supplier_id or "").strip())
        if not supplier:
            raise HTTPException(status_code=404, detail="supplier not found")
        provider_name = normalize_provider_name(supplier.get("custom_llm_provider"))
        record_payload = {
            "provider_key": supplier_provider_key(supplier.get("id"), provider_name),
            "relay_label": str(supplier.get("name") or supplier.get("label") or ""),
            "api_key_env": "",
            "paid_at": payload.paid_at or now_iso_local(),
            "paid_amount_cny": normalized_paid_amount,
            "credited_amount": payload.credited_amount,
            "credited_currency": payload.credited_currency,
            "bonus_amount": payload.bonus_amount,
            "bonus_currency": payload.bonus_currency,
            "balance_after_recharge": payload.balance_after_recharge,
            "note": payload.note,
        }
    else:
        profile = api_profiles.get(payload.api_profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="api_profile not found")
        record_payload = {
            "provider_key": profile_provider_key(profile),
            "relay_label": str(profile.get("label") or ""),
            "api_key_env": str(profile.get("api_key_env") or ""),
            "paid_at": payload.paid_at or now_iso_local(),
            "paid_amount_cny": normalized_paid_amount,
            "credited_amount": payload.credited_amount,
            "credited_currency": payload.credited_currency,
            "bonus_amount": payload.bonus_amount,
            "bonus_currency": payload.bonus_currency,
            "balance_after_recharge": payload.balance_after_recharge,
            "note": payload.note,
        }
    record = store.insert_recharge_record(
        record_payload
    )
    return {"ok": True, "item": record}


@app.post("/api/recharge-records/sync-portal-orders")
def api_sync_portal_orders(payload: SupplierOrderSyncPayload) -> dict[str, Any]:
    management_state = load_management_state()
    env_map = parse_env_file()
    suppliers = {
        item["id"]: item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    supplier = suppliers.get(str(payload.supplier_id or "").strip())
    if not supplier:
        raise HTTPException(status_code=404, detail="supplier not found")

    api_profiles = hydrate_api_profiles_with_secrets(management_state.get("api_profiles", []), env_map)
    supplier_profiles = [
        item for item in api_profiles
        if str(item.get("supplier_id") or "").strip() == str(supplier.get("id") or "").strip()
    ]
    if not supplier_profiles:
        raise HTTPException(status_code=404, detail="supplier has no api_profiles")

    profile = next(
        (
            item for item in supplier_profiles
            if portal_base_for_profile(item)
            and str(item.get("api_base") or "").strip()
        ),
        supplier_profiles[0],
    )

    store = get_usage_quota_store()
    result = sync_supplier_orders_from_portal(
        store,
        supplier,
        profile,
        limit=max(1, min(int(payload.limit or 20), 100)),
    )
    return {"ok": result.get("ok", False), **result}


@app.post("/api/portal/repair-login")
def api_repair_portal_login(payload: PortalLoginRepairPayload) -> dict[str, Any]:
    profile = payload.api_profile.model_dump()
    result = repair_portal_login_state(
        profile,
        totp_code=str(payload.totp_code or "").strip(),
    )
    return result


@app.post("/api/balances/refresh")
def api_refresh_balances(payload: BalanceRefreshPayload) -> dict[str, Any]:
    with _balance_refresh_lock:
        return refresh_balances_v2(payload)

    management_state = load_management_state()
    profiles = [item for item in management_state.get("api_profiles", []) if isinstance(item, dict)]
    if payload.api_profile_ids:
        allowed = set(payload.api_profile_ids)
        profiles = [item for item in profiles if item.get("id") in allowed]
    store = get_usage_quota_store()
    results: list[dict[str, Any]] = []
    for profile in profiles:
        quota = ensure_mapping(profile.get("quota"))
        adapter = str(quota.get("adapter") or "manual")
        currency = str(quota.get("currency") or "CNY")
        limit = quota.get("limit")
        current_balance = quota.get("current_balance")
        used_amount = quota.get("used_amount")
        if current_balance in (None, "") and limit not in (None, "") and used_amount not in (None, ""):
            current_balance = float(limit) - float(used_amount)
        if used_amount in (None, "") and limit not in (None, "") and current_balance not in (None, ""):
            used_amount = float(limit) - float(current_balance)
        status = "ok" if limit not in (None, "") or current_balance not in (None, "") else "unsupported"
        snapshot = store.insert_provider_balance_snapshot(
            {
                "provider_key": profile_provider_key(profile),
                "relay_label": str(profile.get("label") or ""),
                "api_key_env": str(profile.get("api_key_env") or ""),
                "adapter": adapter,
                "status": status,
                "currency": currency,
                "balance_total": float(limit) if limit not in (None, "") else None,
                "balance_used": float(used_amount) if used_amount not in (None, "") else None,
                "balance_remaining": float(current_balance) if current_balance not in (None, "") else None,
                "period_start": str(quota.get("period_start") or ""),
                "period_end": str(quota.get("period_end") or ""),
                "raw_summary": {
                    "source": "manual-quota-config",
                    "force": payload.force,
                },
            }
        )
        results.append(
            {
                "api_profile_id": profile.get("id"),
                "label": profile.get("label"),
                "status": status,
                "source": "manual",
                "adapter": adapter,
                "currency": snapshot.get("currency"),
                "balance_total": snapshot.get("balance_total"),
                "balance_used": snapshot.get("balance_used"),
                "balance_remaining": snapshot.get("balance_remaining"),
                "period_start": snapshot.get("period_start"),
                "period_end": snapshot.get("period_end"),
                "checked_at": snapshot.get("checked_at"),
                "message": "" if status == "ok" else "尚未配置可刷新的额度数据，当前仅支持手动额度。",
            }
        )
    return {"ok": True, "refreshed_at": now_iso_local(), "results": results}


@app.post("/api/service/start-all")
def api_start_all() -> dict[str, Any]:
    result = run_powershell_script("start-all.ps1")
    return {"ok": True, **result}


@app.post("/api/service/stop-all")
def api_stop_all() -> dict[str, Any]:
    result = run_powershell_script("stop-llm-stack.ps1")
    return {"ok": True, **result}


@app.post("/api/service/restart-all")
def api_restart_all() -> dict[str, Any]:
    stop_result = run_powershell_script("stop-llm-stack.ps1")
    start_result = run_powershell_script("start-all.ps1")
    return {
        "ok": True,
        "stop_stdout": stop_result.get("stdout", ""),
        "start_stdout": start_result.get("stdout", ""),
        "stderr": "\n".join(filter(None, [stop_result.get("stderr", ""), start_result.get("stderr", "")])),
    }


@app.post("/api/service/restart-litellm")
def api_restart_litellm() -> dict[str, Any]:
    restart_litellm()
    return {"ok": True}


@app.post("/api/test/direct")
def api_test_direct(payload: DirectTestPayload) -> dict[str, Any]:
    api_base = resolve_provider_api_base(payload.api_base, payload.custom_llm_provider)
    url, headers, body = build_direct_request(
        api_base=api_base,
        api_key=payload.api_key,
        custom_llm_provider=payload.custom_llm_provider,
        upstream_model=payload.upstream_model,
        prompt=payload.prompt,
        max_tokens=payload.max_tokens,
    )
    try:
        started_at = time.perf_counter()
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, headers=headers, json=body)
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        body_json = resp.json() if resp.text else {}
        usage = extract_usage_from_response(body_json)
        store = get_usage_quota_store()
        estimated_cost, currency = estimate_cost_from_profile(
            {
                "pricing": payload.pricing,
                "custom_llm_provider": payload.custom_llm_provider,
            },
            usage["prompt_tokens"],
            usage["completion_tokens"],
        )
        store.insert_usage_event(
            {
                "source": "admin_test_direct",
                "request_id": body_json.get("id") if isinstance(body_json, dict) else None,
                "public_model_name": payload.public_model_name or model_name_for_direct_request(payload.upstream_model, payload.custom_llm_provider),
                "litellm_model_name": None,
                "upstream_model": payload.upstream_model,
                "relay_label": payload.relay_label or payload.api_base,
                "api_base_hash": api_base_hash(api_base),
                "custom_llm_provider": payload.custom_llm_provider,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "estimated_cost": estimated_cost,
                "actual_cost": None,
                "currency": currency,
                "latency_ms": latency_ms,
                "status": "success" if resp.is_success else "error",
                "error_code": None if resp.is_success else str(resp.status_code),
            }
        )
        return {
            "url": url,
            "status_code": resp.status_code,
            "ok": resp.is_success,
            "body": body_json,
            "usage": usage,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/test/gateway")
def api_test_gateway(payload: GatewayTestPayload) -> dict[str, Any]:
    env_map = parse_env_file()
    litellm_port, _, _ = get_ports(env_map)
    master = env_map.get("LITELLM_MASTER_KEY", "")
    headers = {"Authorization": f"Bearer {master}", "Content-Type": "application/json"}
    body = {
        "model": payload.alias_model,
        "messages": [{"role": "user", "content": payload.prompt}],
        "max_tokens": payload.max_tokens,
        "temperature": 0,
        "stream": False,
    }
    try:
        started_at = time.perf_counter()
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"http://127.0.0.1:{litellm_port}/chat/completions", headers=headers, json=body)
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        body_json = resp.json() if resp.text else {}
        usage = extract_usage_from_response(body_json)
        store = get_usage_quota_store()
        estimated_cost, currency = estimate_cost_from_profile(
            {
                "pricing": payload.pricing,
                "custom_llm_provider": payload.custom_llm_provider,
            },
            usage["prompt_tokens"],
            usage["completion_tokens"],
        )
        store.insert_usage_event(
            {
                "source": "admin_test_gateway",
                "request_id": body_json.get("id") if isinstance(body_json, dict) else None,
                "public_model_name": payload.public_model_name or payload.alias_model,
                "litellm_model_name": payload.alias_model,
                "upstream_model": payload.upstream_model or (body_json.get("model") if isinstance(body_json, dict) else payload.alias_model),
                "relay_label": payload.relay_label or payload.alias_model,
                "api_base_hash": api_base_hash(payload.api_base or f"http://127.0.0.1:{litellm_port}"),
                "custom_llm_provider": payload.custom_llm_provider or "openai",
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "estimated_cost": estimated_cost,
                "actual_cost": None,
                "currency": currency,
                "latency_ms": latency_ms,
                "status": "success" if resp.is_success else "error",
                "error_code": None if resp.is_success else str(resp.status_code),
            }
        )
        return {
            "url": f"http://127.0.0.1:{litellm_port}/chat/completions",
            "status_code": resp.status_code,
            "ok": resp.is_success,
            "body": body_json,
            "usage": usage,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/models")
def api_models() -> dict[str, Any]:
    env_map = parse_env_file()
    litellm_port, _, _ = get_ports(env_map)
    master = env_map.get("LITELLM_MASTER_KEY", "")
    headers = {"Authorization": f"Bearer {master}"}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"http://127.0.0.1:{litellm_port}/models", headers=headers)
        return {"url": f"http://127.0.0.1:{litellm_port}/models", "status_code": resp.status_code, "ok": resp.is_success, "body": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/import/ccswitch")
def api_import_ccswitch() -> dict[str, Any]:
    env_map = parse_env_file()
    providers, router_settings, litellm_settings = load_provider_state()
    imported = extract_ccswitch_provider_candidates()

    existing_keys = {
        (
            provider.get("relay_label", ""),
            provider.get("model_name", ""),
            provider.get("api_base", ""),
            provider.get("upstream_model", ""),
            provider.get("api_key_env", ""),
        )
        for provider in providers
    }

    added = []
    for item in imported:
        key = (
            item.get("relay_label", ""),
            item.get("model_name", ""),
            item.get("api_base", ""),
            item.get("upstream_model", ""),
            item.get("api_key_env", ""),
        )
        if key in existing_keys:
            continue
        same_model = [provider for provider in providers if provider.get("model_name") == item.get("model_name")]
        item["priority"] = 10 + 10 * len(same_model)
        item["enabled"] = True
        providers.append(item)
        existing_keys.add(key)
        added.append(item)

    config = build_litellm_config_from_providers(providers, router_settings, litellm_settings)
    save_config(config)
    save_provider_state(providers, router_settings, litellm_settings)
    restart_litellm()

    litellm_port, open_webui_port, admin_port = get_ports(env_map)
    return {
        "ok": True,
        "imported_count": len(imported),
        "added_count": len(added),
        "added": added,
        "ports": {
            "litellm": litellm_port,
            "open_webui": open_webui_port,
            "admin_panel": admin_port,
        },
    }


@app.post("/api/check/providers")
def api_check_providers(payload: ProviderCheckPayload) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, provider in enumerate(payload.providers):
        api_base = provider.api_base.strip()
        key_name = provider.api_key_env.strip()
        api_key = provider.api_key_value.strip() or payload.env.get(key_name, "")
        if api_key and not key_name:
            key_name = default_api_key_env_name(provider.model_name, provider.relay_label, provider.custom_llm_provider)
        result: dict[str, Any] = {
            "index": index,
            "relay_label": provider.relay_label,
            "model_name": provider.model_name,
            "upstream_model": provider.upstream_model,
            "api_base": api_base,
            "status": "yellow",
            "reason": "",
            "status_code": None,
            "token_cost": "none",
        }

        if not api_base:
            result["reason"] = "缺少 API Base"
            results.append(result)
            continue
        if not key_name or not api_key:
            result["reason"] = "缺少 API Key 环境变量或值"
            results.append(result)
            continue

        url = api_base.rstrip("/") + "/models"
        try:
            with httpx.Client(timeout=12) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            result["status_code"] = resp.status_code

            if not resp.is_success:
                result["status"] = "red"
                hint = api_base_mismatch_hint(provider_name, profile.api_base, resp.status_code)
                try:
                    body = resp.json()
                    message = json.dumps(body, ensure_ascii=False)[:300]
                except Exception:
                    message = resp.text[:300]
                result["reason"] = f"{message}；{hint}"[:500] if hint else message
                results.append(result)
                continue

            try:
                body = resp.json()
            except Exception:
                result["status"] = "yellow"
                result["reason"] = "/models 返回成功，但不是 JSON"
                results.append(result)
                continue

            ids = [item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)]
            if provider.upstream_model in ids or provider.model_name in ids:
                result["status"] = "green"
                result["reason"] = "可用：/models 可访问且模型存在"
            else:
                result["status"] = "yellow"
                result["reason"] = "鉴权成功，但 /models 未发现该模型"
            result["known_ids"] = ids[:50]
            results.append(result)
        except Exception as exc:
            result["status"] = "red"
            result["reason"] = str(exc)
            results.append(result)

    return {"results": results}


@app.post("/api/check/providers-v2")
def api_check_providers_v2(payload: ProviderCheckPayload) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, provider in enumerate(payload.providers):
        provider_name = normalize_provider_name(provider.custom_llm_provider)
        api_base = resolve_provider_api_base(provider.api_base, provider_name)
        key_name = provider.api_key_env.strip()
        api_key = provider.api_key_value.strip() or payload.env.get(key_name, "").strip()
        if api_key and not key_name:
            key_name = default_api_key_env_name(provider.model_name, provider.relay_label, provider.custom_llm_provider)
        direct_model = model_name_for_direct_request(provider.upstream_model, provider_name)
        result: dict[str, Any] = {
            "index": index,
            "relay_label": provider.relay_label,
            "model_name": provider.model_name,
            "upstream_model": provider.upstream_model,
            "direct_model": direct_model,
            "provider": provider_name,
            "api_base": api_base,
            "status": "yellow",
            "reason": "",
            "status_code": None,
            "token_cost": "none",
        }

        if not api_base:
            result["reason"] = "缺少 API Base"
            results.append(result)
            continue
        if not key_name or not api_key:
            result["reason"] = "缺少 API Key 环境变量或其值为空"
            results.append(result)
            continue

        probe_env = dict(payload.env)
        if key_name:
            probe_env[key_name] = api_key
        url, headers = provider_models_probe({**provider.model_dump(), "api_key_env": key_name}, probe_env)
        try:
            with httpx.Client(timeout=12) as client:
                resp = client.get(url, headers=headers)
            result["status_code"] = resp.status_code

            if not resp.is_success:
                result["status"] = "red"
                try:
                    body = resp.json()
                    result["reason"] = json.dumps(body, ensure_ascii=False)[:300]
                except Exception:
                    result["reason"] = resp.text[:300]
                results.append(result)
                continue

            try:
                body = resp.json()
            except Exception:
                result["status"] = "yellow"
                result["reason"] = "/models 返回成功，但响应不是 JSON"
                results.append(result)
                continue

            ids = [item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)]
            if provider.upstream_model in ids or provider.model_name in ids or direct_model in ids:
                result["status"] = "green"
                result["reason"] = "可用：/models 可访问且模型存在"
            else:
                result["status"] = "yellow"
                result["reason"] = "鉴权成功，但 /models 中未发现该模型"
            result["known_ids"] = ids[:50]
            results.append(result)
        except Exception as exc:
            result["status"] = "red"
            result["reason"] = str(exc)
            results.append(result)

    return {"results": results}


@app.post("/api/check/api-profiles")
def api_check_api_profiles(payload: APIProfileCheckPayload) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, profile in enumerate(payload.api_profiles):
        provider_name = normalize_provider_name(profile.custom_llm_provider)
        api_base = resolve_provider_api_base(profile.api_base, provider_name)
        key_name = profile.api_key_env.strip()
        api_key = profile.api_key_value.strip() or payload.env.get(key_name, "").strip()
        if api_key and not key_name:
            key_name = default_api_key_env_name(profile.id or profile.label, profile.label, profile.custom_llm_provider)
        result: dict[str, Any] = {
            "index": index,
            "id": profile.id,
            "label": profile.label,
            "supplier_id": profile.supplier_id,
            "provider": provider_name,
            "api_base": api_base,
            "status": "yellow",
            "reason": "",
            "status_code": None,
            "known_ids": [],
        }

        if not api_base:
            result["reason"] = "缺少 API Base"
            results.append(result)
            continue
        if not key_name or not api_key:
            result["reason"] = "缺少 API Key 环境变量或其值为空"
            results.append(result)
            continue

        probe_profile = {
            "custom_llm_provider": provider_name,
            "api_base": profile.api_base,
            "api_key_env": key_name,
        }
        probe_env = dict(payload.env)
        if key_name:
            probe_env[key_name] = api_key
        url, headers = provider_models_probe(probe_profile, probe_env)
        try:
            with httpx.Client(timeout=12) as client:
                resp = client.get(url, headers=headers)
            result["status_code"] = resp.status_code

            if not resp.is_success:
                result["status"] = "red"
                try:
                    body = resp.json()
                    result["reason"] = json.dumps(body, ensure_ascii=False)[:300]
                except Exception:
                    result["reason"] = resp.text[:300]
                results.append(result)
                continue

            try:
                body = resp.json()
            except Exception:
                result["status"] = "yellow"
                result["reason"] = "/models 返回成功，但响应不是 JSON"
                results.append(result)
                continue

            ids = [item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)]
            result["status"] = "green"
            result["reason"] = "可用：/models 可访问"
            result["known_ids"] = ids[:200]
            results.append(result)
        except Exception as exc:
            result["status"] = "red"
            result["reason"] = str(exc)
            results.append(result)

    return {"results": results}


@app.post("/api/check/route-bindings")
def api_check_route_bindings(payload: RouteBindingCheckPayload) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, binding in enumerate(payload.bindings):
        provider_name = normalize_provider_name(binding.custom_llm_provider)
        api_base = resolve_provider_api_base(binding.api_base, provider_name)
        key_name = binding.api_key_env.strip()
        api_key = binding.api_key_value.strip() or payload.env.get(key_name, "").strip()
        if api_key and not key_name:
            key_name = default_api_key_env_name(binding.public_model_name or binding.binding_id, binding.label, binding.custom_llm_provider)
        direct_model = model_name_for_direct_request(binding.upstream_model, provider_name)
        result: dict[str, Any] = {
            "index": index,
            "binding_id": binding.binding_id,
            "route_id": binding.route_id,
            "public_model_name": binding.public_model_name,
            "label": binding.label,
            "supplier_id": binding.supplier_id,
            "provider": provider_name,
            "api_base": api_base,
            "upstream_model": binding.upstream_model,
            "direct_model": direct_model,
            "status": "yellow",
            "reason": "",
            "status_code": None,
            "ok": False,
        }
        if not api_base:
            result["reason"] = "缺少 API Base"
            results.append(result)
            continue
        if not key_name or not api_key:
            result["reason"] = "缺少 API KEY 环境变量或其值为空"
            results.append(result)
            continue
        try:
            url, headers, body = build_direct_request(
                api_base=api_base,
                api_key=api_key,
                custom_llm_provider=provider_name,
                upstream_model=binding.upstream_model,
                prompt=payload.prompt,
                max_tokens=payload.max_tokens,
            )
            with httpx.Client(timeout=20) as client:
                resp = client.post(url, headers=headers, json=body)
            result["status_code"] = resp.status_code
            result["ok"] = resp.is_success
            if resp.is_success:
                result["status"] = "green"
                result["reason"] = "可用：真实对话测试成功"
            else:
                result["status"] = "red" if resp.status_code >= 400 else "yellow"
                try:
                    body_json = resp.json()
                    result["reason"] = json.dumps(body_json, ensure_ascii=False)[:300]
                except Exception:
                    result["reason"] = resp.text[:300]
            results.append(result)
        except Exception as exc:
            result["status"] = "red"
            result["reason"] = str(exc)
            results.append(result)
    return {"results": results}


@app.post("/api/check/route-bindings-network")
def api_check_route_bindings_network(payload: RouteBindingNetworkCheckPayload) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, binding in enumerate(payload.bindings):
        provider_name = normalize_provider_name(binding.custom_llm_provider)
        api_base = resolve_provider_api_base(binding.api_base, provider_name)
        key_name = binding.api_key_env.strip()
        api_key = binding.api_key_value.strip() or payload.env.get(key_name, "").strip()
        if api_key and not key_name:
            key_name = default_api_key_env_name(binding.public_model_name or binding.binding_id, binding.label, binding.custom_llm_provider)
        direct_model = model_name_for_direct_request(binding.upstream_model, provider_name)
        result: dict[str, Any] = {
            "index": index,
            "binding_id": binding.binding_id,
            "route_id": binding.route_id,
            "public_model_name": binding.public_model_name,
            "label": binding.label,
            "supplier_id": binding.supplier_id,
            "provider": provider_name,
            "api_base": api_base,
            "upstream_model": binding.upstream_model,
            "direct_model": direct_model,
            "status": "yellow",
            "reason": "",
            "status_code": None,
            "known_ids": [],
            "ok": False,
        }
        if not api_base:
            result["reason"] = "缺少 API Base"
            results.append(result)
            continue
        if not key_name or not api_key:
            result["reason"] = "缺少 API KEY 环境变量或其值为空"
            results.append(result)
            continue
        probe_env = dict(payload.env)
        probe_env[key_name] = api_key
        try:
            url, headers = provider_models_probe(
                {
                    "custom_llm_provider": provider_name,
                    "api_base": binding.api_base,
                    "api_key_env": key_name,
                    "upstream_model": binding.upstream_model,
                    "model_name": binding.public_model_name,
                },
                probe_env,
            )
            with httpx.Client(timeout=12) as client:
                resp = client.get(url, headers=headers)
            result["status_code"] = resp.status_code
            if not resp.is_success:
                result["status"] = "red"
                try:
                    body = resp.json()
                    result["reason"] = json.dumps(body, ensure_ascii=False)[:300]
                except Exception:
                    result["reason"] = resp.text[:300]
                results.append(result)
                continue
            try:
                body = resp.json()
            except Exception:
                result["status"] = "yellow"
                result["reason"] = "/models 返回成功，但响应不是 JSON"
                results.append(result)
                continue
            ids = [item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)]
            result["known_ids"] = ids[:50]
            model_found = (
                binding.upstream_model in ids
                or binding.public_model_name in ids
                or direct_model in ids
            )
            result["model_found"] = model_found
            result["status"] = "green"
            result["ok"] = True
            result["reason"] = (
                "/models 可访问，网络与鉴权正常；已发现目标模型"
                if model_found
                else "/models 可访问，网络与鉴权正常；列表中暂未发现目标模型，请用测试对话确认"
            )
            results.append(result)
        except Exception as exc:
            result["status"] = "red"
            result["reason"] = str(exc)
            results.append(result)
    return {"results": results}


@app.get("/api/logs/{name}")
def api_logs(name: str) -> dict[str, Any]:
    path = LOG_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="log not found")
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {"name": name, "tail": text[-12000:]}
