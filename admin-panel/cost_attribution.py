from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping


GENERIC_CREDIT_CURRENCIES = {"CREDIT", "CREDITS", "QUOTA"}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _currency(value: Any) -> str:
    return str(value or "").strip().upper()


def _datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date(value: Any) -> date | None:
    parsed = _datetime(value)
    if parsed is not None:
        return parsed.date()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _currency_matches(record_currency: Any, requested_currency: Any) -> bool:
    record_value = _currency(record_currency)
    requested_value = _currency(requested_currency)
    return bool(record_value and (record_value == requested_value or record_value in GENERIC_CREDIT_CURRENCIES))


def _topup_credit_amount(record: Mapping[str, Any], currency: str) -> float:
    """Use an observed balance delta before legacy/manual credit fields."""
    delta = _number(record.get("balance_delta"))
    record_currency = record.get("credited_currency") or currency
    if delta is not None and delta > 0 and _currency_matches(record_currency, currency):
        return delta
    credited = _number(record.get("credited_amount"))
    if credited is None or credited <= 0 or not _currency_matches(record_currency, currency):
        return 0.0
    bonus = _number(record.get("bonus_amount")) or 0.0
    if bonus > 0 and not _currency_matches(record.get("bonus_currency") or record_currency, currency):
        bonus = 0.0
    return credited + max(0.0, bonus)


def weighted_topup_basis(records: Iterable[Mapping[str, Any]], currency: str) -> dict[str, Any]:
    paid_cny = 0.0
    credits = 0.0
    saw_topup = False
    saw_currency_mismatch = False
    matched_records = 0
    for record in records:
        if str(record.get("billing_type") or "topup").strip().lower() != "topup":
            continue
        paid = _number(record.get("paid_amount_cny"))
        raw_credited = _number(record.get("balance_delta")) or _number(record.get("credited_amount"))
        credited = _topup_credit_amount(record, currency)
        if paid is None or paid <= 0 or raw_credited is None or raw_credited <= 0:
            continue
        saw_topup = True
        if credited <= 0:
            saw_currency_mismatch = True
            continue
        if not _currency_matches(record.get("credited_currency"), currency):
            saw_currency_mismatch = True
            continue
        paid_cny += paid
        credits += credited
        matched_records += 1

    if paid_cny > 0 and credits > 0:
        return {
            "status": "available",
            "currency": _currency(currency),
            "paid_cny": paid_cny,
            "credits": credits,
            "cash_per_credit_cny": paid_cny / credits,
            "matched_records": matched_records,
        }
    return {
        "status": "currency-mismatch" if saw_topup and saw_currency_mismatch else "missing-cost-basis",
        "currency": _currency(currency),
        "paid_cny": 0.0,
        "credits": 0.0,
        "cash_per_credit_cny": None,
        "matched_records": 0,
    }


