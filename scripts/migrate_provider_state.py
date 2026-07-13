from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN_DIR = ROOT / "admin-panel"
STATE_PATH = ROOT / "config" / "relaydeck-state.json"


def main() -> None:
    sys.path.insert(0, str(ADMIN_DIR))
    import app as relay_app  # noqa: WPS433

    config = relay_app.load_config()
    providers = []
    priorities_by_model: dict[str, int] = {}

    for row in config.get("model_list", []):
        params = row.get("litellm_params", {}) or {}
        info = row.get("model_info", {}) or {}
        public_model_name = info.get("public_model_name") or row.get("model_name", "")
        if not public_model_name:
            continue

        priority = int(info.get("priority", priorities_by_model.get(public_model_name, 100)))
        priorities_by_model[public_model_name] = priority + 10

        providers.append(
            {
                "relay_label": info.get("relay_label", ""),
                "model_name": public_model_name,
                "upstream_model": params.get("model", ""),
                "api_base": params.get("api_base", ""),
                "api_key_env": relay_app.read_env_var_name(params.get("api_key")),
                "custom_llm_provider": params.get("custom_llm_provider", "openai"),
                "rpm": params.get("rpm"),
                "priority": priority,
                "enabled": bool(info.get("enabled", True)),
            }
        )

    router_settings = config.get("router_settings", {})
    litellm_settings = config.get("litellm_settings", {})

    relay_app.save_provider_state(providers, router_settings, litellm_settings)
    rebuilt = relay_app.build_litellm_config_from_providers(
        providers=providers,
        router_settings=router_settings,
        litellm_settings=litellm_settings,
    )
    relay_app.save_config(rebuilt)

    print(
        json.dumps(
            {
                "state_path": str(STATE_PATH),
                "provider_count": len(providers),
                "fallbacks": rebuilt.get("router_settings", {}).get("fallbacks", []),
                "model_names": [row.get("model_name") for row in rebuilt.get("model_list", [])],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
