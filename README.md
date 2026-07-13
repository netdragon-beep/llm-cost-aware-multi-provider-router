# RelayDeck Local

RelayDeck Local is a local, self-hosted model routing stack. It combines a
LiteLLM gateway, an Open WebUI frontend, and a management panel for provider
accounts, model bindings, fallback routes, quota adapters, and usage checks.

## Components

- LiteLLM gateway: `http://127.0.0.1:4100`
- Open WebUI frontend: `http://127.0.0.1:8090`
- RelayDeck management panel: `http://127.0.0.1:8091`

## Requirements

- Windows PowerShell
- Conda
- A Conda environment named `llm-stack-local`, or an update to
  `scripts/common.ps1` with the local environment path
- Python packages used by `admin-panel/app.py`
- LiteLLM and Open WebUI installed in the configured Conda environment

The startup scripts currently expect the environment at:
`D:/conda/envs/llm-stack-local`.

## First-time setup

1. Create or activate the Conda environment:

   ```powershell
   conda create -n llm-stack-local python=3.11
   conda activate llm-stack-local
   pip install -r requirements.txt
   ```

   The pinned file includes the current management panel, LiteLLM, and
   Open WebUI dependencies. Review and update the pins when upgrading the
   Conda environment.

2. Create the local environment file:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Replace the placeholder values in `.env`. Never commit `.env` or provider
   API keys. The repository intentionally ignores `.env`, generated state,
   databases, logs, and service runtime files.

4. Optional: copy the configuration templates if you need a clean starting
   point:

   ```powershell
   Copy-Item config/litellm.yaml.example config/litellm.yaml
   Copy-Item config/relaydeck-state.example.json config/relaydeck-state.json
   ```

   In normal use, the management panel generates these runtime files after
   providers and model routes are configured.

## Start and stop

Start all services:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

Start services individually:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-litellm.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-open-webui.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-admin-panel.ps1
```

Stop all services:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-llm-stack.ps1
```

Open the management panel at `http://127.0.0.1:8091`. Configure a supplier,
API profile, public model, and upstream binding there. Use network tests for
low-cost connectivity checks and conversation tests only when a real request
is needed.

## Provider adapters

Provider-specific quota scripts live in `quota-adapters/`. Start with
`quota-adapters/adapter_template.py` and follow
`docs/provider-quota-adapter-guide.md` when adding a new provider. Adapter
scripts must never contain credentials; read them from environment variables
or the managed profile passed by the application.

## Repository safety

The following are local runtime artifacts and must stay outside Git:

- `.env` and all environment backups
- `config/litellm.yaml` and `config/relaydeck-state.json`
- `data/`, `logs/`, `run/`, `tmp/`, and virtual environments
- SQLite databases, PID files, generated reports, and archive files

Before pushing a branch, inspect the staged file list and run a secret scanner:

```powershell
git status --short
git diff --cached --name-only
gitleaks detect --no-banner --redact
```

If a credential has ever been committed or shared, revoke it at the provider
and create a replacement before making the repository public.

## License

Choose and add a license before publishing to GitHub. If this is an academic
or private project, keep the repository private until the ownership and reuse
terms are clear.
