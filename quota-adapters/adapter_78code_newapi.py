import base64
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


def make_headers(auth_token=None, user_id=None, session_cookie=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "RelayDeck-Local/1.0",
    }
    if auth_token not in (None, ""):
        headers["Authorization"] = f"Bearer {auth_token}"
    if session_cookie not in (None, ""):
        headers["Cookie"] = str(session_cookie)
    if user_id not in (None, ""):
        headers["New-Api-User"] = str(user_id)
    return headers


def request_json(method, url, headers, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.getcode(), json.loads(body)
            except Exception:
                return response.getcode(), {"raw_text": body[:1000]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw_text": body[:1000]}
    except Exception as exc:
        return None, {"error": str(exc)}


def decode_jwt_payload(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8", errors="replace")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_user_id(candidate):
    text = str(candidate or "").strip()
    if text:
        return text
    return ""


def normalize_success_payload(body):
    if isinstance(body, dict) and body.get("success") is True:
        return body.get("data")
    return None


def build_result(status, message="", **kwargs):
    payload = {
        "status": status,
        "adapter": "custom-script",
        "currency": kwargs.pop("currency", "QUOTA"),
        "balance_total": kwargs.pop("balance_total", None),
        "balance_used": kwargs.pop("balance_used", None),
        "balance_remaining": kwargs.pop("balance_remaining", None),
        "message": message,
        "raw_summary": kwargs.pop("raw_summary", {}),
    }
    payload.update(kwargs)
    return payload


def main():
    context = read_context()
    profile = context.get("profile") or {}
    quota = profile.get("quota") or {}
    api_base = str(profile.get("api_base") or "").strip()
    api_key = str(profile.get("api_key_value") or "").strip()
    auth_token = str(quota.get("auth_token") or "").strip()
    session_cookie = str(quota.get("session_cookie") or quota.get("cookie") or "").strip()
    explicit_user_id = extract_user_id(quota.get("user_id"))
    portal_base = str(quota.get("portal_base") or "").strip()
    if not portal_base and api_base:
        parsed = urllib.parse.urlparse(api_base)
        if parsed.scheme and parsed.netloc:
            portal_base = f"{parsed.scheme}://{parsed.netloc}"

    if not auth_token and not session_cookie:
        print(json.dumps(build_result(
            "unsupported",
            "Missing quota.auth_token or quota.session_cookie. 78code/New API shared quota requires a logged-in session.",
            raw_summary={"portal_base": portal_base},
        ), ensure_ascii=False))
        return

    jwt_payload = decode_jwt_payload(auth_token)
    user_id = explicit_user_id or extract_user_id(jwt_payload.get("user_id") or jwt_payload.get("id"))
    raw_summary = {
        "portal_base": portal_base,
        "derived_user_id": user_id,
        "auth_mode": "cookie" if session_cookie else "bearer",
        "jwt_payload_keys": sorted(jwt_payload.keys())[:20],
    }

    headers = make_headers(auth_token or None, user_id or None, session_cookie or None)
    status_code, self_body = request_json("GET", f"{portal_base}/api/user/self", headers)
    raw_summary["user_self"] = {"status_code": status_code, "body": self_body}
    self_data = normalize_success_payload(self_body) if isinstance(self_body, dict) else None

    if not self_data and not explicit_user_id and not user_id:
        print(json.dumps(build_result(
            "unsupported",
            "Cannot determine user_id from quota.user_id or auth_token JWT payload. Please fill quota.user_id in the panel.",
            raw_summary=raw_summary,
        ), ensure_ascii=False))
        return

    if not self_data and user_id:
        # Retry once with explicit New-Api-User when the first request used a derived value late or the server is strict.
        headers = make_headers(auth_token or None, user_id, session_cookie or None)
        status_code, self_body = request_json("GET", f"{portal_base}/api/user/self", headers)
        raw_summary["user_self_retry"] = {"status_code": status_code, "body": self_body}
        self_data = normalize_success_payload(self_body) if isinstance(self_body, dict) else None

    if not self_data:
        message = "Failed to read 78code user profile quota."
        if isinstance(self_body, dict):
            message = str(self_body.get("message") or self_body.get("error") or message)
        print(json.dumps(build_result("error", message, raw_summary=raw_summary), ensure_ascii=False))
        return

    user_id = extract_user_id(self_data.get("id") or self_data.get("user_id") or user_id)

    wallet_balance = numeric_value(self_data.get("quota"))
    wallet_used_total = numeric_value(self_data.get("used_quota"))
    wallet_request_count = numeric_value(self_data.get("request_count"))

    subscription_status, subscription_body = request_json("GET", f"{portal_base}/api/subscription/self", headers)
    raw_summary["subscription_self"] = {"status_code": subscription_status, "body": subscription_body}
    subscription_data = normalize_success_payload(subscription_body) if isinstance(subscription_body, dict) else None

    plans_status, plans_body = request_json("GET", f"{portal_base}/api/subscription/plans", headers)
    raw_summary["subscription_plans"] = {"status_code": plans_status, "body": plans_body}
    plans_data = normalize_success_payload(plans_body) if isinstance(plans_body, dict) else None
    plan_title_map = {}
    if isinstance(plans_data, list):
        for item in plans_data:
            if isinstance(item, dict):
                plan = item.get("plan") if isinstance(item.get("plan"), dict) else item
                plan_id = plan.get("id") if isinstance(plan, dict) else None
                title = plan.get("title") if isinstance(plan, dict) else None
                if plan_id is not None and title:
                    plan_title_map[str(plan_id)] = str(title)

    billing_preference = ""
    active_subscriptions = []
    all_subscriptions = []
    if isinstance(subscription_data, dict):
        billing_preference = str(subscription_data.get("billing_preference") or "")
        if isinstance(subscription_data.get("subscriptions"), list):
            active_subscriptions = [item for item in subscription_data.get("subscriptions") if isinstance(item, dict)]
        if isinstance(subscription_data.get("all_subscriptions"), list):
            all_subscriptions = [item for item in subscription_data.get("all_subscriptions") if isinstance(item, dict)]

    package_total = 0.0
    package_used = 0.0
    package_remaining = 0.0
    package_next_reset_time = None
    package_end_time = None
    package_titles = []
    for item in active_subscriptions:
        subscription = item.get("subscription") if isinstance(item.get("subscription"), dict) else {}
        amount_total = numeric_value(subscription.get("amount_total")) or 0.0
        amount_used = numeric_value(subscription.get("amount_used")) or 0.0
        amount_remaining = max(0.0, amount_total - amount_used) if amount_total > 0 else None
        package_total += amount_total
        package_used += amount_used
        if amount_remaining is not None:
            package_remaining += amount_remaining
        plan_id = subscription.get("plan_id")
        title = plan_title_map.get(str(plan_id or "")) or str(item.get("title") or "").strip()
        if title and title not in package_titles:
            package_titles.append(title)
        next_reset_time = numeric_value(subscription.get("next_reset_time"))
        end_time = numeric_value(subscription.get("end_time"))
        if next_reset_time and (package_next_reset_time is None or next_reset_time < package_next_reset_time):
            package_next_reset_time = next_reset_time
        if end_time and (package_end_time is None or end_time > package_end_time):
            package_end_time = end_time

    has_package_quota = package_total > 0
    quota_breakdown = {
        "billing_preference": billing_preference,
        "package_quota": {
            "active_count": len(active_subscriptions),
            "all_count": len(all_subscriptions),
            "titles": package_titles,
            "balance_total": package_total if has_package_quota else None,
            "balance_used": package_used if has_package_quota else None,
            "balance_remaining": package_remaining if has_package_quota else None,
            "next_reset_time": int(package_next_reset_time) if package_next_reset_time else None,
            "end_time": int(package_end_time) if package_end_time else None,
        },
        "wallet_quota": {
            "balance_remaining": wallet_balance,
            "balance_used_total": wallet_used_total,
            "request_count": int(wallet_request_count) if wallet_request_count is not None else None,
        },
    }

    total = package_total if has_package_quota else None
    used = package_used if has_package_quota else None
    remaining = package_remaining if has_package_quota else wallet_balance

    token_item = None
    if api_key:
        token_headers = make_headers(auth_token or None, user_id or None, session_cookie or None)
        token_status, token_body = request_json(
            "GET",
            f"{portal_base}/api/token/search",
            token_headers,
            params={"token": api_key},
        )
        raw_summary["token_search"] = {"status_code": token_status, "body": token_body}
        token_data = normalize_success_payload(token_body) if isinstance(token_body, dict) else None
        if isinstance(token_data, list) and token_data:
            token_item = token_data[0]

    message_parts = []
    if billing_preference:
        message_parts.append(f"Billing preference: {billing_preference}.")
    if active_subscriptions:
        message_parts.append(f"Active subscriptions: {len(active_subscriptions)}.")
    if token_item:
        token_name = str(token_item.get("name") or "").strip()
        token_remain = numeric_value(token_item.get("remain_quota"))
        token_unlimited = bool(token_item.get("unlimited_quota"))
        if token_name:
            message_parts.append(f"Matched token: {token_name}.")
        if token_unlimited:
            message_parts.append("Token quota is unlimited.")
        elif token_remain is not None:
            message_parts.append(f"Token remain_quota: {token_remain:.2f}.")
    elif api_key:
        message_parts.append("User quota fetched, but the current API key was not found in token search results.")

    if total is None and used is None and remaining is None and token_item:
        token_remain = numeric_value(token_item.get("remain_quota"))
        if token_remain is not None:
            remaining = token_remain
            message_parts.append("Falling back to token remain_quota because user-level quota fields are empty.")

    status = "ok" if any(value is not None for value in (total, used, remaining)) else "unsupported"
    if status != "ok" and not message_parts:
        message_parts.append("No quota fields were found in 78code/New API responses.")

    print(json.dumps(build_result(
        status,
        " ".join(message_parts).strip(),
        balance_total=total,
        balance_used=used,
        balance_remaining=remaining,
        currency="QUOTA",
        raw_summary={**raw_summary, "quota_breakdown": quota_breakdown},
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
