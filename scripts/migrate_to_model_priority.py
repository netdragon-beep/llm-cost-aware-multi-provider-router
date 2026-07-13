from pathlib import Path

from admin_panel.app import (
    ROOT,
    build_litellm_config_from_providers,
    load_provider_state,
    save_config,
    save_provider_state,
)


def main() -> None:
    providers, router_settings, litellm_settings = load_provider_state()
    config = build_litellm_config_from_providers(
        providers=providers,
        router_settings=router_settings,
        litellm_settings=litellm_settings,
    )
    save_provider_state(providers, router_settings, litellm_settings)
    save_config(config)
    print(f"migrated providers={len(providers)} root={ROOT}")


if __name__ == "__main__":
    main()
