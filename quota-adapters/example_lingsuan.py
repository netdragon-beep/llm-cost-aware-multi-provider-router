import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def read_context():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def recursive_find(obj, aliases):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in aliases:
                return value
        for value in obj.values():
            found = recursive_find(value, aliases)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find(item, aliases)
            if found is not None:
                return found
    return None


def numeric_value(value):
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


def pick_numeric(payload, aliases):
    return numeric_value(recursive_find(payload, aliases))


def normalize_payload(body):
    if isinstance(body, dict) and body.get("code") == 0 and "data" in body:
        data = body.get("data")
        if isinstance(data, (dict, list)):
            return data
    return body


def extract_snapshot(payloads, default_currency="CNY"):
    total = used = remaining = None
    currency = default_currency
    for name in ("subscriptions_summary", "subscriptions_active", "subscriptions_progress", "usage", "balance"):
        payload = payloads.get(name)
        if payload is None:
            continue
        if total is None:
            total = pick_numeric(payload, {
                "total",
                "limit",
                "quota_total",
                "total_quota",
                "balance_total",
                "total_amount",
                "granted",
                "credit_total",
            })
        if remaining is None:
            remaining = pick_numeric(payload, {
                "remaining",
                "available",
                "balance",
                "remaining_balance",
                "current_balance",
                "available_balance",
                "credit_balance",
            })
        if used is None:
            used = pick_numeric(payload, {
                "used",
                "spent",
                "consumed",
                "usage",
                "balance_used",
                "used_amount",
                "cost_used",
                "total_usage",
            })
        found_currency = recursive_find(payload, {"currency", "unit"})
        if isinstance(found_currency, str) and found_currency.strip():
            currency = found_currency.strip()
    if used is None and total is not None and remaining is not None:
        used = total - remaining
    if remaining is None and total is not None and used is not None:
        remaining = total - used
    return total, used, remaining, currency


def request_json(url, headers):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.getcode(), json.loads(body)
            except Exception:
                return response.getcode(), {"raw_text": body[:500]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw_text": body[:500]}


def main():
    context = read_context()
    profile = context.get("profile") or {}
    quota = profile.get("quota") or {}
    auth_token = str(quota.get("auth_token") or "").strip()
    api_base = str(profile.get("api_base") or "").strip()
    portal_base = str(quota.get("portal_base") or "").strip()
    if not portal_base and api_base:
        parsed = urllib.parse.urlparse(api_base)
        if parsed.scheme and parsed.netloc:
            portal_base = f"{parsed.scheme}://{parsed.netloc}"

    if not auth_token:
        print(json.dumps({
            "status": "unsupported",
            "adapter": "custom-script",
            "message": "Missing quota.auth_token for LingSuan script.",
            "raw_summary": {"portal_base": portal_base},
        }, ensure_ascii=False))
        return

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "User-Agent": "RelayDeck-Local/1.0",
    }
    payloads = {}
    raw_summary = {"portal_base": portal_base}
    for name, path in (
        ("subscriptions_summary", "/api/v1/subscriptions/summary"),
        ("subscriptions_active", "/api/v1/subscriptions/active"),
        ("subscriptions_progress", "/api/v1/subscriptions/progress"),
        ("usage", "/api/v1/usage"),
        ("balance", "/api/v1/balance"),
    ):
        status_code, body = request_json(f"{portal_base}{path}", headers)
        raw_summary[name] = {"status_code": status_code, "body": body}
        if status_code < 400:
            payloads[name] = normalize_payload(body)

    total, used, remaining, currency = extract_snapshot(payloads, str(quota.get("currency") or "CNY"))
    status = "ok" if any(value is not None for value in (total, used, remaining)) else "unsupported"
    message = "" if status == "ok" else "No unified quota fields were found in LingSuan responses."
    print(json.dumps({
        "status": status,
        "adapter": "custom-script",
        "currency": currency,
        "balance_total": total,
        "balance_used": used,
        "balance_remaining": remaining,
        "message": message,
        "raw_summary": raw_summary,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
