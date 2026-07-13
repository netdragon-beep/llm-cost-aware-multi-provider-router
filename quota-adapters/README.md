Quota adapter scripts

Detailed guide
- Full guide for writing a new supplier adapter:
  [docs/provider-quota-adapter-guide.md](../docs/provider-quota-adapter-guide.md)

Purpose
- Put custom quota-fetch scripts here when a provider does not expose balance data through a normal API.
- The admin panel can call these scripts and show the returned balance in the usage/quota dashboard.

How to use
1. Create a script in this directory. Supported file types:
   - `.py`
   - `.ps1`
   - `.cmd`
   - `.bat`
2. In the admin panel, open an API profile and set:
   - `quota.adapter = custom-script`
   - `quota.script_name = your_script.py`
   - optional: `quota.script_timeout_sec = 60`
3. Click `刷新额度`.

Input contract
- The script receives one JSON object from `stdin`.
- The same JSON is also available in env var `RELAYDECK_QUOTA_CONTEXT`.

Input shape
```json
{
  "profile": {
    "id": "api_xxx",
    "label": "LingSuan",
    "supplier_id": "supplier_xxx",
    "api_base": "https://example.com/v1",
    "api_key_env": "MY_API_KEY",
    "api_key_value": "<provider-api-key>",
    "custom_llm_provider": "openai",
    "quota": {
      "adapter": "custom-script",
      "script_name": "example_lingsuan.py",
      "auth_token": "optional bearer token"
    }
  },
  "window": {
    "start_at": "2026-06-01T00:00:00+08:00",
    "end_at": "2026-06-26T09:00:00+08:00"
  }
}
```

Output contract
- Print exactly one JSON object to `stdout`.

Required output fields
```json
{
  "status": "ok",
  "adapter": "custom-script",
  "currency": "CNY",
  "balance_total": 100,
  "balance_used": 37.5,
  "balance_remaining": 62.5,
  "message": "",
  "raw_summary": {
    "note": "optional debug info"
  }
}
```

Status values
- `ok`
- `unsupported`
- `error`

Notes
- `raw_summary` is optional but recommended for debugging.
- If the target site requires browser automation, the script can do that itself and only return the final JSON payload.
- Keep secrets out of source files when possible; prefer values already stored in the profile quota config or environment.

Examples
- `example_lingsuan.py` shows a simple site-specific adapter skeleton.
- `adapter_template.py` is the recommended copy-and-edit starting point for a new supplier.
