"""Claude Desktop model-discovery compatibility helpers.

The metadata in this module affects only Anthropic-client model discovery. It
does not rename models or change the OpenAI-compatible routing surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLAUDE_DISCOVERY_TIERS = frozenset({"opus", "sonnet", "haiku"})


def normalize_claude_discovery_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(settings or {})
    default_tier = str(source.get("claude_desktop_default_tier") or "sonnet").strip().lower()
    if default_tier not in CLAUDE_DISCOVERY_TIERS:
        default_tier = "sonnet"
    return {
        "claude_desktop_discovery_enabled": bool(
            source.get("claude_desktop_discovery_enabled", False)
        ),
        "claude_desktop_default_tier": default_tier,
    }


def _infer_claude_tier(model_name: str) -> str | None:
    name = str(model_name or "").strip().lower()
    if not name:
        return None
    if "opus" in name:
        return "opus"
    if "haiku" in name:
        return "haiku"
    if "sonnet" in name:
        return "sonnet"
    return None


def claude_discovery_metadata(
    model_name: str,
    settings: Mapping[str, Any] | None,
) -> dict[str, str]:
    normalized = normalize_claude_discovery_settings(settings)
    if not normalized["claude_desktop_discovery_enabled"]:
        return {}
    return {
        "anthropic_family_tier": _infer_claude_tier(model_name)
        or normalized["claude_desktop_default_tier"],
    }


__all__ = [
    "CLAUDE_DISCOVERY_TIERS",
    "claude_discovery_metadata",
    "normalize_claude_discovery_settings",
]
