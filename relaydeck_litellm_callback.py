from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from litellm.integrations.custom_logger import CustomLogger


ROOT = Path(__file__).resolve().parent
ADMIN_PANEL_DIR = ROOT / "admin-panel"
if str(ADMIN_PANEL_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_PANEL_DIR))

from cost_attribution import subscription_amortization, weighted_topup_basis  # noqa: E402
from usage_quota_store import UsageQuotaStore, get_usage_quota_store, utc_now_iso  # noqa: E402


LOGGER = logging.getLogger("relaydeck.usage_callback")


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    return str(value or "").strip() or utc_now_iso()


def _usage(response: Mapping[str, Any]) -> dict[str, int]:
    usage = _mapping(response.get("usage"))
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
    }


def _relaydeck_metadata(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    litellm_params = _mapping(kwargs.get("litellm_params"))
    candidates = [
        _mapping(litellm_params.get("metadata")),
        _mapping(litellm_params.get("model_info")),
        _mapping(kwargs.get("metadata")),
        _mapping(kwargs.get("model_info")),
        _mapping(kwargs.get("standard_logging_object")),
        _mapping(kwargs.get("standard_logging_payload")),
    ]
    for candidate in candidates:
        relaydeck = _mapping(candidate.get("relaydeck"))
        if relaydeck:
            return relaydeck
        model_info = _mapping(candidate.get("model_info"))
        relaydeck = _mapping(model_info.get("relaydeck"))
        if relaydeck:
            return relaydeck
        metadata = _mapping(candidate.get("metadata"))
        relaydeck = _mapping(metadata.get("relaydeck"))
        if relaydeck:
            return relaydeck
        model_info = _mapping(metadata.get("model_info"))
        relaydeck = _mapping(model_info.get("relaydeck"))
        if relaydeck:
            return relaydeck
        if candidate.get("api_profile_id") and candidate.get("route_binding_id"):
            return candidate
    return {}


def normalize_litellm_usage_event(
    kwargs: Mapping[str, Any],
    response_obj: Any,
    start_time: Any,
    end_time: Any,
    *,
    status: str,
) -> dict[str, Any]:
    response = _mapping(response_obj)
    metadata = _relaydeck_metadata(kwargs)
    usage = _usage(response)
    start = start_time if isinstance(start_time, datetime) else None
    end = end_time if isinstance(end_time, datetime) else None
    latency_ms = round((end - start).total_seconds() * 1000) if start and end else None
    exception = kwargs.get("exception")
    if status == "success":
        error_code = None
    elif exception is not None:
        error_code = str(getattr(exception, "status_code", "") or type(exception).__name__)
    else:
        error_code = "gateway-error"
    return {
        "source": "litellm_gateway",
        "request_id": str(
            response.get("id")
            or kwargs.get("litellm_call_id")
            or kwargs.get("request_id")
            or ""
        ).strip()
        or None,
        "created_at": _iso(end_time),
        "public_model_name": str(
            metadata.get("public_model_name")
            or _mapping(kwargs.get("litellm_params")).get("model_group")
            or kwargs.get("model")
            or ""
        ),
        "litellm_model_name": str(kwargs.get("model") or ""),
        "upstream_model": str(metadata.get("upstream_model") or response.get("model") or kwargs.get("model") or ""),
        "relay_label": str(metadata.get("relay_label") or ""),
        "api_base_hash": str(metadata.get("api_base_hash") or ""),
        "custom_llm_provider": str(metadata.get("custom_llm_provider") or ""),
        "platform_key": str(metadata.get("platform_key") or ""),
        "provider_key": str(metadata.get("cost_provider_key") or ""),
        "api_profile_id": str(metadata.get("api_profile_id") or ""),
        "model_route_id": str(metadata.get("model_route_id") or ""),
        "route_binding_id": str(metadata.get("route_binding_id") or metadata.get("binding_id") or ""),
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "estimated_cost": None,
        "actual_cost": _number(kwargs.get("response_cost")),
        "cash_cost_cny": None,
        "cost_attribution_status": "pending",
        "currency": "USD",
        "latency_ms": latency_ms,
        "status": status,
        "error_code": error_code,
    }


def enrich_usage_event(event: Mapping[str, Any], store: UsageQuotaStore) -> dict[str, Any]:
    item = dict(event)
    if str(item.get("status") or "") != "success":
        item["cost_attribution_status"] = "request-failed"
        return item
    api_profile_id = str(item.get("api_profile_id") or "")
    upstream_model = str(item.get("upstream_model") or "")
    price = store.get_effective_pricing_version(
        api_profile_id,
        upstream_model,
        str(item.get("created_at") or ""),
    )
    if not price:
        item["cost_attribution_status"] = "missing-price"
        return item

    item["pricing_version_id"] = price["id"]
    item["currency"] = str(price.get("currency") or "USD")
    prompt_tokens = int(item.get("prompt_tokens") or 0)
    completion_tokens = int(item.get("completion_tokens") or 0)
    item["estimated_cost"] = (
        prompt_tokens / 1_000_000 * float(price.get("input_per_1m") or 0)
        + completion_tokens / 1_000_000 * float(price.get("output_per_1m") or 0)
    )

    provider_key = str(item.get("provider_key") or "")
    records = store.list_recharge_records(limit=1000, provider_key=provider_key) if provider_key else []
    event_time = str(item.get("created_at") or "")
    if subscription_amortization(records, event_time, event_time)["amortized_cny"] > 0:
        item["cost_attribution_status"] = "pending-subscription-allocation"
        return item
    basis = weighted_topup_basis(records, item["currency"])
    rate = basis.get("cash_per_credit_cny")
    if rate is None:
        item["cost_attribution_status"] = str(basis.get("status") or "missing-cost-basis")
        return item
    item["cash_cost_cny"] = float(item["estimated_cost"] or 0) * float(rate)
    item["cost_attribution_status"] = "attributed-topup"
    return item


class RelayDeckUsageLogger(CustomLogger):
    def __init__(self, store_factory: Callable[[], UsageQuotaStore] = get_usage_quota_store) -> None:
        super().__init__()
        self.store_factory = store_factory

    def _persist(
        self,
        kwargs: Mapping[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        status: str,
    ) -> None:
        try:
            store = self.store_factory()
            event = normalize_litellm_usage_event(
                kwargs,
                response_obj,
                start_time,
                end_time,
                status=status,
            )
            store.insert_usage_event_once(enrich_usage_event(event, store))
        except Exception as error:
            LOGGER.warning("RelayDeck usage callback could not persist event: %s", error)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._persist(kwargs, response_obj, start_time, end_time, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._persist(kwargs, response_obj, start_time, end_time, "error")


proxy_handler_instance = RelayDeckUsageLogger()


__all__ = [
    "RelayDeckUsageLogger",
    "enrich_usage_event",
    "normalize_litellm_usage_event",
    "proxy_handler_instance",
]
