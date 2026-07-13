from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "litellm.yaml"


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    rows = config.get("model_list", [])
    clean = []
    seen = set()

    for row in rows:
        params = row.get("litellm_params", {}) or {}
        info = row.get("model_info", {}) or {}

        alias = str(row.get("model_name", "")).strip()
        model = str(params.get("model", "")).strip()
        base = str(params.get("api_base", "")).strip()
        key_ref = str(params.get("api_key", "")).strip()
        source = str(info.get("source", "")).strip()
        relay = str(info.get("relay_label", "")).strip()

        if source == "ccswitch-webdata":
            if not base.startswith("http"):
                continue
            if not alias or alias.startswith("http"):
                continue
            if not model or model.startswith("http"):
                continue

        dedupe_key = (alias, base, model, key_ref)
        if dedupe_key in seen:
            for kept in clean:
                kept_params = kept.get("litellm_params", {}) or {}
                kept_info = kept.get("model_info", {}) or {}
                kept_key = (
                    str(kept.get("model_name", "")).strip(),
                    str(kept_params.get("api_base", "")).strip(),
                    str(kept_params.get("model", "")).strip(),
                    str(kept_params.get("api_key", "")).strip(),
                )
                if kept_key == dedupe_key and relay and not kept_info.get("relay_label"):
                    kept.setdefault("model_info", {})["relay_label"] = relay
                    break
            continue

        seen.add(dedupe_key)
        clean.append(row)

    config["model_list"] = clean
    CONFIG_PATH.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"{len(rows)} -> {len(clean)}")


if __name__ == "__main__":
    main()