def _allocate_topup_credit_batches(
    events: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Allocate provider-credit usage against recharge batches in FIFO order.

    The balance delta identifies the credit lot. Request cost consumes that lot;
    the calendar is used only to preserve request order, never to average costs.
    """
    lots_by_currency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in records:
        record = dict(raw)
        if str(record.get("billing_type") or "topup").strip().lower() != "topup":
            continue
        currency = _currency(record.get("credited_currency"))
        if not currency:
            continue
        credits = _topup_credit_amount(record, currency)
        if credits <= 0:
            continue
        paid = _number(record.get("paid_amount_cny"))
        lots_by_currency[currency].append(
            {
                "remaining": credits,
                "rate": paid / credits if paid is not None and paid > 0 else None,
            }
        )

    for lots in lots_by_currency.values():
        lots.sort(key=lambda item: str(item.get("paid_at") or item.get("created_at") or ""))

    cursors: dict[str, int] = defaultdict(int)
    allocations: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(
        (dict(item) for item in events),
        key=lambda item: str(item.get("created_at") or ""),
    )
    for event in ordered_events:
        event_id = str(event.get("id") or event.get("request_id") or "")
        if not event_id:
            continue
        estimated = _number(event.get("estimated_cost"))
        if estimated is None or estimated <= 0:
            allocations[event_id] = {
                "source": "none",
                "topup_cny": 0.0,
                "subscription_cny": 0.0,
                "total_cny": 0.0,
                "status": "missing-price",
            }
            continue

        currency = _currency(event.get("currency"))
        lots = lots_by_currency.get(currency, [])
        cursor = cursors[currency]
        remaining_usage = estimated
        cash_cost = 0.0
        consumed = 0.0
        saw_unpaid = False
        while remaining_usage > 1e-12 and cursor < len(lots):
            lot = lots[cursor]
            take = min(remaining_usage, max(0.0, float(lot["remaining"])))
            if take <= 0:
                cursor += 1
                continue
            consumed += take
            remaining_usage -= take
            lot["remaining"] -= take
            if lot["rate"] is None:
                saw_unpaid = True
            else:
                cash_cost += take * float(lot["rate"])
            if lot["remaining"] <= 1e-12:
                cursor += 1
        cursors[currency] = cursor

        if consumed <= 0:
            status = "missing-cost-basis"
        elif saw_unpaid or remaining_usage > 1e-12:
            status = "partial-missing-paid" if cash_cost > 0 else "missing-paid"
        else:
            status = "attributed-topup"
        allocations[event_id] = {
            "source": "topup" if cash_cost > 0 else "none",
            "topup_cny": cash_cost,
            "subscription_cny": 0.0,
            "total_cny": cash_cost,
            "credit_consumed": consumed,
            "status": status,
        }
    return allocations


def _subscription_daily_cost(record: Mapping[str, Any], day: date) -> float:
    if str(record.get("billing_type") or "topup").strip().lower() != "subscription":
        return 0.0
    starts_on = _date(record.get("service_start") or record.get("paid_at"))
    ends_on = _date(record.get("service_end"))
    paid = _number(record.get("paid_amount_cny"))
    if starts_on is None or ends_on is None or paid is None or paid <= 0 or ends_on < starts_on:
        return 0.0
    if day < starts_on or day > ends_on:
        return 0.0
    service_days = (ends_on - starts_on).days + 1
    return paid / service_days


def subscription_amortization(
    records: Iterable[Mapping[str, Any]],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    starts_on = _date(period_start)
    ends_on = _date(period_end)
    if starts_on is None or ends_on is None or ends_on < starts_on:
        return {"daily_cny": 0.0, "amortized_cny": 0.0, "covered_days": 0, "daily_costs": {}}
    record_list = list(records)
    daily_costs: dict[str, float] = {}
    for day in _date_range(starts_on, ends_on):
        amount = sum(_subscription_daily_cost(record, day) for record in record_list)
        if amount > 0:
            daily_costs[day.isoformat()] = amount
    values = list(daily_costs.values())
    return {
        "daily_cny": values[0] if values and all(abs(item - values[0]) < 1e-12 for item in values) else None,
        "amortized_cny": sum(values),
        "covered_days": len(values),
        "daily_costs": daily_costs,
    }


def subscription_usage_metrics(
    record: Mapping[str, Any],
    quota_item: Mapping[str, Any] | None = None,
    *,
    as_of: Any = None,
) -> dict[str, Any]:
    """Calculate realized, full-use, and expiry-loss metrics for one package."""
    source = dict(quota_item or {})
    paid = _number(record.get("paid_amount_cny"))
    total = _number(
        source.get("balance_total", source.get("total"))
        or record.get("quota_total")
        or record.get("credited_amount")
    )
    if total is not None and record.get("credited_amount") not in (None, "") and source.get("balance_total") is None:
        total += max(0.0, _number(record.get("bonus_amount")) or 0.0)
    used = _number(source.get("balance_used", source.get("used")) or record.get("quota_used"))
    remaining = _number(source.get("balance_remaining", source.get("remaining")) or record.get("quota_remaining"))
    if used is None and total is not None and remaining is not None:
        used = max(0.0, total - remaining)
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)

    starts_on = _date(record.get("service_start") or source.get("period_start"))
    ends_on = _date(record.get("service_end") or source.get("period_end"))
    current_day = _date(as_of) or date.today()
    if starts_on is None or ends_on is None or ends_on < starts_on:
        status = "missing-period"
    elif current_day < starts_on:
        status = "not-started"
    elif current_day > ends_on:
        status = "expired"
    else:
        status = "active"

    utilization = None
    consumed_cost = None
    unit_cost = None
    if total is not None and total > 0 and used is not None:
        utilization = max(0.0, min(1.0, used / total))
    if paid is not None and paid > 0 and utilization is not None:
        consumed_cost = paid * utilization
        unit_cost = paid / total

    projected_used = used
    elapsed_days = 0
    remaining_days = 0
    period_days = 0
    if starts_on is not None and ends_on is not None and ends_on >= starts_on:
        period_days = (ends_on - starts_on).days + 1
        if current_day >= starts_on:
            elapsed_days = min(period_days, (min(current_day, ends_on) - starts_on).days + 1)
        if current_day <= ends_on:
            remaining_days = max(0, (ends_on - max(current_day, starts_on)).days + 1)
        if status == "active" and total is not None and used is not None and elapsed_days > 0:
            projected_used = min(total, used / elapsed_days * period_days)
        elif status == "not-started":
            projected_used = 0.0

    projected_unused = None
    projected_loss = None
    confirmed_loss = None
    if total is not None and total > 0 and projected_used is not None and paid is not None and paid > 0:
        projected_unused = max(0.0, total - projected_used)
        projected_loss = paid * projected_unused / total
    if status == "expired" and total is not None and total > 0 and remaining is not None and paid is not None and paid > 0:
        confirmed_loss = paid * max(0.0, remaining) / total

    if paid is None or paid <= 0:
        data_status = "missing-paid"
    elif total is None or total <= 0:
        data_status = "missing-quota"
    else:
        data_status = "available"
    return {
        "status": status,
        "data_status": data_status,
        "paid_amount_cny": paid,
        "quota_total": total,
        "quota_used": used,
        "quota_remaining": remaining,
        "utilization_ratio": utilization,
        "consumed_cost_cny": consumed_cost,
        "full_use_cost_per_credit_cny": unit_cost,
        "projected_used": projected_used,
        "projected_unused": projected_unused,
        "projected_expiry_loss_cny": projected_loss if status != "expired" else None,
        "confirmed_expiry_loss_cny": confirmed_loss,
        "period_start": starts_on.isoformat() if starts_on else "",
        "period_end": ends_on.isoformat() if ends_on else "",
        "period_days": period_days,
        "elapsed_days": elapsed_days,
        "remaining_days": remaining_days,
        "quota_source_id": str(source.get("id") or record.get("quota_source_id") or ""),
        "label": str(source.get("label") or "套餐额度"),
    }


def subscription_usage_summary(
    records: Iterable[Mapping[str, Any]],
    quota_items: Iterable[Mapping[str, Any]] = (),
    *,
    as_of: Any = None,
) -> dict[str, Any]:
    """Summarize package utilization, theoretical unit cost, and expiry loss."""
    subscription_records = [
        dict(item) for item in records
        if str(item.get("billing_type") or "topup").strip().lower() == "subscription"
    ]
    subscriptions = [dict(item) for item in quota_items if str(item.get("type") or "").strip().lower() == "subscription"]
    packages: list[dict[str, Any]] = []
    for index, record in enumerate(subscription_records):
        requested_id = str(record.get("quota_source_id") or "").strip()
        quota_item = next((item for item in subscriptions if str(item.get("id") or "") == requested_id), None)
        if quota_item is None and len(subscriptions) == 1:
            quota_item = subscriptions[0]
        if quota_item is None and index < len(subscriptions):
            quota_item = subscriptions[index]
        packages.append({
            "record_id": str(record.get("id") or ""),
            **subscription_usage_metrics(record, quota_item, as_of=as_of),
        })

    valid_packages = [item for item in packages if item.get("data_status") == "available"]
    total_paid = sum(float(item.get("paid_amount_cny") or 0) for item in valid_packages)
    total_quota = sum(float(item.get("quota_total") or 0) for item in valid_packages)
    total_used = sum(float(item.get("quota_used") or 0) for item in valid_packages)
    total_consumed = sum(float(item.get("consumed_cost_cny") or 0) for item in valid_packages)
    total_projected_loss = sum(float(item.get("projected_expiry_loss_cny") or 0) for item in valid_packages)
    total_confirmed_loss = sum(float(item.get("confirmed_expiry_loss_cny") or 0) for item in valid_packages)
    return {
        "packages": packages,
        "package_count": len(packages),
        "available_count": len(valid_packages),
        "paid_amount_cny": total_paid,
        "quota_total": total_quota,
        "quota_used": total_used,
        "utilization_ratio": total_used / total_quota if total_quota > 0 else None,
        "consumed_cost_cny": total_consumed,
        "full_use_cost_per_credit_cny": total_paid / total_quota if total_quota > 0 else None,
        "projected_expiry_loss_cny": total_projected_loss,
        "confirmed_expiry_loss_cny": total_confirmed_loss,
    }


def attribute_supplier_costs(
    events: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    starts_at = _datetime(period_start)
    ends_at = _datetime(period_end)
    record_list = list(records)
    event_list: list[dict[str, Any]] = []
    for raw in events:
        item = dict(raw)
        created_at = _datetime(item.get("created_at"))
        if created_at is None or (starts_at and created_at < starts_at) or (ends_at and created_at > ends_at):
            continue
        if str(item.get("status") or "success").lower() != "success":
            continue
        event_list.append(item)

    amortization = subscription_amortization(record_list, period_start, period_end)
    events_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in event_list:
        event_day = _date(event.get("created_at"))
        if event_day is not None:
            events_by_day[event_day.isoformat()].append(event)

    event_costs: dict[str, dict[str, Any]] = {}
    subscription_allocated = 0.0
    subscription_unallocated = 0.0
    for day_key, daily_cny in amortization["daily_costs"].items():
        day_events = events_by_day.get(day_key, [])
        if not day_events:
            subscription_unallocated += daily_cny
            continue
        price_weights = [max(0.0, _number(event.get("estimated_cost")) or 0.0) for event in day_events]
        if sum(price_weights) <= 0:
            price_weights = [max(0.0, _number(event.get("total_tokens")) or 0.0) for event in day_events]
        total_weight = sum(price_weights)
        if total_weight <= 0:
            subscription_unallocated += daily_cny
            continue
        for event, weight in zip(day_events, price_weights):
            event_id = str(event.get("id") or event.get("request_id") or "")
            allocated = daily_cny * weight / total_weight
            event_costs[event_id] = {
                "source": "subscription",
                "topup_cny": 0.0,
                "subscription_cny": allocated,
                "total_cny": allocated,
                "status": "attributed-subscription",
            }
            subscription_allocated += allocated

    topup_events = [
        event
        for event in event_list
        if str(event.get("id") or event.get("request_id") or "") not in event_costs
    ]
    topup_allocations = _allocate_topup_credit_batches(topup_events, record_list)
    topup_cost = 0.0
    for event in topup_events:
        event_id = str(event.get("id") or event.get("request_id") or "")
        allocation = topup_allocations.get(
            event_id,
            {
                "source": "none",
                "topup_cny": 0.0,
                "subscription_cny": 0.0,
                "total_cny": 0.0,
                "status": "missing-cost-basis",
            },
        )
        event_costs[event_id] = allocation
        topup_cost += float(allocation.get("topup_cny") or 0)

    unattributed = sum(1 for item in event_costs.values() if not str(item.get("status") or "").startswith("attributed-"))
    return {
        "event_costs": event_costs,
        "topup_cash_cost_cny": topup_cost,
        "subscription_amortized_cny": amortization["amortized_cny"],
        "subscription_allocated_cny": subscription_allocated,
        "subscription_unallocated_cny": subscription_unallocated,
        "attributed_cash_cost_cny": topup_cost + subscription_allocated,
        "unattributed_requests": unattributed,
    }


__all__ = [
    "attribute_supplier_costs",
    "subscription_amortization",
    "subscription_usage_metrics",
    "subscription_usage_summary",
    "weighted_topup_basis",
]
