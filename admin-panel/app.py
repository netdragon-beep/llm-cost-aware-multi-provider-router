from __future__ import annotations

import copy
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
from cost_attribution import (
    attribute_supplier_costs,
    subscription_amortization,
    subscription_usage_summary,
    weighted_topup_basis,
)
from credential_vault import CredentialVault, get_credential_vault
from supplier_sso import (
    BUILTIN_BROWSER_SSO_ADAPTERS,
    SsoTaskActiveError,
    SupplierSsoManager,
    browser_runtime_status,
    browser_sso_adapter_for_supplier,
    run_supplier_browser_sso,
)
from relaydeck_litellm_discovery import (
    claude_discovery_metadata,
    normalize_claude_discovery_settings,
)
from usage_quota_store import get_usage_quota_store, init_usage_quota_store


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "litellm.yaml"
CLAUDE_CONFIG_PATH = ROOT / "config" / "litellm-claude.yaml"
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
    "vip.auto-code.net": "autocode-web",
}
BUILTIN_QUOTA_ADAPTER_LABELS = {
    "lingsuan-web": "灵算网站额度",
    "autocode-web": "AutoCode 网站额度",
}
PERSISTED_SUPPLIER_QUOTA_FIELDS = {"enabled", "currency", "portal_base", "pricing_automation", "billing_mode"}
BILLING_MODES = {"balance", "subscription", "mixed"}
PRICING_AUTOMATION_MODES = {"manual", "detect-confirm", "auto-apply"}
SENSITIVE_QUOTA_CREDENTIAL_FIELDS = {
    "auth_token",
    "login_email",
    "login_password",
    "refresh_token",
    "session_cookie",
    "totp_secret",
}
ADAPTER_CREDENTIAL_PERMISSION_FIELDS = {*SENSITIVE_QUOTA_CREDENTIAL_FIELDS, "api_key_value"}
SENSITIVE_KEY_FRAGMENTS = ("authorization", "cookie", "password", "secret", "token", "api_key", "apikey")
CLAUDE_DISCOVERY_SETTINGS_KEY = "relaydeck_claude_discovery"


