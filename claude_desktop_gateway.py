"""Anthropic model-discovery adapter in front of the Claude LiteLLM gateway."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask


DEFAULT_INTERNAL_PORT = 4102
VALID_TIERS = frozenset({"opus", "sonnet", "haiku", "fable"})
ROOT = Path(__file__).resolve().parent
CLIENT_SHORTCUTS_PATH = ROOT / "config" / "client-shortcuts.json"
LEGACY_CLAUDE_SHORTCUT_TIERS = {
    "claude-haiku": "haiku",
    "claude-sonnet": "sonnet",
    "claude-opus": "opus",
}
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

app = FastAPI(title="RelayDeck Claude Desktop Gateway")


def configured_tier() -> str:
    tier = os.environ.get("CLAUDE_DESKTOP_DEFAULT_TIER", "haiku").strip().lower()
    return tier if tier in VALID_TIERS else "haiku"


def infer_shortcut_tier(name: str) -> str:
    normalized = name.strip().lower()
    for tier in ("haiku", "sonnet", "opus", "fable"):
        if tier in normalized:
            return tier
    return "sonnet"


def build_model_discovery_response(
    models: Iterable[dict[str, Any]],
    *,
    tier: str = "haiku",
) -> dict[str, Any]:
    """Return the model discovery shape expected by Claude Desktop."""
    normalized_tier = tier if tier in VALID_TIERS else "haiku"
    data: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for model in models:
        model_id = str(model.get("id") or "").strip()
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        data.append(
            {
                "type": "model",
                "id": model_id,
                "display_name": str(model.get("display_name") or model_id),
                "anthropic_family_tier": normalized_tier,
            }
        )
    return {
        "data": data,
        "has_more": False,
        "first_id": None,
        "last_id": None,
    }


def load_claude_shortcuts() -> list[dict[str, str]]:
    """Load editable Claude Code aliases from local state without failing requests."""
    try:
        payload = json.loads(CLIENT_SHORTCUTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    submitted = payload.get("claude_code")
    if isinstance(submitted, dict):
        return [
            {"name": name, "tier": infer_shortcut_tier(name), "target": str(submitted.get(name) or "").strip()}
            for name in LEGACY_CLAUDE_SHORTCUT_TIERS
        ]
    if not isinstance(submitted, list):
        return []
    rows: list[dict[str, str]] = []
    for item in submitted:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        tier = infer_shortcut_tier(name)
        target = str(item.get("target") or "").strip()
        if name:
            rows.append({"name": name, "tier": tier, "target": target})
    return rows


def shortcut_target_alias(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model.strip().lower()).strip("-") or "relay"
    return f"claude-haiku-relaydeck-{slug}"


def rewrite_shortcut_model(model: str, slots: Iterable[dict[str, str]]) -> str:
    """Translate an exact alias, or an unambiguous Claude family, to a RelayDeck route."""
    normalized_model = model.strip().casefold()
    configured_slots = list(slots)
    for slot in configured_slots:
        name = str(slot.get("name") or "").strip()
        if name.casefold() == normalized_model and (target := str(slot.get("target") or "").strip()):
            return shortcut_target_alias(target)

    # Claude Code's built-in picker submits canonical ids such as ``claude-opus-5``.
    # A user-facing alias such as ``Opus`` should still work when its family has one target.
    requested_tier = infer_shortcut_tier(model)
    family_targets = {
        str(slot.get("target") or "").strip()
        for slot in configured_slots
        if str(slot.get("target") or "").strip()
        and infer_shortcut_tier(str(slot.get("name") or "")) == requested_tier
    }
    if len(family_targets) == 1:
        return shortcut_target_alias(family_targets.pop())
    return model


def build_shortcut_model_discovery_response(slots: Iterable[dict[str, str]]) -> dict[str, Any]:
    """Expose configured Claude Code aliases without leaking internal LiteLLM aliases."""
    models = [
        {
            "type": "model",
            "id": slot["name"],
            "display_name": f"{slot['name']} -> {slot['target']}",
            "anthropic_family_tier": slot["tier"],
        }
        for slot in slots
        if slot.get("target")
    ]
    return {
        "data": models,
        "has_more": False,
        "first_id": None,
        "last_id": None,
    }


def internal_base_url() -> str:
    port = int(os.environ.get("CLAUDE_LITELLM_INTERNAL_PORT", str(DEFAULT_INTERNAL_PORT)))
    return f"http://127.0.0.1:{port}"


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


async def close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


async def request_upstream(request: Request, *, stream: bool) -> tuple[httpx.Response, httpx.AsyncClient]:
    body = await request.body()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json" and request.method in {"POST", "PUT", "PATCH"}:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            payload["model"] = rewrite_shortcut_model(payload["model"], load_claude_shortcuts())
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    client = httpx.AsyncClient(timeout=None, follow_redirects=False)
    upstream_request = client.build_request(
        request.method,
        f"{internal_base_url()}{request.url.path}",
        params=list(request.query_params.multi_items()),
        headers=filtered_headers(request.headers.items()),
        content=body,
    )
    return await client.send(upstream_request, stream=stream), client


@app.get("/v1/models")
async def desktop_model_discovery(request: Request) -> Response:
    shortcuts = load_claude_shortcuts()
    if shortcuts:
        return JSONResponse(build_shortcut_model_discovery_response(shortcuts))

    upstream, client = await request_upstream(request, stream=False)
    try:
        if upstream.status_code >= 400:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
            )
        payload = upstream.json()
        return JSONResponse(
            build_model_discovery_response(payload.get("data") or [], tier=configured_tier())
        )
    finally:
        await close_upstream(upstream, client)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def forward_to_litellm(request: Request, path: str) -> Response:
    upstream, client = await request_upstream(request, stream=True)
    response_headers = filtered_headers(upstream.headers.items())
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(close_upstream, upstream, client),
    )
