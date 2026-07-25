# Quota adapters

RelayDeck binds one quota adapter to a supplier automatically. Users do not
select a quota method in the control panel.

Built-in bindings:

- `lingsuan.top` -> `lingsuan-web`
- `auto-code.net` -> `autocode-web`

Both built-in website adapters use the same isolated browser SSO contract.
The user signs in in a supplier-specific browser profile; RelayDeck captures
only successful same-origin session signals, encrypts the resulting token or
cookie with Windows DPAPI CurrentUser, and reuses it for quota refresh. The
adapter-specific quota script still owns the supplier's balance fields and
endpoint parsing.

For another supplier, put a script and a companion manifest in this directory.
Supported script types are `.py`, `.ps1`, `.cmd`, and `.bat`.

## Automatic binding

Name the files with the same stem:

```text
adapter_example.py
adapter_example.adapter.json
```

Declare the supplier domains in the manifest:

```json
{
  "version": 1,
  "id": "example-supplier",
  "display_name": "Example supplier quota",
  "supported_hosts": ["example.com", "api.example.com"],
  "capabilities": ["fetch_quota"],
  "credential_permissions": ["auth_token", "session_cookie"]
}
```

To enable the shared browser SSO flow for a custom website adapter, add a
`browser_sso` object to the companion manifest:

```json
{
  "browser_sso": {
    "portal_hosts": ["example.com"],
    "login_path": "/dashboard",
    "auth_me_path": "/api/v1/auth/me",
    "cookie_domains": ["example.com"],
    "google_rejection_check": true
  }
}
```

`portal_hosts` and `cookie_domains` are allowlists. The browser driver never
stores credentials from another domain. `auth_me_path` must be a successful
same-origin endpoint that proves the supplier session is authenticated.
Suppliers with a different login or session contract should implement the
same manifest fields rather than adding a new control-panel login method.

`supported_hosts` is required for automatic binding. A host also matches its
subdomains, so `example.com` covers `api.example.com`. Built-in adapters take
precedence over scripts.

The bundled example scripts intentionally have no `supported_hosts` entry and
therefore cannot bind to a real supplier accidentally. Copy
`adapter_template.py` and `adapter_template.adapter.json`, then replace the
placeholder host.

## Runtime contract

RelayDeck sends one JSON object through `stdin`. The `profile.quota` object
already contains the automatically resolved `adapter` and `script_name`.
Credentials are included only when listed in `credential_permissions`.

The script must print exactly one JSON object to `stdout`:

```json
{
  "status": "ok",
  "adapter": "custom-script",
  "currency": "CNY",
  "balance_total": 100,
  "balance_used": 37.5,
  "balance_remaining": 62.5,
  "message": "",
  "credential_updates": {},
  "raw_summary": {}
}
```

Valid status values are `ok`, `unsupported`, `error`, `auth_required`, and
`partial`.

## Security

- Supplier credentials are encrypted with Windows DPAPI CurrentUser.
- Scripts receive only credentials explicitly granted by their manifest.
- Never print tokens, cookies, passwords, or API keys to stdout, stderr,
  `message`, or `raw_summary`.
- `credential_updates` may contain only granted fields.

See [the full adapter guide](../docs/provider-quota-adapter-guide.md).

## Multiple quota sources

When a supplier exposes both a pay-as-you-go wallet and a subscription, return
both sources in `quota_items`. Do not merge them into one total:

```json
{
  "quota_items": [
    {
      "id": "wallet",
      "type": "balance",
      "label": "Pay-as-you-go balance",
      "remaining": 6.9
    },
    {
      "id": "monthly-plan",
      "type": "subscription",
      "label": "Monthly plan",
      "total": 1000,
      "used": 320,
      "remaining": 680,
      "period_end": "2026-07-31"
    }
  ]
}
```

RelayDeck normalizes each item independently and renders one progress bar per
source. The legacy scalar fields remain supported for adapters with one source.
