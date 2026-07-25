# Supplier Quota Vault Design

## Goal

Move quota ownership and portal credentials from individual API profiles to one shared supplier-platform quota configuration, with sensitive values encrypted by Windows DPAPI CurrentUser.

## Boundaries

- A supplier platform is identified by the normalized API hostname already used by the supplier view, for example `lingsuan.top`.
- All API profiles in one supplier platform share one quota snapshot, adapter configuration, login session and recharge history.
- API-profile pricing and token usage settings remain on the API profile.
- Credentials never appear in `/api/state`, `relaydeck-state.json`, logs or adapter command-line arguments.

## Credential Vault

`admin-panel/credential_vault.py` stores an encrypted JSON credential object per supplier platform in `data/credential-vault.json`. Encryption uses Windows `CryptProtectData` and `CryptUnprotectData` with CurrentUser scope and RelayDeck-specific optional entropy. The vault exposes status metadata without returning plaintext.

## Adapter Execution

RelayDeck resolves the supplier quota configuration, decrypts only the supplier credentials needed for the adapter, and supplies them to the existing adapter process through JSON stdin. Adapter credential updates are validated and written back through the vault. Custom scripts remain trusted local code and never access the vault file directly.

## State And Migration

`supplier_quotas` is a top-level mapping keyed by supplier platform. Non-sensitive adapter settings are stored there. On load, legacy API quota values are grouped by platform; sensitive fields are imported into the vault and omitted from normalized API profiles.

## Failure Rules

- Failed refreshes retain the last successful balance snapshot.
- Authentication failures report `auth_required` or `error` without replacing the previous balance with zero.
- Initial login may still require human intervention for CAPTCHA, SMS, email confirmation or unsupported 2FA.

