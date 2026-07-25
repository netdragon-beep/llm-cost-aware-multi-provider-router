"""Optional LiteLLM response compatibility patch for Claude Desktop discovery.

Python loads this module before the LiteLLM CLI starts. The patch is guarded by
an environment variable so normal RelayDeck/admin-panel Python processes are
not affected.
"""

from __future__ import annotations

import os


def _patch_litellm_model_discovery() -> None:
    if os.environ.get("RELAYDECK_LITELLM_DISCOVERY_PATCH") != "1":
        return

    try:
        from litellm.proxy import utils
    except Exception:
        return

    original = getattr(utils, "create_model_info_response", None)
    if original is None or getattr(original, "_relaydeck_patched", False):
        return

    def create_model_info_response_with_relaydeck_metadata(*args, **kwargs):
        response = original(*args, **kwargs)
        model_id = kwargs.get("model_id")
        if model_id is None and args:
            model_id = args[0]
        router = kwargs.get("llm_router")
        if router is None:
            return response
        try:
            deployments = router.get_model_list(model_name=str(model_id or "")) or []
            for deployment in deployments:
                model_info = deployment.get("model_info", {}) or {}
                tier = str(model_info.get("anthropic_family_tier") or "").strip().lower()
                if tier in {"opus", "sonnet", "haiku"}:
                    return {**response, "anthropic_family_tier": tier}
        except Exception:
            return response
        return response

    create_model_info_response_with_relaydeck_metadata._relaydeck_patched = True
    utils.create_model_info_response = create_model_info_response_with_relaydeck_metadata


_patch_litellm_model_discovery()