def claude_discovery_settings(litellm_settings: dict[str, Any] | None) -> dict[str, Any]:
    source = ensure_mapping((litellm_settings or {}).get(CLAUDE_DISCOVERY_SETTINGS_KEY))
    return normalize_claude_discovery_settings(source)


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
        try:
            manifest = load_quota_adapter_manifest(path)
        except Exception as exc:
            manifest = {"version": 1, "credential_permissions": [], "error": str(exc)}
        items.append(
            {
                "name": path.name,
                "stem": path.stem,
                "path": str(path),
                "type": path.suffix.lower().lstrip("."),
                "manifest": manifest,
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


def load_quota_adapter_manifest(script_path: Path) -> dict[str, Any]:
    manifest_path = script_path.with_suffix(".adapter.json")
    if not manifest_path.exists():
        return {"version": 1, "credential_permissions": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Invalid adapter manifest: {manifest_path.name}")
    permissions = [
        str(item).strip()
        for item in ensure_list(manifest.get("credential_permissions"))
        if str(item).strip() in ADAPTER_CREDENTIAL_PERMISSION_FIELDS
    ]
    return {**manifest, "version": int(manifest.get("version") or 1), "credential_permissions": permissions}


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


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{ts}")
    backup_path.write_bytes(path.read_bytes())
    return backup_path


def save_config(config: dict[str, Any]) -> None:
    backup_file(CONFIG_PATH)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def save_gateway_configs(configs: dict[str, dict[str, Any]]) -> None:
    """Persist the native and Claude Code gateway views together."""
    save_config(configs["openai"])
    backup_file(CLAUDE_CONFIG_PATH)
    with CLAUDE_CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(configs["claude"], f, sort_keys=False, allow_unicode=True)


def configure_claude_code_settings(
    settings_path: Path,
    base_url: str,
    auth_token: str,
) -> dict[str, str]:
    """Merge RelayDeck discovery settings without replacing user preferences."""
    settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Claude Code settings.json is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise ValueError("Claude Code settings.json must contain a JSON object")
        settings = loaded

    current_env = settings.get("env")
    if current_env is None:
        current_env = {}
    if not isinstance(current_env, dict):
        raise ValueError("Claude Code settings env must be a JSON object")

    backup_path = backup_file(settings_path)
    current_env.update(
        {
            "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }
    )
    settings["env"] = current_env
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "settings_path": str(settings_path),
        "backup_path": str(backup_path) if backup_path else "",
    }


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


def supplier_host_matches(platform_key: str, supported_host: str) -> bool:
    key = str(platform_key or "").strip().lower().lstrip(".")
    host = str(supported_host or "").strip().lower().lstrip(".")
    return bool(key and host and (key == host or key.endswith(f".{host}")))


def resolve_supplier_quota_adapter(
    platform_key: str,
    *,
    adapter_scripts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = str(platform_key or "").strip().lower()
    for known_host, adapter in KNOWN_PORTAL_HOST_ADAPTERS.items():
        if supplier_host_matches(key, known_host):
            return {
                "supported": True,
                "adapter": adapter,
                "adapter_id": adapter,
                "label": BUILTIN_QUOTA_ADAPTER_LABELS.get(adapter, adapter),
                "source": "builtin",
                "script_name": "",
            }

    scripts = list_quota_adapter_scripts() if adapter_scripts is None else adapter_scripts
    for item in scripts:
        manifest = ensure_mapping(item.get("manifest"))
        supported_hosts = [str(host).strip().lower() for host in ensure_list(manifest.get("supported_hosts"))]
        if not any(supplier_host_matches(key, host) for host in supported_hosts):
            continue
        script_name = str(item.get("name") or "").strip()
        return {
            "supported": True,
            "adapter": "custom-script",
            "adapter_id": str(manifest.get("id") or item.get("stem") or script_name),
            "label": str(manifest.get("display_name") or manifest.get("name") or item.get("stem") or script_name),
            "source": "script",
            "script_name": script_name,
        }

    return {
        "supported": False,
        "adapter": "unsupported",
        "adapter_id": "",
        "label": "暂未安装额度适配器",
        "source": "none",
        "script_name": "",
    }


def persisted_supplier_quota_config(quota: dict[str, Any] | None) -> dict[str, Any]:
    public_config, _ = split_quota_credentials(quota)
    config = {
        key: value
        for key, value in public_config.items()
        if key in PERSISTED_SUPPLIER_QUOTA_FIELDS and value not in (None, "")
    }
    if "pricing_automation" in config:
        config["pricing_automation"] = normalize_pricing_automation(config["pricing_automation"])
    return config


def resolved_supplier_quota_config(platform_key: str, quota: dict[str, Any] | None) -> dict[str, Any]:
    config = persisted_supplier_quota_config(quota)
    config.setdefault("pricing_automation", "detect-confirm")
    config.setdefault("billing_mode", "balance")
    resolution = resolve_supplier_quota_adapter(platform_key)
    resolved = {
        **config,
        "adapter": resolution["adapter"],
        "adapter_resolution": resolution,
    }
    if resolution.get("script_name"):
        resolved["script_name"] = resolution["script_name"]
    return resolved


def normalize_pricing_automation(value: Any) -> str:
    mode = str(value or "detect-confirm").strip().lower()
    return mode if mode in PRICING_AUTOMATION_MODES else "detect-confirm"


def normalize_billing_mode(value: Any) -> str:
    mode = str(value or "balance").strip().lower()
    return mode if mode in BILLING_MODES else "balance"


def apply_billing_mode_snapshot(
    snapshot: dict[str, Any],
    *,
    previous_snapshot: dict[str, Any] | None = None,
    billing_mode: str = "balance",
) -> dict[str, Any]:
    normalized = dict(snapshot or {})
    mode = normalize_billing_mode(billing_mode)
    normalized["billing_mode"] = mode

    total = numeric_value(normalized.get("balance_total"))
    used = numeric_value(normalized.get("balance_used"))
    remaining = numeric_value(normalized.get("balance_remaining"))
    if mode == "subscription":
        if used is None and total is not None and remaining is not None:
            used = max(0.0, total - remaining)
        if remaining is None and total is not None and used is not None:
            remaining = max(0.0, total - used)
        normalized["balance_total"] = total
        normalized["balance_used"] = used
        normalized["balance_remaining"] = remaining
        return normalized

    if remaining is None:
        return normalized
    previous = previous_snapshot or {}
    previous_remaining = numeric_value(previous.get("balance_remaining", previous.get("remaining")))
    baseline = previous_remaining
    total = remaining if baseline is None or remaining > baseline else baseline
    normalized["balance_total"] = total
    normalized["balance_remaining"] = remaining
    normalized["balance_used"] = max(0.0, total - remaining)
    return normalized


def parse_quota_items(value: Any) -> list[dict[str, Any]]:
    raw = value
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def snapshot_quota_items(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return quota sources from a snapshot, keeping legacy scalar snapshots usable."""
    current = dict(snapshot or {})
    items = parse_quota_items(current.get("quota_items"))
    if items:
        if any(
            item.get(key) not in (None, "")
            for item in items
            for key in ("balance_total", "balance_used", "balance_remaining")
        ):
            return items
        items = []
    if not any(current.get(key) not in (None, "") for key in ("balance_total", "balance_used", "balance_remaining")):
        return []
    billing_mode = normalize_billing_mode(current.get("billing_mode"))
    item_type = "subscription" if billing_mode == "subscription" else "balance"
    return [{
        "id": item_type,
        "type": item_type,
        "billing_mode": item_type,
        "label": "套餐额度" if item_type == "subscription" else "按量余额",
        "currency": current.get("currency") or "CNY",
        "balance_total": current.get("balance_total"),
        "balance_used": current.get("balance_used"),
        "balance_remaining": current.get("balance_remaining"),
        "period_start": current.get("period_start") or "",
        "period_end": current.get("period_end") or "",
        "status": current.get("status") or "unknown",
        "source": current.get("adapter") or "snapshot",
    }]


def normalize_quota_items(
    raw_items: Any,
    *,
    fallback_snapshot: dict[str, Any] | None = None,
    previous_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    fallback = dict(fallback_snapshot or {})
    source_items = parse_quota_items(raw_items)
    if not source_items:
        source_items = [
            {
                "id": "balance" if normalize_billing_mode(fallback.get("billing_mode")) == "balance" else "subscription",
                "type": normalize_billing_mode(fallback.get("billing_mode")),
                "label": "按量余额" if normalize_billing_mode(fallback.get("billing_mode")) == "balance" else "套餐额度",
                **fallback,
            }
        ]

    previous_items = {
        str(item.get("id") or "").strip(): item
        for item in parse_quota_items((previous_snapshot or {}).get("quota_items"))
        if str(item.get("id") or "").strip()
    }
    normalized_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(source_items):
        raw_type = str(raw_item.get("type") or raw_item.get("kind") or raw_item.get("billing_mode") or "balance").strip().lower()
        item_type = "subscription" if raw_type in {"subscription", "package", "plan", "套餐额度"} else "balance"
        item_id = str(raw_item.get("id") or raw_item.get("name") or f"{item_type}-{index + 1}").strip() or f"{item_type}-{index + 1}"
        if item_id in seen_ids:
            item_id = f"{item_id}-{index + 1}"
        seen_ids.add(item_id)
        item = {
            "id": item_id,
            "label": str(raw_item.get("label") or raw_item.get("name") or ("套餐额度" if item_type == "subscription" else "按量余额")),
            "type": item_type,
            "billing_mode": item_type,
            "currency": str(raw_item.get("currency") or fallback.get("currency") or "CNY"),
            "balance_total": raw_item.get("balance_total", raw_item.get("total")),
            "balance_used": raw_item.get("balance_used", raw_item.get("used")),
            "balance_remaining": raw_item.get("balance_remaining", raw_item.get("remaining")),
            "period_start": str(raw_item.get("period_start") or raw_item.get("start_time") or ""),
            "period_end": str(raw_item.get("period_end") or raw_item.get("end_time") or raw_item.get("expires_at") or ""),
            "status": str(raw_item.get("status") or "ok"),
            "source": str(raw_item.get("source") or "supplier-adapter"),
        }
        adjusted = apply_billing_mode_snapshot(
            item,
            previous_snapshot=previous_items.get(item_id),
            billing_mode=item_type,
        )
        normalized_items.append(adjusted)
    return normalized_items


def normalize_pricing_observation(
    platform_key: str,
    profile: dict[str, Any],
    observation: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    raw = ensure_mapping(observation)
    multiplier = float(raw.get("multiplier") if raw.get("multiplier") not in (None, "") else 1)
    if multiplier < 0:
        raise ValueError("pricing multiplier cannot be negative")

    raw_input = raw.get("input_per_1m")
    raw_output = raw.get("output_per_1m")
    base_input = raw.get("base_input_per_1m")
    base_output = raw.get("base_output_per_1m")
    if base_input in (None, ""):
        base_input = float(raw_input or 0) / multiplier if multiplier else float(raw_input or 0)
    if base_output in (None, ""):
        base_output = float(raw_output or 0) / multiplier if multiplier else float(raw_output or 0)
    base_input = float(base_input or 0)
    base_output = float(base_output or 0)
    input_per_1m = float(raw_input) if raw_input not in (None, "") else base_input * multiplier
    output_per_1m = float(raw_output) if raw_output not in (None, "") else base_output * multiplier
    if min(base_input, base_output, input_per_1m, output_per_1m) < 0:
        raise ValueError("pricing values cannot be negative")

    api_profile_id = str(raw.get("api_profile_id") or profile.get("id") or "").strip()
    upstream_model = str(raw.get("upstream_model") or "*").strip() or "*"
    currency = str(raw.get("currency") or "USD").strip().upper() or "USD"
    fingerprint_payload = {
        "api_profile_id": api_profile_id,
        "upstream_model": upstream_model,
        "base_input_per_1m": round(base_input, 12),
        "base_output_per_1m": round(base_output, 12),
        "multiplier": round(multiplier, 12),
        "input_per_1m": round(input_per_1m, 12),
        "output_per_1m": round(output_per_1m, 12),
        "currency": currency,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "platform_key": str(platform_key or "").strip().lower(),
        "api_profile_id": api_profile_id,
        "upstream_model": upstream_model,
        "group_name": str(raw.get("group_name") or "").strip(),
        "base_input_per_1m": base_input,
        "base_output_per_1m": base_output,
        "multiplier": multiplier,
        "input_per_1m": input_per_1m,
        "output_per_1m": output_per_1m,
        "currency": currency,
        "effective_from": str(raw.get("effective_from") or now_iso_local()).strip(),
        "status": "candidate",
        "source": str(source or "adapter").strip().lower(),
        "confidence": str(raw.get("confidence") or ("exact" if raw.get("effective_from") else "inferred")).strip().lower(),
        "note": str(raw.get("note") or "").strip(),
        "fingerprint": fingerprint,
    }


def pricing_change_ratio(current: dict[str, Any] | None, observed: dict[str, Any]) -> float:
    if not current:
        return 0.0
    ratios: list[float] = []
    for key in ("input_per_1m", "output_per_1m"):
        before = float(current.get(key) or 0)
        after = float(observed.get(key) or 0)
        if before == after:
            ratios.append(0.0)
        elif before <= 0:
            ratios.append(float("inf"))
        else:
            ratios.append(abs(after - before) / before)
    return max(ratios or [0.0])


def process_pricing_observations(
    platform_key: str,
    profile: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    automation_mode: str,
    store: Any | None = None,
) -> dict[str, Any]:
    mode = normalize_pricing_automation(automation_mode)
    pricing_store = store or get_usage_quota_store()
    summary = {
        "mode": mode,
        "observed": 0,
        "unchanged": 0,
        "ignored": 0,
        "candidates": 0,
        "applied": 0,
        "requires_confirmation": 0,
        "items": [],
    }
    for raw in observations:
        normalized = normalize_pricing_observation(platform_key, profile, raw, source="adapter")
        summary["observed"] += 1
        current = pricing_store.get_active_pricing_version(
            normalized["api_profile_id"], normalized["upstream_model"]
        )
        if current and current.get("fingerprint") == normalized["fingerprint"]:
            summary["unchanged"] += 1
            continue
        if mode == "manual":
            summary["ignored"] += 1
            continue
        candidate = pricing_store.observe_pricing_candidate(normalized)
        summary["candidates"] += 1
        summary["items"].append(candidate)
        if mode != "auto-apply" or int(candidate.get("observation_count") or 0) < 2:
            continue
        if pricing_change_ratio(current, candidate) > 0.5:
            summary["requires_confirmation"] += 1
            continue
        pricing_store.activate_pricing_version(candidate["id"])
        summary["applied"] += 1
    return summary


def pricing_catalog_from_adapter_payload(
    payload: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    capabilities = {
        str(item or "").strip().lower()
        for item in ensure_list(ensure_mapping(manifest).get("capabilities"))
    }
    if "fetch_pricing" not in capabilities:
        return []
    return [dict(item) for item in ensure_list(payload.get("pricing_catalog")) if isinstance(item, dict)]


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
    inferred_adapter = infer_portal_quota_adapter(profile)
    if inferred_adapter:
        return inferred_adapter
    adapter = str(quota.get("adapter") or "").strip().lower()
    return adapter if adapter in {"custom-script", "unsupported"} else "unsupported"


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
            "Provider=anthropic 支持填写根地址或已经包含 /v1 的 API Base；"
            f"当前地址 {value} 会自动使用 {base}/v1/models 和 {base}/v1/messages，避免重复拼接 /v1。"
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


def supplier_platform_key(api_base: str | None, fallback: str | None = None) -> str:
    parsed = urlparse(str(api_base or "").strip())
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    if hostname:
        parts = [part for part in hostname.split(".") if part]
        if len(parts) > 2:
            return ".".join(parts[-2:])
        return hostname
    return str(fallback or "未分组供应商").strip() or "未分组供应商"


def split_quota_credentials(quota: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    public_config: dict[str, Any] = {}
    credentials: dict[str, str] = {}
    for raw_key, value in ensure_mapping(quota).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if key in SENSITIVE_QUOTA_CREDENTIAL_FIELDS:
            if value not in (None, ""):
                credentials[key] = str(value)
            continue
        public_config[key] = value
    return public_config, credentials


def adapter_scoped_quota(quota: dict[str, Any], manifest: dict[str, Any] | None) -> dict[str, Any]:
    public_config, credentials = split_quota_credentials(quota)
    permissions = {
        str(item or "").strip()
        for item in ensure_list(ensure_mapping(manifest).get("credential_permissions"))
        if str(item or "").strip() in SENSITIVE_QUOTA_CREDENTIAL_FIELDS
    }
    return {**public_config, **{key: value for key, value in credentials.items() if key in permissions}}


def redact_sensitive_data(value: Any, secret_values: list[str] | None = None) -> Any:
    secrets = [item for item in (secret_values or []) if item]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower()
            if any(fragment in normalized_key for fragment in SENSITIVE_KEY_FRAGMENTS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_sensitive_data(item, secrets)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_data(item, secrets) for item in value]
    if isinstance(value, str):
        redacted_value = value
        for secret in secrets:
            if secret and secret in redacted_value:
                redacted_value = redacted_value.replace(secret, "[redacted]")
        return redacted_value
    return value


def supplier_platform_key_for_profile(
    profile: dict[str, Any],
    suppliers_by_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    supplier = (suppliers_by_id or {}).get(str(profile.get("supplier_id") or ""), {})
    fallback = supplier.get("name") or supplier.get("label") or profile.get("label")
    return supplier_platform_key(profile.get("api_base"), fallback)


def supplier_platform_provider_key(platform_key: str) -> str:
    return make_stable_id("billing-platform", str(platform_key or "").strip().lower())


def supplier_quota_targets(
    management_state: dict[str, Any],
    allowed_profile_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    suppliers_by_id = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for profile in management_state.get("api_profiles", []):
        if not isinstance(profile, dict) or not profile.get("id"):
            continue
        key = supplier_platform_key_for_profile(profile, suppliers_by_id)
        grouped.setdefault(key, []).append(profile)

    supplier_quotas = ensure_mapping(management_state.get("supplier_quotas"))
    targets: list[dict[str, Any]] = []
    for platform_key, profiles in grouped.items():
        selected_profile_ids = [
            str(profile.get("id") or "")
            for profile in profiles
            if allowed_profile_ids is None or str(profile.get("id") or "") in allowed_profile_ids
        ]
        if allowed_profile_ids is not None and not selected_profile_ids:
            continue
        representative = next((profile for profile in profiles if profile.get("enabled", True)), profiles[0])
        targets.append(
            {
                "platform_key": platform_key,
                "provider_key": supplier_platform_provider_key(platform_key),
                "profiles": profiles,
                "selected_profile_ids": selected_profile_ids,
                "representative_profile": representative,
                "quota": resolved_supplier_quota_config(platform_key, ensure_mapping(supplier_quotas.get(platform_key))),
            }
        )
    return sorted(targets, key=lambda item: item["platform_key"])


def browser_sso_adapter_for_target(target: dict[str, Any]) -> dict[str, Any] | None:
    platform_key = str(target.get("platform_key") or "").strip().lower()
    quota = ensure_mapping(target.get("quota"))
    adapter_id = str(quota.get("adapter") or "").strip().lower()
    if adapter_id in BUILTIN_BROWSER_SSO_ADAPTERS:
        try:
            return browser_sso_adapter_for_supplier(platform_key, adapter_id)
        except ValueError:
            return None

    if adapter_id != "custom-script":
        return None
    script_name = str(quota.get("script_name") or "").strip()
    script_path = resolve_quota_adapter_script(script_name)
    if not script_path:
        return None
    try:
        manifest = load_quota_adapter_manifest(script_path)
    except Exception:
        return None
    raw_config = ensure_mapping(manifest.get("browser_sso"))
    if not raw_config:
        return None
    portal_hosts = [
        str(host).strip().lower()
        for host in ensure_list(raw_config.get("portal_hosts") or manifest.get("supported_hosts"))
        if str(host).strip()
    ]
    if not portal_hosts or not str(raw_config.get("auth_me_path") or "/api/v1/auth/me").strip():
        return None
    return {
        "id": str(manifest.get("id") or script_path.stem),
        "display_name": str(manifest.get("display_name") or script_path.stem),
        "supplier_key": platform_key,
        "portal_hosts": portal_hosts,
        "login_path": str(raw_config.get("login_path") or "/dashboard"),
        "auth_me_path": str(raw_config.get("auth_me_path") or "/api/v1/auth/me"),
        "cookie_domains": [
            str(domain).strip().lower()
            for domain in ensure_list(raw_config.get("cookie_domains") or portal_hosts)
            if str(domain).strip()
        ],
        "google_rejection_check": bool(raw_config.get("google_rejection_check", True)),
    }


def migrate_legacy_supplier_quota_state(
    state: dict[str, Any],
    credential_vault: CredentialVault | Any | None = None,
) -> tuple[dict[str, Any], bool]:
    migrated = copy.deepcopy(state or {})
    vault = credential_vault or get_credential_vault()
    suppliers = [item for item in ensure_list(migrated.get("suppliers")) if isinstance(item, dict)]
    suppliers_by_id = {str(item.get("id") or ""): item for item in suppliers if item.get("id")}
    profiles = [item for item in ensure_list(migrated.get("api_profiles")) if isinstance(item, dict)]
    raw_supplier_quotas = migrated.get("supplier_quotas")
    supplier_quotas: dict[str, dict[str, Any]] = {}
    changed = not isinstance(raw_supplier_quotas, dict)

    for raw_key, raw_quota in ensure_mapping(raw_supplier_quotas).items():
        key = str(raw_key or "").strip().lower()
        if not key:
            changed = True
            continue
        public_config, credentials = split_quota_credentials(ensure_mapping(raw_quota))
        if credentials:
            vault.merge(key, credentials)
            changed = True
        cleaned_config = persisted_supplier_quota_config(public_config)
        if cleaned_config != public_config:
            changed = True
        supplier_quotas[key] = cleaned_config

    for profile in profiles:
        legacy_quota = ensure_mapping(profile.get("quota"))
        if not legacy_quota:
            profile["quota"] = {}
            continue
        platform_key = supplier_platform_key_for_profile(profile, suppliers_by_id)
        public_config, credentials = split_quota_credentials(legacy_quota)
        if credentials:
            vault.merge(platform_key, credentials)
        target = supplier_quotas.setdefault(platform_key, {})
        for key, value in public_config.items():
            if key not in target or target[key] in (None, ""):
                target[key] = value
        profile["quota"] = {}
        changed = True

    for platform_key, quota in list(supplier_quotas.items()):
        cleaned_quota = persisted_supplier_quota_config(quota)
        if cleaned_quota != quota:
            changed = True
        supplier_quotas[platform_key] = cleaned_quota

    migrated["api_profiles"] = profiles
    migrated["supplier_quotas"] = supplier_quotas
    return migrated, changed


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
    billing_mode = normalize_billing_mode((latest_balance or {}).get("billing_mode") or quota.get("billing_mode"))

    if balance_total in (None, "") and manual_limit not in (None, ""):
        balance_total = float(manual_limit)
    if balance_used in (None, "") and balance_total not in (None, "") and balance_remaining not in (None, ""):
        balance_used = float(balance_total) - float(balance_remaining)
    if balance_remaining in (None, "") and balance_total not in (None, "") and balance_used not in (None, ""):
        balance_remaining = float(balance_total) - float(balance_used)

    cycle_adjusted = (
        apply_latest_recharge_cycle(
            recharge_records,
            balance_total=balance_total,
            balance_used=balance_used,
            balance_remaining=balance_remaining,
        )
        if billing_mode == "balance"
        else {
            "balance_total": balance_total,
            "balance_used": balance_used,
            "balance_remaining": balance_remaining,
            "cycle_recharge_record": None,
            "quota_message": "",
        }
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
        "billing_mode": billing_mode,
        "quota_items": snapshot_quota_items(latest_balance),
        "period_start": (latest_balance or {}).get("period_start") or quota.get("period_start") or "",
        "period_end": (latest_balance or {}).get("period_end") or quota.get("period_end") or "",
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
    cash_costs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    matched_events = [item for item in usage_events if usage_event_matches_binding(item, route, binding)]
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    estimated_cost = 0.0
    actual_cost = 0.0
    actual_cost_count = 0
    cash_cost_cny = 0.0
    cash_cost_count = 0
    cash_tokens = 0
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
        event_id = str(item.get("id") or item.get("request_id") or "")
        attribution = ensure_mapping((cash_costs or {}).get(event_id))
        attribution_status = str(attribution.get("status") or "")
        if attribution_status.startswith("attributed-") and float(attribution.get("total_cny") or 0) > 0:
            cash_cost_cny += float(attribution.get("total_cny") or 0)
            cash_cost_count += 1
            cash_tokens += int(item.get("total_tokens") or 0)
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
    if cash_tokens > 0 and cash_cost_count > 0 and cash_cost_cny > 0:
        effective_cost_per_1m = cash_cost_cny / cash_tokens * 1_000_000
        tokens_per_cost_unit = cash_tokens / cash_cost_cny
        cost_currency = "CNY"
        cost_source = "actual-cash"
    elif total_tokens > 0 and actual_cost_count > 0 and actual_cost > 0:
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
        "cash_cost_cny": round(cash_cost_cny, 6) if cash_cost_count else None,
        "cash_cost_count": cash_cost_count,
        "cash_tokens": cash_tokens,
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


def latest_balance_for_supplier_platform(
    store: Any,
    platform_key: str,
    *,
    provider_key: str | None = None,
) -> dict[str, Any] | None:
    resolved_provider_key = provider_key or supplier_platform_provider_key(platform_key)
    snapshots = store.list_provider_balance_snapshots(limit=25, provider_key=resolved_provider_key)
    if not snapshots:
        return None
    latest = dict(snapshots[0])
    latest["stale_balance"] = False
    has_quota_data = bool(snapshot_quota_items(latest))
    if not has_quota_data:
        previous = next(
            (
                item
                for item in snapshots[1:]
                if snapshot_quota_items(item)
            ),
            None,
        )
        if previous:
            for key in ("balance_total", "balance_used", "balance_remaining", "billing_mode", "quota_items", "currency", "period_start", "period_end"):
                latest[key] = previous.get(key)
            latest["stale_balance"] = True
            latest["balance_value_checked_at"] = previous.get("checked_at")
    latest["quota_items"] = snapshot_quota_items(latest)
    if not latest.get("stale_balance") and normalize_billing_mode(latest.get("billing_mode")) == "balance":
        previous_remaining_snapshot = next(
            (
                item
                for item in snapshots[1:]
                if str(item.get("status") or "") == "ok"
                and item.get("balance_remaining") not in (None, "")
            ),
            None,
        )
        latest = apply_billing_mode_snapshot(
            latest,
            previous_snapshot=previous_remaining_snapshot,
            billing_mode="balance",
        )
    return latest


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
        return "未启用自动额度监控。"

    adapter = effective_quota_adapter(profile)
    if adapter == "custom-script":
        if not str(quota.get("script_name") or quota.get("script_path") or "").strip():
            return "额度适配器清单缺少脚本文件名。"
        return "已自动绑定供应商额度脚本。"

    if adapter in {"lingsuan-web", "autocode-web"}:
        if not str(quota.get("auth_token") or quota.get("session_cookie") or "").strip():
            return "网站登录凭据尚未建立，请先完成供应商登录。"
        return "已自动绑定网站额度适配器，可刷新额度。"

    return "暂未安装该供应商的额度适配器。"


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
    platform_key: str | None = None,
) -> list[dict[str, Any]]:
    provider_keys = {
        supplier_provider_key(supplier_id, provider_name),
    }
    if platform_key:
        provider_keys.add(supplier_platform_provider_key(platform_key))
    records_by_id: dict[str, dict[str, Any]] = {}
    for provider_key in provider_keys:
        for item in store.list_recharge_records(limit=500, provider_key=provider_key):
            records_by_id[str(item.get("id") or f"{provider_key}:{len(records_by_id)}")] = item
    return list(records_by_id.values())


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


def supplier_cost_attribution_snapshot(
    management_state: dict[str, Any],
    store: Any,
    start_at: str,
    end_at: str,
) -> dict[str, dict[str, Any]]:
    suppliers = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    profiles = [
        item
        for item in management_state.get("api_profiles", [])
        if isinstance(item, dict) and item.get("id")
    ]
    profiles_by_platform: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        supplier = suppliers.get(str(profile.get("supplier_id") or ""), {})
        platform_key = supplier_platform_key(profile.get("api_base"), supplier.get("name"))
        profiles_by_platform.setdefault(platform_key, []).append(profile)

    start_dt = iso_to_dt(start_at)
    end_dt = iso_to_dt(end_at)
    all_events = store.list_usage_events(limit=10000, source="litellm_gateway")
    result: dict[str, dict[str, Any]] = {}
    for platform_key, platform_profiles in profiles_by_platform.items():
        profile_ids = {str(item.get("id") or "") for item in platform_profiles}
        events: list[dict[str, Any]] = []
        for item in all_events:
            item_platform = str(item.get("platform_key") or "").strip().lower()
            if item_platform:
                if item_platform != platform_key:
                    continue
            elif str(item.get("api_profile_id") or "") not in profile_ids:
                continue
            created_dt = iso_to_dt(item.get("created_at"))
            if start_dt is not None and created_dt is not None and created_dt < start_dt:
                continue
            if end_dt is not None and created_dt is not None and created_dt > end_dt:
                continue
            events.append(item)

        provider_keys = {
            supplier_provider_key(
                profile.get("supplier_id"),
                profile.get("custom_llm_provider"),
            )
            for profile in platform_profiles
        }
        records_by_id: dict[str, dict[str, Any]] = {}
        for provider_key in provider_keys:
            for record in store.list_recharge_records(limit=1000, provider_key=provider_key):
                records_by_id[str(record.get("id") or f"{provider_key}:{len(records_by_id)}")] = record
        platform_provider_key = supplier_platform_provider_key(platform_key)
        for record in store.list_recharge_records(limit=1000, provider_key=platform_provider_key):
            records_by_id[str(record.get("id") or f"{platform_provider_key}:{len(records_by_id)}")] = record
        records = list(records_by_id.values())
        latest_balance = latest_balance_for_supplier_platform(store, platform_key)
        quota_items = parse_quota_items((latest_balance or {}).get("quota_items"))
        attribution = attribute_supplier_costs(events, records, start_at, end_at)
        subscription_usage = subscription_usage_summary(records, quota_items, as_of=end_at)
        gateway_requests = len(events)
        priced_requests = sum(1 for item in events if item.get("pricing_version_id"))
        currencies = sorted(
            {
                str(item.get("currency") or "").strip().upper()
                for item in events
                if str(item.get("currency") or "").strip()
            }
        )
        topup_bases = {
            currency: weighted_topup_basis(records, currency)
            for currency in currencies
        }
        current_subscription = subscription_amortization(records, end_at, end_at)
        recent_events = [
            {
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "request_id": item.get("request_id"),
                "public_model_name": item.get("public_model_name"),
                "upstream_model": item.get("upstream_model"),
                "api_profile_id": item.get("api_profile_id"),
                "route_binding_id": item.get("route_binding_id"),
                "pricing_version_id": item.get("pricing_version_id"),
                "prompt_tokens": int(item.get("prompt_tokens") or 0),
                "completion_tokens": int(item.get("completion_tokens") or 0),
                "total_tokens": int(item.get("total_tokens") or 0),
                "estimated_cost": item.get("estimated_cost"),
                "currency": item.get("currency"),
                "latency_ms": item.get("latency_ms"),
                "status": item.get("status"),
                "cost_attribution_status": item.get("cost_attribution_status"),
            }
            for item in sorted(events, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:20]
        ]
        result[platform_key] = {
            "platform_key": platform_key,
            "gateway_requests": gateway_requests,
            "priced_requests": priced_requests,
            "pricing_coverage": round(priced_requests / gateway_requests, 4) if gateway_requests else None,
            "topup_cash_cost_cny": round(float(attribution["topup_cash_cost_cny"]), 6),
            "subscription_amortized_cny": round(float(attribution["subscription_amortized_cny"]), 6),
            "subscription_allocated_cny": round(float(attribution["subscription_allocated_cny"]), 6),
            "subscription_unallocated_cny": round(float(attribution["subscription_unallocated_cny"]), 6),
            "subscription_usage": subscription_usage,
            "subscription_consumed_cost_cny": round(float(subscription_usage["consumed_cost_cny"]), 6),
            "subscription_full_use_cost_per_credit_cny": round(float(subscription_usage["full_use_cost_per_credit_cny"]), 6) if subscription_usage.get("full_use_cost_per_credit_cny") is not None else None,
            "subscription_projected_expiry_loss_cny": round(float(subscription_usage["projected_expiry_loss_cny"]), 6),
            "subscription_confirmed_expiry_loss_cny": round(float(subscription_usage["confirmed_expiry_loss_cny"]), 6),
            "attributed_cash_cost_cny": round(float(attribution["attributed_cash_cost_cny"]), 6),
            "unattributed_requests": int(attribution["unattributed_requests"]),
            "current_subscription_daily_cny": round(float(current_subscription["amortized_cny"]), 6),
            "cost_batch_count": len(records),
            "topup_bases": topup_bases,
            "event_costs": attribution["event_costs"],
            "recent_events": recent_events,
        }
    return result


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
    grouped: dict[str, dict[str, Any]] = {}
    for row in summary_rows:
        supplier_id = str(row.get("supplier_id") or "").strip()
        platform_key = supplier_platform_key(row.get("api_base"), row.get("supplier_name"))
        group_key = platform_key
        group = grouped.get(group_key)
        if group is None:
            group = {
                "supplier_id": supplier_id,
                "supplier_ids": set(),
                "platform_key": platform_key,
                "supplier_name": platform_key,
                "provider": normalize_provider_name(row.get("provider")),
                "rows": [],
                "api_labels": [],
                "latest_balance": None,
                "latest_balance_dt": None,
                "quota_message": "",
            }
            grouped[group_key] = group

        if supplier_id:
            group["supplier_ids"].add(supplier_id)
        group["rows"].append(row)
        api_label = str(row.get("api_label") or "").strip()
        if api_label and api_label not in group["api_labels"]:
            group["api_labels"].append(api_label)

        checked_at = row.get("balance_checked_at")
        latest_balance_dt = iso_to_dt(checked_at)
        if checked_at and latest_balance_dt is not None:
            current_latest_dt = group.get("latest_balance_dt")
            if current_latest_dt is None or latest_balance_dt > current_latest_dt:
                supplier_records = []
                for grouped_supplier_id in group["supplier_ids"]:
                    supplier_records.extend(
                        recharge_records_for_supplier(
                            store,
                            grouped_supplier_id,
                            platform_key=group.get("platform_key"),
                        )
                    )
                group["latest_balance"] = {
                    "checked_at": checked_at,
                    "status": row.get("balance_status"),
                    "adapter": row.get("balance_source"),
                    "currency": row.get("currency"),
                    "balance_total": row.get("balance_total"),
                    "balance_used": row.get("balance_used"),
                    "balance_remaining": row.get("balance_remaining"),
                    "billing_mode": row.get("billing_mode"),
                    "quota_items": snapshot_quota_items(row),
                    "period_start": row.get("period_start"),
                    "period_end": row.get("period_end"),
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
        billing_mode = normalize_billing_mode(latest_balance.get("billing_mode"))
        quota_message = quota_message_from_snapshot(latest_balance) or str(group.get("quota_message") or "")
        cycle_adjusted = (
            apply_latest_recharge_cycle(
                supplier_recharge_records,
                balance_total=balance_total,
                balance_used=balance_used,
                balance_remaining=balance_remaining,
                quota_message=quota_message,
            )
            if billing_mode == "balance"
            else {
                "balance_total": balance_total,
                "balance_used": balance_used,
                "balance_remaining": balance_remaining,
                "quota_message": quota_message,
                "cycle_recharge_record": None,
            }
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
                "platform_key": group["platform_key"],
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
                "billing_mode": billing_mode,
                "quota_items": snapshot_quota_items(latest_balance),
                "period_start": latest_balance.get("period_start"),
                "period_end": latest_balance.get("period_end"),
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


def aggregate_model_usage_rows(
    management_state: dict[str, Any],
    store: Any,
    cash_costs: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
                cash_costs,
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
    window_start = month_start_iso_local()
    window_end = now_iso_local()
    profiles = management_state.get("api_profiles", [])
    suppliers = {item["id"]: item for item in management_state.get("suppliers", []) if isinstance(item, dict) and item.get("id")}
    supplier_quotas = ensure_mapping(management_state.get("supplier_quotas"))
    summary_rows: list[dict[str, Any]] = []
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_estimated_cost = 0.0
    total_actual_cost = 0.0
    total_paid_cny = 0.0
    alerts: list[dict[str, Any]] = []
    for profile in profiles:
        platform_key = supplier_platform_key_for_profile(profile, suppliers)
        supplier_quota = split_quota_credentials(ensure_mapping(supplier_quotas.get(platform_key)))[0]
        profile_for_summary = {**profile, "quota": supplier_quota}
        usage_events = usage_events_for_profile(store, profile, window_start)
        recharge_records = recharge_records_for_profile(store, profile)
        latest_balance = latest_balance_for_supplier_platform(store, platform_key) or latest_balance_for_profile(store, profile)
        row = summarize_profile_usage(profile_for_summary, usage_events, recharge_records, latest_balance)
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
                "platform_key": platform_key,
                "balance_source": (latest_balance or {}).get("adapter") or ("manual" if supplier_quota else "unknown"),
                "balance_checked_at": (latest_balance or {}).get("checked_at"),
                "balance_status": (latest_balance or {}).get("status") or "unknown",
                "balance_raw_summary": (latest_balance or {}).get("raw_summary"),
                "quota_message": row.get("cycle_quota_message") or quota_display_message(profile_for_summary, latest_balance),
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
    supplier_costs = supplier_cost_attribution_snapshot(
        management_state,
        store,
        window_start,
        window_end,
    )
    for row in supplier_rows:
        row["cost_attribution"] = supplier_costs.get(str(row.get("platform_key") or ""), {})
    event_costs: dict[str, Any] = {}
    for supplier_cost in supplier_costs.values():
        event_costs.update(ensure_mapping(supplier_cost.get("event_costs")))
    model_rows = [
        item
        for item in aggregate_model_usage_rows(management_state, store, event_costs)
        if item.get("cost_source") == "actual-cash"
    ]
    comparison_rows = sorted(
        model_rows,
        key=lambda item: (
            item.get("effective_cost_per_1m") if item.get("effective_cost_per_1m") is not None else float("inf"),
            -(item.get("success_rate") or 0),
        ),
    )
    return {
        "generated_at": window_end,
        "window": {
            "preset": "month_to_date",
            "start_at": window_start,
            "end_at": window_end,
        },
        "totals": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "estimated_cost": round(total_estimated_cost, 6),
            "actual_cost": round(total_actual_cost, 6) if total_actual_cost else None,
            "recharge_paid_cny": round(total_paid_cny, 4),
            "attributed_cash_cost_cny": round(
                sum(float(item.get("attributed_cash_cost_cny") or 0) for item in supplier_costs.values()),
                6,
            ),
            "subscription_amortized_cny": round(
                sum(float(item.get("subscription_amortized_cny") or 0) for item in supplier_costs.values()),
                6,
            ),
            "gateway_requests": sum(int(item.get("gateway_requests") or 0) for item in supplier_costs.values()),
            "priced_gateway_requests": sum(int(item.get("priced_requests") or 0) for item in supplier_costs.values()),
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
    adapter_id = infer_portal_quota_adapter(profile)
    for adapter in BUILTIN_BROWSER_SSO_ADAPTERS.values():
        if str(adapter.get("id") or "").strip().lower() == adapter_id:
            configured_base = str(adapter.get("portal_base") or "").strip().rstrip("/")
            if configured_base:
                return configured_base
    api_base = str(profile.get("api_base") or "").strip()
    parsed = urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def supplier_http_client(*, timeout: float) -> httpx.Client:
    """Use IPv4 for supplier sites whose IPv6 TLS route is unavailable locally."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)


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


def pick_text_from_payload(payload: Any, aliases: set[str]) -> str:
    value = recursive_find_key(payload, aliases)
    if value in (None, ""):
        return ""
    return str(value).strip()


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
    period_start = ""
    period_end = ""
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
        if not period_start:
            period_start = pick_text_from_payload(
                payload,
                {"period_start", "cycle_start", "start_time", "start_at", "service_start", "begin_time"},
            )
        if not period_end:
            period_end = pick_text_from_payload(
                payload,
                {"period_end", "cycle_end", "end_time", "end_at", "service_end", "expires_at", "expire_at", "valid_until"},
            )
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
        "period_start": period_start,
        "period_end": period_end,
    }


def extract_subscription_quota_items(payloads: dict[str, Any], default_currency: str = "CNY") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_name in ("subscriptions_summary", "subscriptions_active", "subscriptions_progress"):
        payload = payloads.get(source_name)
        candidates: list[Any] = []
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            for key in ("subscriptions", "plans", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            total = pick_numeric_from_payload(candidate, {"total", "limit", "quota_total", "total_quota", "credit_total"})
            used = pick_numeric_from_payload(candidate, {"used", "spent", "consumed", "usage", "used_amount"})
            remaining = pick_numeric_from_payload(candidate, {"remaining", "available", "remaining_balance", "available_balance"})
            if total is None and used is None and remaining is None:
                continue
            items.append(
                {
                    "id": str(candidate.get("id") or candidate.get("subscription_id") or candidate.get("plan_id") or f"subscription-{index + 1}"),
                    "type": "subscription",
                    "label": str(candidate.get("name") or candidate.get("title") or "套餐额度"),
                    "currency": str(candidate.get("currency") or candidate.get("unit") or default_currency),
                    "balance_total": total,
                    "balance_used": used,
                    "balance_remaining": remaining,
                    "period_start": pick_text_from_payload(candidate, {"period_start", "cycle_start", "start_time", "service_start"}),
                    "period_end": pick_text_from_payload(candidate, {"period_end", "cycle_end", "end_time", "expires_at", "service_end"}),
                    "source": source_name,
                }
            )
    return items


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

    try:
        manifest = load_quota_adapter_manifest(script_path)
    except Exception as exc:
        return {
            "status": "error",
            "adapter": adapter_name,
            "message": f"适配器权限清单读取失败：{exc}",
            "raw_summary": {"script": str(script_path)},
        }
    scoped_quota = adapter_scoped_quota(quota, manifest)

    context = {
        "profile": {
            "id": str(profile.get("id") or ""),
            "label": str(profile.get("label") or ""),
            "supplier_id": str(profile.get("supplier_id") or ""),
            "api_base": str(profile.get("api_base") or ""),
            "api_key_env": str(profile.get("api_key_env") or ""),
            "api_key_value": (
                str(profile.get("api_key_value") or "")
                if "api_key_value" in set(manifest.get("credential_permissions") or [])
                else ""
            ),
            "custom_llm_provider": normalize_provider_name(profile.get("custom_llm_provider")),
            "quota": scoped_quota,
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
        "billing_mode": normalize_billing_mode(payload.get("billing_mode") or quota.get("billing_mode")),
        "balance_total": numeric_value(payload.get("balance_total")),
        "balance_used": numeric_value(payload.get("balance_used")),
        "balance_remaining": numeric_value(payload.get("balance_remaining")),
        "period_start": str(payload.get("period_start") or ""),
        "period_end": str(payload.get("period_end") or ""),
        "quota_items": parse_quota_items(payload.get("quota_items")),
        "currency": str(payload.get("currency") or quota.get("currency") or "CNY"),
        "pricing_catalog": pricing_catalog_from_adapter_payload(payload, manifest),
        "credential_updates": {
            key: value
            for key, value in ensure_mapping(payload.get("credential_updates")).items()
            if key in set(manifest.get("credential_permissions") or []) and value not in (None, "")
        },
        "raw_summary": {
            **raw_summary,
            "script_output": payload.get("raw_summary") if isinstance(payload.get("raw_summary"), (dict, list, str)) else payload,
        },
    }


def probe_portal_quota(profile: dict[str, Any]) -> dict[str, Any]:
    quota = ensure_mapping(profile.get("quota"))
    adapter = effective_quota_adapter(profile)
    billing_mode = normalize_billing_mode(quota.get("billing_mode"))
    portal_base = portal_base_for_profile(profile)
    default_currency = str(quota.get("currency") or "CNY")
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = normalize_session_cookie_header(quota.get("session_cookie"))

    if not portal_base:
        return {
            "status": "unsupported",
            "adapter": adapter or "portal_web_token",
            "message": "缺少 portal_base，无法定位网站后台接口。",
            "raw_summary": {},
        }

    if adapter not in {"lingsuan-web", "autocode-web"}:
        return {
            "status": "unsupported",
            "adapter": adapter or "unsupported",
            "message": "当前供应商未绑定网站额度适配器。",
            "raw_summary": {"portal_base": portal_base},
        }
    if not auth_token and not session_cookie:
        return {
            "status": "unsupported",
            "adapter": adapter,
            "message": "缺少网站后台 auth_token / session_cookie，请先完成供应商登录。",
            "raw_summary": {"portal_base": portal_base},
        }

    headers: dict[str, str] = {"Accept": "application/json"}
    auth_mode = "website_token"
    raw_summary: dict[str, Any] = {
        "portal_base": portal_base,
        "auth_mode": auth_mode,
        "adapter": adapter,
        "session_cookie_configured": bool(session_cookie),
    }

    with supplier_http_client(timeout=20) as client:
        user_id = ""
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
                    user_id = extract_portal_user_id(normalize_portal_data(me_body))
                    break
                last_error_message = f"{attempt_mode} returned HTTP {me_resp.status_code}"
            except Exception as exc:
                raw_summary[f"auth_me_{attempt_mode}"] = {"error": str(exc)}
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
    quota_items = extract_subscription_quota_items(payloads, default_currency)
    if quota_items and status == "unsupported":
        status = "ok"
        message = ""
    if any(snapshot.get(key) is not None for key in ("balance_total", "balance_used", "balance_remaining")):
        quota_items.insert(
            0,
            {
                "id": "balance",
                "type": billing_mode,
                "label": "按量余额" if billing_mode == "balance" else "套餐额度",
                "currency": snapshot.get("currency") or default_currency,
                "balance_total": snapshot.get("balance_total"),
                "balance_used": snapshot.get("balance_used"),
                "balance_remaining": snapshot.get("balance_remaining"),
                "period_start": snapshot.get("period_start"),
                "period_end": snapshot.get("period_end"),
                "source": adapter,
            },
        )
    return {
        "status": status,
        "adapter": adapter,
        "message": message,
        "billing_mode": billing_mode,
        "quota_items": quota_items,
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

    with supplier_http_client(timeout=20) as client:
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
    suppliers_by_id = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    supplier_quotas = ensure_mapping(management_state.get("supplier_quotas"))
    updated = False
    for item in api_profiles:
        if str(item.get("id") or "").strip() != str(profile_id or "").strip():
            continue
        platform_key = supplier_platform_key_for_profile(item, suppliers_by_id)
        public_updates, credential_updates = split_quota_credentials(quota_updates)
        if credential_updates:
            get_credential_vault().merge(platform_key, credential_updates)
        quota = ensure_mapping(supplier_quotas.get(platform_key))
        quota.update({key: value for key, value in public_updates.items() if value not in (None, "")})
        supplier_quotas[platform_key] = quota
        item["quota"] = {}
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
        supplier_quotas,
    )


def repair_portal_login_state(
    profile: dict[str, Any],
    *,
    totp_code: str = "",
    supplier_key: str = "",
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

    with supplier_http_client(timeout=20) as client:
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
        if supplier_key:
            get_credential_vault().merge(supplier_key, quota_updates)
        else:
            save_management_state_profile_quota_fields(str(profile.get("id") or ""), quota_updates)

        result = {
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
        return redact_sensitive_data(result, list(quota_updates.values()))


def portal_probe_requires_browser_renewal(probe: dict[str, Any] | None) -> bool:
    status = str(ensure_mapping(probe).get("status") or "").strip().lower()
    message = str(ensure_mapping(probe).get("message") or "").strip().lower()
    if status not in {"error", "unsupported", "auth_required"}:
        return False
    auth_markers = ("401", "403", "unauthorized", "forbidden", "token expired", "jwt expired", "登录已失效", "令牌已过期")
    return any(marker in message for marker in auth_markers)


def renew_supplier_sso_silently(
    supplier_key: str,
    portal_base: str,
    *,
    adapter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _supplier_sso_manager.run_silent(
        supplier_key,
        portal_base,
        timeout_seconds=45,
        adapter=adapter,
    )


def refresh_balances_v2(
    payload: "BalanceRefreshPayload",
    *,
    silent_sso_refresher: Any | None = None,
) -> dict[str, Any]:
    management_state = load_management_state()
    allowed_profile_ids = set(payload.api_profile_ids) if payload.api_profile_ids else None
    targets = supplier_quota_targets(management_state, allowed_profile_ids)
    store = get_usage_quota_store()
    vault = get_credential_vault()
    sso_refresher = silent_sso_refresher or renew_supplier_sso_silently
    results: list[dict[str, Any]] = []

    for target in targets:
        platform_key = str(target.get("platform_key") or "")
        profile = copy.deepcopy(target.get("representative_profile") or {})
        quota = ensure_mapping(target.get("quota"))
        browser_sso_adapter = browser_sso_adapter_for_target(target)
        if not bool(quota.get("enabled", False)) and not payload.force:
            results.append(
                {
                    "supplier_key": platform_key,
                    "api_profile_ids": [item.get("id") for item in target.get("profiles", [])],
                    "label": platform_key,
                    "status": "disabled",
                    "source": "supplier",
                    "adapter": str(quota.get("adapter") or "unsupported"),
                    "message": "该供应商的自动额度监控已关闭。",
                }
            )
            continue
        try:
            credentials = vault.get(platform_key)
        except Exception as exc:
            credentials = {}
            logger.warning("supplier credential vault read failed for %s: %s", platform_key, exc)
        quota = {**quota, **credentials}
        profile["quota"] = quota
        adapter = effective_quota_adapter(profile)
        snapshot_adapter = adapter
        currency = str(quota.get("currency") or "CNY")
        auto_probe: dict[str, Any] | None = None

        if adapter == "custom-script":
            auto_probe = probe_custom_quota_script(profile)
            snapshot_adapter = str(auto_probe.get("adapter") or "custom-script")
            limit = auto_probe.get("balance_total")
            current_balance = auto_probe.get("balance_remaining")
            used_amount = auto_probe.get("balance_used")
            currency = str(auto_probe.get("currency") or currency)
            status = str(auto_probe.get("status") or "unsupported")
            message = str(auto_probe.get("message") or "")
        elif adapter in {"lingsuan-web", "autocode-web"}:
            web_adapters = {"lingsuan-web", "autocode-web"}
            login_result: dict[str, Any] | None = None
            if False and (
                adapter in web_adapters
                and not str(quota.get("auth_token") or "").strip()
                and not str(quota.get("session_cookie") or "").strip()
                and str(quota.get("login_email") or "").strip()
                and str(quota.get("login_password") or "").strip()
            ):
                login_result = repair_portal_login_state(profile, supplier_key=platform_key)
                if login_result.get("ok"):
                    credentials = vault.get(platform_key)
                    quota = {**ensure_mapping(target.get("quota")), **credentials}
                    profile["quota"] = quota

            auto_probe = probe_portal_quota(profile)
            probe_message = str(auto_probe.get("message") or "").lower()
            should_retry_login = False and (
                adapter in web_adapters
                and not (login_result or {}).get("ok")
                and str(quota.get("login_email") or "").strip()
                and str(quota.get("login_password") or "").strip()
                and ("401" in probe_message or "login" in probe_message or "auth" in probe_message)
            )
            if should_retry_login:
                login_result = repair_portal_login_state(profile, supplier_key=platform_key)
                if login_result.get("ok"):
                    credentials = vault.get(platform_key)
                    quota = {**ensure_mapping(target.get("quota")), **credentials}
                    profile["quota"] = quota
                    auto_probe = probe_portal_quota(profile)
                elif auto_probe.get("status") in {"unsupported", "error"}:
                    auto_probe["message"] = str(login_result.get("message") or auto_probe.get("message") or "登录态修复失败")
            if browser_sso_adapter and portal_probe_requires_browser_renewal(auto_probe):
                sso_result = ensure_mapping(
                    sso_refresher(
                        platform_key,
                        str(
                            quota.get("portal_base")
                            or browser_sso_adapter.get("portal_base")
                            or f"https://{platform_key}"
                        ).strip().rstrip("/"),
                        adapter=browser_sso_adapter,
                    )
                )
                if str(sso_result.get("status") or "") == "authenticated":
                    credentials = vault.get(platform_key)
                    quota = {**ensure_mapping(target.get("quota")), **credentials}
                    profile["quota"] = quota
                    auto_probe = probe_portal_quota(profile)
                elif bool(sso_result.get("requires_interaction")):
                    auto_probe["message"] = str(sso_result.get("message") or "浏览器会话需要重新授权。")
            snapshot_adapter = str(auto_probe.get("adapter") or adapter)
            limit = auto_probe.get("balance_total")
            current_balance = auto_probe.get("balance_remaining")
            used_amount = auto_probe.get("balance_used")
            currency = str(auto_probe.get("currency") or currency)
            status = str(auto_probe.get("status") or "unsupported")
            message = str(auto_probe.get("message") or "")
        else:
            auto_probe = {
                "status": "unsupported",
                "adapter": "unsupported",
                "message": f"供应商 {platform_key} 暂未安装额度适配器。",
                "raw_summary": {"platform_key": platform_key},
            }
            snapshot_adapter = "unsupported"
            limit = None
            current_balance = None
            used_amount = None
            status = "unsupported"
            message = str(auto_probe["message"])

        credential_updates = ensure_mapping((auto_probe or {}).get("credential_updates"))
        scoped_updates = {
            key: value
            for key, value in credential_updates.items()
            if key in SENSITIVE_QUOTA_CREDENTIAL_FIELDS and value not in (None, "")
        }
        if scoped_updates:
            try:
                vault.merge(platform_key, scoped_updates)
            except Exception as exc:
                logger.warning("supplier credential vault update failed for %s: %s", platform_key, exc)

        pricing_processing: list[dict[str, Any]] = []
        pricing_catalog = [
            item
            for item in ensure_list((auto_probe or {}).get("pricing_catalog"))
            if isinstance(item, dict)
        ]
        if pricing_catalog:
            profiles_by_id = {
                str(item.get("id") or ""): item
                for item in target.get("profiles", [])
                if isinstance(item, dict) and item.get("id")
            }
            default_profile_id = str(profile.get("id") or "")
            for pricing_observation in pricing_catalog:
                observation_profile_id = str(
                    pricing_observation.get("api_profile_id") or default_profile_id
                ).strip()
                observation_profile = profiles_by_id.get(observation_profile_id)
                if not observation_profile:
                    continue
                pricing_processing.append(
                    process_pricing_observations(
                        platform_key,
                        observation_profile,
                        [pricing_observation],
                        automation_mode=str(quota.get("pricing_automation") or "detect-confirm"),
                        store=store,
                    )
                )

        requested_billing_mode = normalize_billing_mode(
            (auto_probe or {}).get("billing_mode") or quota.get("billing_mode")
        )
        previous_snapshot = None
        raw_quota_items = parse_quota_items((auto_probe or {}).get("quota_items"))
        if current_balance not in (None, "") or raw_quota_items:
            provider_key = str(target.get("provider_key") or supplier_platform_provider_key(platform_key))
            previous_snapshot = next(
                (
                    item
                    for item in store.list_provider_balance_snapshots(limit=200, provider_key=provider_key)
                    if str(item.get("status") or "") == "ok"
                    and item.get("balance_remaining") not in (None, "")
                ),
                None,
            )
        fallback_snapshot = {
            "billing_mode": requested_billing_mode,
            "balance_total": limit,
            "balance_used": used_amount,
            "balance_remaining": current_balance,
            "currency": currency,
            "period_start": str((auto_probe or {}).get("period_start") or quota.get("period_start") or ""),
            "period_end": str((auto_probe or {}).get("period_end") or quota.get("period_end") or ""),
        }
        quota_items = normalize_quota_items(
            raw_quota_items,
            fallback_snapshot=fallback_snapshot,
            previous_snapshot=previous_snapshot,
        )
        primary_item = next((item for item in quota_items if item.get("type") == "balance"), quota_items[0])
        item_types = {str(item.get("type") or "balance") for item in quota_items}
        billing_mode = "mixed" if len(item_types) > 1 else normalize_billing_mode(next(iter(item_types), requested_billing_mode))
        limit = primary_item.get("balance_total")
        used_amount = primary_item.get("balance_used")
        current_balance = primary_item.get("balance_remaining")
        period_start = primary_item.get("period_start") or ""
        period_end = primary_item.get("period_end") or ""

        snapshot_raw_summary = (
            {
                "message": str(auto_probe.get("message") or ""),
                "status": str(auto_probe.get("status") or status),
                "adapter": str(auto_probe.get("adapter") or snapshot_adapter),
                "billing_mode": billing_mode,
                "quota_items": quota_items,
                "raw_summary": auto_probe.get("raw_summary"),
                "pricing_processing": pricing_processing,
            }
            if auto_probe is not None
            else {"source": "supplier-adapter", "force": payload.force}
        )
        snapshot_raw_summary = redact_sensitive_data(snapshot_raw_summary, list(credentials.values()))

        provider_key = str(target.get("provider_key") or supplier_platform_provider_key(platform_key))
        recharge_event = None
        recharge_event_created = False
        previous_remaining = numeric_value((previous_snapshot or {}).get("balance_remaining"))
        current_remaining_value = numeric_value(current_balance)
        if status == "ok" and previous_remaining is not None and current_remaining_value is not None:
            recharge_event, recharge_event_created = store.record_balance_increase(
                provider_key=provider_key,
                relay_label=platform_key,
                previous_balance=previous_remaining,
                current_balance=current_remaining_value,
                currency=currency,
                detected_at=now_iso_local(),
                previous_checked_at=str((previous_snapshot or {}).get("checked_at") or ""),
            )

        snapshot = store.insert_provider_balance_snapshot(
            {
                "provider_key": provider_key,
                "relay_label": platform_key,
                "api_key_env": "",
                "adapter": snapshot_adapter,
                "status": status,
                "currency": currency,
                "balance_total": float(limit) if limit not in (None, "") else None,
                "balance_used": float(used_amount) if used_amount not in (None, "") else None,
                "balance_remaining": float(current_balance) if current_balance not in (None, "") else None,
                "billing_mode": billing_mode,
                "quota_items": quota_items,
                "period_start": period_start,
                "period_end": period_end,
                "raw_summary": snapshot_raw_summary,
            }
        )

        results.append(
            {
                "supplier_key": platform_key,
                "api_profile_ids": [item.get("id") for item in target.get("profiles", [])],
                "label": platform_key,
                "status": status,
                "source": "auto" if auto_probe is not None else "manual",
                "adapter": snapshot_adapter,
                "currency": snapshot.get("currency"),
                "balance_total": snapshot.get("balance_total"),
                "balance_used": snapshot.get("balance_used"),
                "balance_remaining": snapshot.get("balance_remaining"),
                "billing_mode": snapshot.get("billing_mode"),
                "quota_items": quota_items,
                "period_start": snapshot.get("period_start"),
                "period_end": snapshot.get("period_end"),
                "checked_at": snapshot.get("checked_at"),
                "message": message,
                "pricing_processing": pricing_processing,
                "recharge_event": recharge_event,
                "recharge_event_created": recharge_event_created,
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


def anthropic_endpoint_url(api_base: str, endpoint: str) -> str:
    """Build an Anthropic endpoint without duplicating an existing /v1 prefix."""
    base = str(api_base or "").strip().rstrip("/")
    suffix = str(endpoint or "").strip().lstrip("/")
    if base.lower().endswith("/v1"):
        return f"{base}/{suffix}"
    return f"{base}/v1/{suffix}"


def provider_models_probe(provider: dict[str, Any], env_map: dict[str, str]) -> tuple[str, dict[str, str]]:
    provider_name = normalize_provider_name(provider.get("custom_llm_provider"))
    api_base = resolve_provider_api_base(provider.get("api_base"), provider_name)
    api_key = env_map.get(provider.get("api_key_env", ""), "").strip()
    if provider_name == "anthropic":
        return (
            anthropic_endpoint_url(api_base, "models"),
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
            anthropic_endpoint_url(api_base, "messages"),
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
        legacy_gateway_enabled = row.get("gateway_enabled")
        if legacy_gateway_enabled is None and "expose_in_gateway" not in row:
            # Existing state predates the publish flag and was already exposed by LiteLLM.
            legacy_gateway_enabled = True
        items.append(
            {
                **row,
                "id": route_id,
                "public_model_name": public_model_name,
                "display_name": str(row.get("display_name") or public_model_name).strip(),
                "model_family": str(row.get("model_family") or row.get("category") or guess_model_family(public_model_name)).strip(),
                "routing_strategy": str(row.get("routing_strategy") or "simple-shuffle").strip(),
                "enabled": bool(row.get("enabled", True)),
                # Only explicitly published public models are exposed by the gateway.
                "gateway_enabled": bool(row.get("gateway_enabled", row.get("expose_in_gateway", legacy_gateway_enabled))),
            }
        )
    return items


def migrate_gateway_publish_flags(model_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(bool(item.get("gateway_enabled", False)) for item in model_routes):
        return model_routes
    try:
        legacy_config = load_config()
    except Exception:
        return model_routes
    published_names = {
        str((row.get("model_info") or {}).get("public_model_name") or row.get("model_name") or "").strip()
        for row in ensure_list(legacy_config.get("model_list"))
        if isinstance(row, dict)
    }
    published_names.discard("")
    if not published_names:
        return model_routes
    for route in model_routes:
        if str(route.get("public_model_name") or "").strip() in published_names:
            route["gateway_enabled"] = True
    return model_routes


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
        "supplier_quotas": {},
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
    include_unpublished: bool = False,
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
        if not include_unpublished and not bool(route.get("gateway_enabled", False)):
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
                "gateway_enabled": bool(route.get("gateway_enabled", False)),
                "known_models": ensure_list(api_profile.get("known_models")),
                "platform_key": supplier_platform_key(api_profile.get("api_base"), supplier.get("name")),
                "cost_provider_key": supplier_provider_key(
                    supplier.get("id"),
                    api_profile.get("custom_llm_provider"),
                ),
            }
        )
    return providers


def load_management_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state, quota_state_changed = migrate_legacy_supplier_quota_state(state)
        if quota_state_changed:
            migration_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".migrating")
            migration_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(migration_path, STATE_PATH)
        if any(key in state for key in ("suppliers", "api_profiles", "model_routes", "route_bindings")):
            suppliers = normalize_suppliers([item for item in ensure_list(state.get("suppliers")) if isinstance(item, dict)])
            api_profiles = normalize_api_profiles(
                [item for item in ensure_list(state.get("api_profiles")) if isinstance(item, dict)],
                suppliers,
            )
            model_routes = normalize_model_routes([item for item in ensure_list(state.get("model_routes")) if isinstance(item, dict)])
            model_routes = migrate_gateway_publish_flags(model_routes)
            route_bindings = normalize_route_bindings(
                [item for item in ensure_list(state.get("route_bindings")) if isinstance(item, dict)],
                api_profiles,
                model_routes,
            )
            return {
                "routing_view_mode": str(state.get("routing_view_mode") or "by_model"),
                "suppliers": suppliers,
                "api_profiles": api_profiles,
                "supplier_quotas": {
                    str(key): ensure_mapping(value)
                    for key, value in ensure_mapping(state.get("supplier_quotas")).items()
                    if str(key).strip()
                },
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


def claude_gateway_model_alias(public_model_name: str) -> str:
    """Return a Claude Desktop-discoverable route name for a public model."""
    return f"claude-haiku-relaydeck-{slugify(public_model_name)}"


def build_litellm_config_from_providers(
    providers: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
    model_name_transform: Any = None,
) -> dict[str, Any]:
    discovery_settings = claude_discovery_settings(litellm_settings)
    transform_model_name = model_name_transform or (lambda model_name: model_name)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for provider in providers:
        if not bool(provider.get("gateway_enabled", True)):
            continue
        grouped.setdefault(provider["model_name"], []).append(provider)

    model_list: list[dict[str, Any]] = []
    fallbacks: list[dict[str, list[str]]] = []
    for public_model_name, rows in grouped.items():
        gateway_model_name = str(transform_model_name(public_model_name) or "").strip()
        if not gateway_model_name:
            continue
        active_rows = [row for row in rows if row.get("enabled", True)]
        active_rows.sort(key=lambda row: (int(row.get("priority", 100)), row.get("relay_label", "")))
        if not active_rows:
            continue

        backup_aliases: list[str] = []
        for idx, provider in enumerate(active_rows):
            relay_slug = slugify(provider.get("relay_label", "")) or f"relay-{idx+1}"
            alias = (
                gateway_model_name
                if idx == 0
                else f"{gateway_model_name}__{idx+1}__{relay_slug}"
            )
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

            platform_key = str(
                provider.get("platform_key")
                or supplier_platform_key(provider.get("api_base"), provider.get("supplier_name"))
            )
            relaydeck_metadata = {
                "platform_key": platform_key,
                "cost_provider_key": str(provider.get("cost_provider_key") or ""),
                "supplier_id": str(provider.get("supplier_id") or ""),
                "api_profile_id": str(provider.get("api_profile_id") or ""),
                "model_route_id": str(provider.get("model_route_id") or ""),
                "route_binding_id": str(provider.get("binding_id") or ""),
                "public_model_name": public_model_name,
                "upstream_model": str(provider.get("upstream_model") or ""),
                "relay_label": str(provider.get("relay_label") or ""),
                "api_base_hash": api_base_hash(provider.get("api_base")),
                "custom_llm_provider": str(provider.get("custom_llm_provider") or "openai"),
            }
            discovery_metadata = claude_discovery_metadata(public_model_name, discovery_settings)
            if discovery_metadata:
                relaydeck_metadata.update(discovery_metadata)
            params["metadata"] = {"relaydeck": relaydeck_metadata}

            model_info = {
                "relay_label": slugify(provider.get("relay_label", "")) or str(provider.get("relay_label", "") or ""),
                "public_model_name": public_model_name,
                "platform_key": platform_key,
                "supplier_id": str(provider.get("supplier_id") or ""),
                "api_profile_id": str(provider.get("api_profile_id") or ""),
                "model_route_id": str(provider.get("model_route_id") or ""),
                "route_binding_id": str(provider.get("binding_id") or ""),
                "relaydeck": relaydeck_metadata,
                "priority": int(provider.get("priority", 100)),
                "enabled": bool(provider.get("enabled", True)),
            }
            model_info.update(discovery_metadata)

            model_list.append(
                {
                    "model_name": alias,
                    "litellm_params": params,
                    "model_info": model_info,
                }
            )

        if backup_aliases:
            fallbacks.append({gateway_model_name: backup_aliases})

    next_router = dict(router_settings or {})
    next_router["fallbacks"] = fallbacks
    next_router.pop("model_group_alias", None)

    next_litellm_settings = dict(litellm_settings or {})
    next_litellm_settings.pop(CLAUDE_DISCOVERY_SETTINGS_KEY, None)
    callback_path = "relaydeck_callback.proxy_handler_instance"
    legacy_callback_paths = {
        "relaydeck_litellm_callback.proxy_handler_instance",
        callback_path,
    }
    for callback_key in ("success_callback", "failure_callback"):
        existing = next_litellm_settings.get(callback_key)
        if isinstance(existing, str):
            existing = [existing]
        if isinstance(existing, list):
            existing = [item for item in existing if item not in legacy_callback_paths]
            if existing:
                next_litellm_settings[callback_key] = existing
            else:
                next_litellm_settings.pop(callback_key, None)
    callbacks = next_litellm_settings.get("callbacks")
    if isinstance(callbacks, str):
        callbacks = [callbacks]
    elif not isinstance(callbacks, list):
        callbacks = []
    callbacks = [item for item in callbacks if item not in legacy_callback_paths]
    callbacks.append(callback_path)
    next_litellm_settings["callbacks"] = callbacks

    return {
        "model_list": model_list,
        "router_settings": next_router,
        "litellm_settings": next_litellm_settings,
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def build_litellm_gateway_configs_from_providers(
    providers: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build client-isolated views from the same public routing state."""
    return {
        "openai": build_litellm_config_from_providers(
            providers,
            router_settings,
            litellm_settings,
        ),
        "claude": build_litellm_config_from_providers(
            providers,
            router_settings,
            litellm_settings,
            claude_gateway_model_alias,
        ),
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
    supplier_quotas: dict[str, dict[str, Any]] | None = None,
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
    if supplier_quotas is None:
        supplier_quotas = {}
        if STATE_PATH.exists():
            try:
                supplier_quotas = ensure_mapping(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("supplier_quotas"))
            except Exception:
                supplier_quotas = {}
    normalized_supplier_quotas = {
        str(key).strip().lower(): split_quota_credentials(ensure_mapping(value))[0]
        for key, value in ensure_mapping(supplier_quotas).items()
        if str(key).strip()
    }
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
                "supplier_quotas": normalized_supplier_quotas,
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


def build_litellm_gateway_configs_from_management_state(
    suppliers: list[dict[str, Any]],
    api_profiles: list[dict[str, Any]],
    model_routes: list[dict[str, Any]],
    route_bindings: list[dict[str, Any]],
    router_settings: dict[str, Any],
    litellm_settings: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    providers = flatten_management_state_to_providers(suppliers, api_profiles, model_routes, route_bindings)
    return build_litellm_gateway_configs_from_providers(providers, router_settings, litellm_settings)


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
    run_powershell_script("stop-litellm.ps1")
    time.sleep(1)
    run_powershell_script("start-litellm.ps1")


def control_service(service_name: str, action: str) -> dict[str, Any]:
    if service_name == "litellm":
        target, command_pattern, start_script = LITELLM_EXE, "litellm", "start-litellm.ps1"
    elif service_name == "open-webui":
        target, command_pattern, start_script = OPEN_WEBUI_EXE, "open-webui", "start-open-webui.ps1"
    else:
        raise ValueError(f"不支持控制服务：{service_name}")

    if action == "start":
        return run_powershell_script(start_script)
    if action == "stop":
        if service_name == "litellm":
            return run_powershell_script("stop-litellm.ps1")
        stop_processes_by_target(target=target, command_pattern=command_pattern)
        return {"stdout": f"已停止 {service_name}", "stderr": ""}
    if action == "restart":
        if service_name == "litellm":
            restart_litellm()
            return {"stdout": f"宸查噸啟 {service_name}", "stderr": ""}
        stop_processes_by_target(target=target, command_pattern=command_pattern)
        time.sleep(1)
        return run_powershell_script(start_script)
    raise ValueError(f"不支持服务操作：{action}")


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
    gateway_enabled: bool = False


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
    supplier_quotas: dict[str, dict[str, Any]] = Field(default_factory=dict)
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
    supplier_quotas: dict[str, dict[str, Any]] = Field(default_factory=dict)
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
    billing_type: str = "topup"
    service_start: str = ""
    service_end: str = ""
    allocation_mode: str = ""
    note: str | None = None


class SupplierOrderSyncPayload(BaseModel):
    supplier_id: str
    limit: int = 20


class PortalLoginRepairPayload(BaseModel):
    api_profile: APIProfileCheckRow
    totp_code: str = ""


class SupplierCredentialPayload(BaseModel):
    credentials: dict[str, str] = Field(default_factory=dict)
    remove_fields: list[str] = Field(default_factory=list)


class SupplierLoginRepairPayload(BaseModel):
    totp_code: str = ""


class SupplierPricingConfigPayload(BaseModel):
    automation_mode: str = "detect-confirm"


class PricingVersionPayload(BaseModel):
    api_profile_id: str
    upstream_model: str = "*"
    group_name: str = ""
    base_input_per_1m: float = 0
    base_output_per_1m: float = 0
    multiplier: float = 1
    currency: str = "USD"
    effective_from: str = ""
    note: str = ""


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
    SupplierCredentialPayload,
    SupplierLoginRepairPayload,
):
    model_cls.model_rebuild()


app = FastAPI(title="RelayDeck Local Admin")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_balance_refresh_lock = threading.Lock()
_balance_refresh_stop_event = threading.Event()
_balance_refresh_thread: threading.Thread | None = None


def store_supplier_sso_credentials(
    supplier_key: str,
    credentials: dict[str, Any],
    *,
    vault: CredentialVault | Any | None = None,
) -> dict[str, Any]:
    allowed_fields = {"auth_token", "refresh_token", "session_cookie"}
    updates = {
        str(key): str(value)
        for key, value in ensure_mapping(credentials).items()
        if str(key) in allowed_fields and value not in (None, "")
    }
    if not updates:
        raise ValueError("Browser SSO did not return supplier credentials")
    return (vault or get_credential_vault()).merge(str(supplier_key or "").strip().lower(), updates)


_supplier_sso_manager = SupplierSsoManager(
    ROOT / "data" / "browser-sessions",
    worker=run_supplier_browser_sso,
    credential_sink=store_supplier_sso_credentials,
)


def refresh_balances_in_background() -> None:
    with _balance_refresh_lock:
        refresh_balances_v2(BalanceRefreshPayload(force=False))


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
    _supplier_sso_manager.mark_active_tasks_interrupted()
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


@app.post("/api/clients/claude-code/configure")
def api_configure_claude_code() -> dict[str, Any]:
    env_map = parse_env_file()
    gateway_key = str(env_map.get("LITELLM_MASTER_KEY") or "").strip()
    if not gateway_key:
        raise HTTPException(status_code=400, detail="Missing LITELLM_MASTER_KEY")
    claude_port = int(env_map.get("CLAUDE_LITELLM_PORT", "4101"))
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        result = configure_claude_code_settings(
            settings_path,
            f"http://127.0.0.1:{claude_port}",
            gateway_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result, "restart_required": True}


@app.get("/api/quota-adapters")
def api_quota_adapters() -> dict[str, Any]:
    return {
        "ok": True,
        "directory": str(ensure_quota_adapters_dir()),
        "items": list_quota_adapter_scripts(),
    }


def supplier_quota_public_state(management_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    suppliers_by_id = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    configs = {
        str(key).strip().lower(): split_quota_credentials(ensure_mapping(value))[0]
        for key, value in ensure_mapping(management_state.get("supplier_quotas")).items()
        if str(key).strip()
    }
    for profile in management_state.get("api_profiles", []):
        if isinstance(profile, dict):
            configs.setdefault(supplier_platform_key_for_profile(profile, suppliers_by_id), {})
    vault = get_credential_vault()
    pricing_store = get_usage_quota_store()
    runtime_status = browser_runtime_status()
    public_state: dict[str, dict[str, Any]] = {}
    for key, config in configs.items():
        resolved_config = resolved_supplier_quota_config(key, config)
        try:
            credential_status = vault.status(key)
        except Exception as exc:
            credential_status = {
                "supplier_key": key,
                "configured": False,
                "configured_fields": [],
                "updated_at": "",
                "storage": "windows-dpapi-current-user",
                "error": str(exc),
            }
        public_state[key] = {
            **resolved_config,
            "browser_sso": browser_sso_adapter_for_target({"platform_key": key, "quota": resolved_config}) or {},
            "credential_status": credential_status,
            "sso_status": _supplier_sso_manager.status(key),
            "browser_runtime": runtime_status,
            "pricing_history": {
                "versions": pricing_store.list_pricing_versions(platform_key=key, limit=200),
            },
        }
    return public_state


def known_supplier_platform_keys(management_state: dict[str, Any]) -> set[str]:
    suppliers_by_id = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    keys = {
        str(key).strip().lower()
        for key in ensure_mapping(management_state.get("supplier_quotas"))
        if str(key).strip()
    }
    for profile in management_state.get("api_profiles", []):
        if isinstance(profile, dict):
            keys.add(supplier_platform_key_for_profile(profile, suppliers_by_id))
    return keys


def validate_supplier_platform_key(raw_key: str) -> str:
    key = str(raw_key or "").strip().lower()
    if not key or len(key) > 200:
        raise HTTPException(status_code=400, detail="Invalid supplier platform key")
    management_state = load_management_state()
    if key not in known_supplier_platform_keys(management_state):
        raise HTTPException(status_code=404, detail="Supplier platform not found")
    return key


def profile_for_supplier_platform(
    management_state: dict[str, Any],
    platform_key: str,
    api_profile_id: str,
) -> dict[str, Any]:
    suppliers_by_id = {
        str(item.get("id") or ""): item
        for item in management_state.get("suppliers", [])
        if isinstance(item, dict) and item.get("id")
    }
    profile = next(
        (
            item
            for item in management_state.get("api_profiles", [])
            if isinstance(item, dict) and str(item.get("id") or "") == str(api_profile_id or "")
        ),
        None,
    )
    if not profile or supplier_platform_key_for_profile(profile, suppliers_by_id) != platform_key:
        raise HTTPException(status_code=400, detail="API profile does not belong to this supplier platform")
    return profile


def save_supplier_pricing_automation(management_state: dict[str, Any], platform_key: str, mode: str) -> None:
    supplier_quotas = {
        str(key).strip().lower(): persisted_supplier_quota_config(ensure_mapping(value))
        for key, value in ensure_mapping(management_state.get("supplier_quotas")).items()
        if str(key).strip()
    }
    supplier_quotas.setdefault(platform_key, {})["pricing_automation"] = normalize_pricing_automation(mode)
    save_management_state(
        management_state.get("suppliers", []),
        management_state.get("api_profiles", []),
        management_state.get("model_routes", []),
        management_state.get("route_bindings", []),
        management_state.get("model_families", []),
        management_state.get("router_settings", {}),
        management_state.get("litellm_settings", {}),
        management_state.get("routing_view_mode", "by_model"),
        supplier_quotas,
    )


@app.get("/api/supplier-pricing/{supplier_key}")
def api_supplier_pricing(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    state = load_management_state()
    quota = resolved_supplier_quota_config(key, ensure_mapping(state.get("supplier_quotas")).get(key))
    versions = get_usage_quota_store().list_pricing_versions(platform_key=key, limit=500)
    return {
        "ok": True,
        "supplier_key": key,
        "automation_mode": normalize_pricing_automation(quota.get("pricing_automation")),
        "versions": versions,
        "active": [item for item in versions if item.get("status") == "active"],
        "candidates": [item for item in versions if item.get("status") == "candidate"],
    }


@app.post("/api/supplier-pricing/{supplier_key}/config")
def api_update_supplier_pricing_config(
    supplier_key: str,
    payload: SupplierPricingConfigPayload,
) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    mode = normalize_pricing_automation(payload.automation_mode)
    state = load_management_state()
    save_supplier_pricing_automation(state, key, mode)
    return {"ok": True, "supplier_key": key, "automation_mode": mode}


@app.post("/api/supplier-pricing/{supplier_key}/versions")
def api_create_supplier_pricing_version(
    supplier_key: str,
    payload: PricingVersionPayload,
) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    state = load_management_state()
    profile = profile_for_supplier_platform(state, key, payload.api_profile_id)
    normalized = normalize_pricing_observation(
        key,
        profile,
        {
            **payload.model_dump(),
            "effective_from": payload.effective_from or now_iso_local(),
            "confidence": "exact",
        },
        source="manual",
    )
    store = get_usage_quota_store()
    version = store.insert_pricing_version(normalized)
    active = store.activate_pricing_version(version["id"])
    return {"ok": True, "item": active}


def validate_pricing_version_supplier(supplier_key: str, version_id: str) -> tuple[Any, dict[str, Any]]:
    key = validate_supplier_platform_key(supplier_key)
    store = get_usage_quota_store()
    item = store.get_pricing_version(version_id)
    if not item or str(item.get("platform_key") or "") != key:
        raise HTTPException(status_code=404, detail="Pricing version not found")
    return store, item


@app.post("/api/supplier-pricing/{supplier_key}/versions/{version_id}/activate")
def api_activate_supplier_pricing_version(supplier_key: str, version_id: str) -> dict[str, Any]:
    store, _ = validate_pricing_version_supplier(supplier_key, version_id)
    return {"ok": True, "item": store.activate_pricing_version(version_id)}


@app.post("/api/supplier-pricing/{supplier_key}/versions/{version_id}/reject")
def api_reject_supplier_pricing_version(supplier_key: str, version_id: str) -> dict[str, Any]:
    store, _ = validate_pricing_version_supplier(supplier_key, version_id)
    try:
        item = store.reject_pricing_version(version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@app.post("/api/supplier-credentials/{supplier_key}")
def api_update_supplier_credentials(supplier_key: str, payload: SupplierCredentialPayload) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    unknown_fields = (set(payload.credentials) | set(payload.remove_fields)) - SENSITIVE_QUOTA_CREDENTIAL_FIELDS
    if unknown_fields:
        raise HTTPException(status_code=400, detail=f"Unsupported credential fields: {', '.join(sorted(unknown_fields))}")
    vault = get_credential_vault()
    try:
        if payload.remove_fields:
            vault.remove_fields(key, payload.remove_fields)
        status = vault.merge(key, payload.credentials)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Credential vault update failed: {exc}") from exc
    return {"ok": True, "credential_status": status}


@app.delete("/api/supplier-credentials/{supplier_key}")
def api_delete_supplier_credentials(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    vault = get_credential_vault()
    try:
        deleted = vault.delete(key)
        status = vault.status(key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Credential vault delete failed: {exc}") from exc
    return {"ok": True, "deleted": deleted, "credential_status": status}


@app.post("/api/supplier-credentials/{supplier_key}/repair-login")
def api_repair_supplier_login(supplier_key: str, payload: SupplierLoginRepairPayload) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    management_state = load_management_state()
    target = next((item for item in supplier_quota_targets(management_state) if item.get("platform_key") == key), None)
    if not target:
        raise HTTPException(status_code=404, detail="Supplier platform not found")
    vault = get_credential_vault()
    credentials = vault.get(key)
    profile = copy.deepcopy(target.get("representative_profile") or {})
    profile["quota"] = {**ensure_mapping(target.get("quota")), **credentials}
    result = repair_portal_login_state(
        profile,
        totp_code=str(payload.totp_code or "").strip(),
        supplier_key=key,
    )
    return redact_sensitive_data(result, list(credentials.values()))


def resolve_supplier_sso_target(management_state: dict[str, Any], supplier_key: str) -> dict[str, Any]:
    key = str(supplier_key or "").strip().lower()
    target = next((item for item in supplier_quota_targets(management_state) if item.get("platform_key") == key), None)
    if not target:
        raise HTTPException(status_code=404, detail="Supplier platform not found")
    quota = ensure_mapping(target.get("quota"))
    adapter = browser_sso_adapter_for_target(target)
    if not adapter:
        raise HTTPException(status_code=400, detail="Browser SSO is not configured for this supplier adapter")
    portal_base = str(
        quota.get("portal_base")
        or adapter.get("portal_base")
        or f"https://{key}"
    ).strip().rstrip("/")
    parsed = urlparse(portal_base)
    allowed_hosts = {str(host).strip().lower().lstrip(".") for host in adapter.get("portal_hosts", [])}
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower().lstrip(".") not in allowed_hosts:
        raise HTTPException(status_code=400, detail="Supplier portal base must use an HTTPS adapter-approved host")
    return {**target, "portal_base": portal_base, "browser_sso": adapter}


@app.post("/api/supplier-sso/{supplier_key}/start")
def api_start_supplier_sso(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    target = resolve_supplier_sso_target(load_management_state(), key)
    try:
        status = _supplier_sso_manager.start(
            key,
            str(target.get("portal_base") or f"https://{key}"),
            interactive=True,
            timeout_seconds=600,
            adapter=ensure_mapping(target.get("browser_sso")),
        )
    except SsoTaskActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "sso_status": status}


@app.get("/api/supplier-sso/{supplier_key}/status")
def api_supplier_sso_status(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    return {
        "ok": True,
        "sso_status": _supplier_sso_manager.status(key),
        "browser_runtime": browser_runtime_status(),
    }


@app.post("/api/supplier-sso/{supplier_key}/complete")
def api_complete_supplier_sso(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    completed = _supplier_sso_manager.complete_interactive(key)
    return {
        "ok": True,
        "completed": completed,
        "sso_status": _supplier_sso_manager.status(key),
    }


@app.post("/api/supplier-sso/{supplier_key}/cancel")
def api_cancel_supplier_sso(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    cancelled = _supplier_sso_manager.cancel(key)
    return {"ok": True, "cancelled": cancelled, "sso_status": _supplier_sso_manager.status(key)}


@app.delete("/api/supplier-sso/{supplier_key}/session")
def api_clear_supplier_sso_session(supplier_key: str) -> dict[str, Any]:
    key = validate_supplier_platform_key(supplier_key)
    try:
        cleared = _supplier_sso_manager.clear_session(key)
    except SsoTaskActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "cleared": cleared, "sso_status": _supplier_sso_manager.status(key)}


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
        "supplier_quotas": supplier_quota_public_state(management_state),
        "model_routes": management_state.get("model_routes", []),
        "route_bindings": management_state.get("route_bindings", []),
        "model_families": management_state.get("model_families", []),
        "env": env_map,
        "router_settings": router_settings,
        "litellm_settings": litellm_settings,
        "usage_dashboard": usage_dashboard_snapshot(management_state),
        "ports": {
            "litellm": litellm_port,
            "claude_litellm": int(env_map.get("CLAUDE_LITELLM_PORT", "4101")),
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
        configs = build_litellm_gateway_configs_from_management_state(
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
        configs = build_litellm_gateway_configs_from_management_state(
            suppliers,
            api_profiles,
            model_routes,
            route_bindings,
            payload.router_settings,
            payload.litellm_settings,
        )

    next_env = merge_api_profile_secrets_into_env(payload.env, api_profiles)
    backup_file(ENV_PATH)
    save_gateway_configs(configs)
    save_management_state(
        suppliers,
        api_profiles,
        model_routes,
        route_bindings,
        payload.model_families,
        payload.router_settings,
        payload.litellm_settings,
        payload.routing_view_mode,
        payload.supplier_quotas,
    )
    write_env_file(next_env)

    if payload.restart_litellm:
        restart_litellm()

    return {"ok": True}


@app.post("/api/save-routing-draft")
def api_save_routing_draft(payload: RoutingDraftPayload) -> dict[str, Any]:
    previous_state = load_management_state()
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
        payload.supplier_quotas,
    )
    previous_routes = {str(item.get("id")): bool(item.get("gateway_enabled", False)) for item in previous_state.get("model_routes", [])}
    next_routes = {str(item.get("id")): bool(item.get("gateway_enabled", False)) for item in model_routes}
    previous_discovery = claude_discovery_settings(previous_state.get("litellm_settings", {}))
    next_discovery = claude_discovery_settings(payload.litellm_settings)
    gateway_publish_changed = previous_routes != next_routes or previous_discovery != next_discovery
    gateway_reload_error = ""
    if gateway_publish_changed:
        try:
            configs = build_litellm_gateway_configs_from_management_state(
                suppliers,
                api_profiles,
                model_routes,
                route_bindings,
                payload.router_settings,
                payload.litellm_settings,
            )
            save_gateway_configs(configs)
            restart_litellm()
        except Exception as error:
            gateway_reload_error = str(error)
    return {
        "ok": True,
        "saved_to": str(STATE_PATH),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gateway_reloaded": gateway_publish_changed and not gateway_reload_error,
        "gateway_reload_error": gateway_reload_error,
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


@app.get("/api/cost-attribution/{platform_key}")
def api_cost_attribution(platform_key: str) -> dict[str, Any]:
    normalized_key = str(platform_key or "").strip().lower()
    management_state = load_management_state()
    start_at = month_start_iso_local()
    end_at = now_iso_local()
    snapshot = supplier_cost_attribution_snapshot(
        management_state,
        get_usage_quota_store(),
        start_at,
        end_at,
    )
    if normalized_key not in snapshot:
        raise HTTPException(status_code=404, detail="supplier platform not found")
    return {
        "ok": True,
        "window": {"start_at": start_at, "end_at": end_at},
        "item": snapshot[normalized_key],
    }


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
    supplier_platform_lookup = {
        supplier_platform_provider_key(supplier_platform_key(profile.get("api_base"), suppliers.get(str(profile.get("supplier_id") or ""), {}).get("name"))): suppliers.get(str(profile.get("supplier_id") or ""), {})
        for profile in api_profiles.values()
        if str(profile.get("supplier_id") or "").strip()
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
        platform_keys = {
            supplier_platform_key(
                profile.get("api_base"),
                supplier.get("name") or supplier.get("label"),
            )
            for profile in api_profiles.values()
            if str(profile.get("supplier_id") or "") == str(supplier_id)
        }
        items = []
        for platform_key in platform_keys or {None}:
            items.extend(
                recharge_records_for_supplier(
                    store,
                    supplier_id,
                    provider_name=normalize_provider_name(supplier.get("custom_llm_provider")),
                    platform_key=platform_key,
                )
            )
        items = list({str(item.get("id")): item for item in items}.values())
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
        matched_supplier = supplier_lookup.get(str(item.get("provider_key") or "")) or supplier_platform_lookup.get(str(item.get("provider_key") or ""))
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
    billing_type = str(payload.billing_type or "topup").strip().lower()
    if billing_type not in {"topup", "subscription"}:
        raise HTTPException(status_code=400, detail="billing_type must be topup or subscription")
    if billing_type == "subscription":
        service_start = iso_to_dt(payload.service_start)
        service_end = iso_to_dt(payload.service_end)
        if normalized_paid_amount < 0:
            raise HTTPException(status_code=400, detail="subscription paid_amount_cny cannot be negative")
        if service_start is None or service_end is None or service_end < service_start:
            raise HTTPException(status_code=400, detail="subscription service period is invalid")
    allocation_mode = str(payload.allocation_mode or "").strip().lower()
    if not allocation_mode:
        allocation_mode = "daily-amortized" if billing_type == "subscription" else "weighted-credit"
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
            "billing_type": billing_type,
            "service_start": payload.service_start or None,
            "service_end": payload.service_end or None,
            "allocation_mode": allocation_mode,
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
            "billing_type": billing_type,
            "service_start": payload.service_start or None,
            "service_end": payload.service_end or None,
            "allocation_mode": allocation_mode,
            "note": payload.note,
        }
    record = store.insert_recharge_record(
        record_payload
    )
    return {"ok": True, "item": record}


@app.patch("/api/recharge-records/{recharge_id}")
def api_update_recharge_record(recharge_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "paid_amount_cny" in payload:
        try:
            updates["paid_amount_cny"] = float(payload.get("paid_amount_cny") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="paid_amount_cny must be a number") from exc
    for key in (
        "paid_at",
        "credited_amount",
        "credited_currency",
        "bonus_amount",
        "bonus_currency",
        "balance_after_recharge",
        "service_start",
        "service_end",
        "allocation_mode",
        "note",
    ):
        if key in payload:
            updates[key] = payload.get(key)
    try:
        record = get_usage_quota_store().update_recharge_record(recharge_id, updates)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "not found" in detail else 400, detail=detail) from exc
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


@app.post("/api/service/{service_name}/{action}")
def api_control_service(service_name: str, action: str) -> dict[str, Any]:
    result = control_service(service_name, action)
    return {"ok": True, "service": service_name, "action": action, **result}


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
    current_state = load_management_state()
    router_settings = current_state.get("router_settings", {}) or {}
    litellm_settings = current_state.get("litellm_settings", {}) or {}
    providers = flatten_management_state_to_providers(
        current_state.get("suppliers", []),
        current_state.get("api_profiles", []),
        current_state.get("model_routes", []),
        current_state.get("route_bindings", []),
        include_unpublished=True,
    )
    existing_routes = {
        str(item.get("public_model_name") or ""): item
        for item in current_state.get("model_routes", [])
        if isinstance(item, dict)
    }
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

    next_state = management_state_from_providers(
        providers,
        router_settings,
        litellm_settings,
        current_state.get("routing_view_mode", "by_model"),
    )
    next_routes = next_state.get("model_routes", [])
    next_route_names = {str(item.get("public_model_name") or "") for item in next_routes}
    for route in current_state.get("model_routes", []):
        route_name = str(route.get("public_model_name") or "")
        if route_name and route_name not in next_route_names:
            next_routes.append(route)
    for route in next_routes:
        previous = existing_routes.get(str(route.get("public_model_name") or ""))
        if previous:
            route["gateway_enabled"] = bool(previous.get("gateway_enabled", False))

    suppliers = normalize_suppliers(next_state.get("suppliers", []))
    api_profiles = normalize_api_profiles(next_state.get("api_profiles", []), suppliers)
    model_routes = normalize_model_routes(next_routes)
    route_bindings = normalize_route_bindings(next_state.get("route_bindings", []), api_profiles, model_routes)
    save_management_state(
        suppliers,
        api_profiles,
        model_routes,
        route_bindings,
        current_state.get("model_families", []),
        router_settings,
        litellm_settings,
        current_state.get("routing_view_mode", "by_model"),
    )
    configs = build_litellm_gateway_configs_from_management_state(
        suppliers,
        api_profiles,
        model_routes,
        route_bindings,
        router_settings,
        litellm_settings,
    )
    save_gateway_configs(configs)
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
