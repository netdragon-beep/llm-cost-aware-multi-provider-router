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


def build_portal_base(profile, quota):
    portal_base = str(quota.get("portal_base") or "").strip()
    if portal_base:
        return portal_base.rstrip("/")
    api_base = str(profile.get("api_base") or "").strip()
    if not api_base:
        return ""
    parsed = urllib.parse.urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def extract_snapshot(payloads, default_currency="CNY"):
    total = used = remaining = None
    currency = default_currency

    # TODO: Replace these endpoint names with the real endpoints for your provider.
    for name in ("balance", "usage", "subscription"):
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
                "credit_total",
            })
        if used is None:
            used = pick_numeric(payload, {
                "used",
                "spent",
                "consumed",
                "usage",
                "balance_used",
                "used_amount",
                "total_usage",
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
        found_currency = recursive_find(payload, {"currency", "unit"})
        if isinstance(found_currency, str) and found_currency.strip():
            currency = found_currency.strip()

    if used is None and total is not None and remaining is not None:
        used = total - remaining
    if remaining is None and total is not None and used is not None:
        remaining = total - used

    return total, used, remaining, currency


def main():
    context = read_context()
    profile = context.get("profile") or {}
    quota = profile.get("quota") or {}

    portal_base = build_portal_base(profile, quota)
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = str(quota.get("session_cookie") or "").strip()
    default_currency = str(quota.get("currency") or "CNY")

    if not portal_base:
        print(json.dumps({
            "status": "unsupported",
            "adapter": "custom-script",
            "message": "Missing portal_base and unable to infer it from api_base.",
            "raw_summary": {},
        }, ensure_ascii=False))
        return

    # TODO: Adjust the authentication mode for your provider.
    headers = {
        "Accept": "application/json",
        "User-Agent": "RelayDeck-Local/1.0",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    elif session_cookie:
        headers["Cookie"] = session_cookie
    else:
        print(json.dumps({
            "status": "unsupported",
            "adapter": "custom-script",
            "message": "Missing auth_token or session_cookie.",
            "raw_summary": {
                "portal_base": portal_base,
            },
        }, ensure_ascii=False))
        return

    payloads = {}
    raw_summary = {
        "portal_base": portal_base,
    }

    endpoint_specs = [
        # TODO: Replace these with the real provider endpoints.
        ("balance", "/api/v1/balance"),
        ("usage", "/api/v1/usage"),
        ("subscription", "/api/v1/subscriptions/summary"),
    ]

    for name, path in endpoint_specs:
        url = f"{portal_base}{path}"
        try:
            status_code, body = request_json(url, headers)
            raw_summary[name] = {
                "url": url,
                "status_code": status_code,
                "body": body,
            }
            if status_code < 400:
                payloads[name] = normalize_payload(body)
        except Exception as exc:
            raw_summary[name] = {
                "url": url,
                "error": str(exc),
            }

    total, used, remaining, currency = extract_snapshot(payloads, default_currency)
    status = "ok" if any(value is not None for value in (total, used, remaining)) else "unsupported"
    message = "" if status == "ok" else "The provider responded, but no unified quota fields were recognized yet."

    print(json.dumps({
        "status": status,
        "adapter": "custom-script",
        "currency": currency,
        "balance_total": total,
        "balance_used": used,
        "balance_remaining": remaining,
        # Return one item per independent quota source when the provider has both.
        "quota_items": [],
        "message": message,
        # Populate this list only after implementing the provider's real price source.
        "pricing_catalog": [],
        # RelayDeck encrypts accepted updates after checking the companion manifest.
        "credential_updates": {},
        "raw_summary": raw_summary,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
