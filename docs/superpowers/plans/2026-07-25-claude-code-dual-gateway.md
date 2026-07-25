# Claude Code Dual Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Claude Code gateway on port 4101 that exposes discoverable aliases while retaining the existing native OpenAI/Codex gateway on port 4100.

**Architecture:** The existing management state remains the only source of routing truth. Config generation produces native groups for the existing gateway and alias-only groups for the Claude gateway, with matching deployments and fallback chains. The management panel writes Claude Code's JSON settings by merging the three required environment variables after creating a backup.

**Tech Stack:** FastAPI, Python, LiteLLM YAML configuration, PowerShell service scripts, vanilla JavaScript, `unittest`.

---

### Task 1: Generate Claude Alias Routing Configuration

**Files:**
- Modify: `admin-panel/app.py:50-55`
- Modify: `admin-panel/app.py:3968-4088`
- Modify: `admin-panel/tests/test_claude_model_discovery.py`

- [ ] **Step 1: Write failing tests for alias-only configuration generation**

```python
def test_claude_gateway_config_uses_aliases_without_native_groups(self):
    configs = build_litellm_gateway_configs_from_providers(self.providers, {}, {})
    self.assertEqual(
        [row["model_name"] for row in configs["claude"]["model_list"]],
        ["claude-relaydeck-gpt-5-6-sol"],
    )
    self.assertEqual(
        [row["model_name"] for row in configs["openai"]["model_list"]],
        ["gpt-5.6-sol"],
    )

def test_claude_alias_has_matching_fallbacks_and_upstream_parameters(self):
    configs = build_litellm_gateway_configs_from_providers(self.providers_with_backup, {}, {})
    native = configs["openai"]
    claude = configs["claude"]
    self.assertEqual(
        claude["router_settings"]["fallbacks"],
        [{"claude-relaydeck-gpt-5-6-sol": ["claude-relaydeck-gpt-5-6-sol__2__backup"]}],
    )
    self.assertEqual(
        claude["model_list"][0]["litellm_params"],
        native["model_list"][0]["litellm_params"],
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `D:/conda/envs/llm-stack-local/python.exe -m unittest admin-panel/tests/test_claude_model_discovery.py -v`

Expected: FAIL because `build_litellm_gateway_configs_from_providers` is undefined.

- [ ] **Step 3: Add a pure alias helper and a dual-config generator**

```python
def claude_gateway_model_alias(public_model_name: str) -> str:
    return f"claude-relaydeck-{slugify(public_model_name)}"

def build_litellm_gateway_configs_from_providers(...):
    native = build_litellm_config_from_providers(..., model_name_transform=lambda name: name)
    claude = build_litellm_config_from_providers(..., model_name_transform=claude_gateway_model_alias)
    return {"openai": native, "claude": claude}
```

Refactor `build_litellm_config_from_providers` so the model-group name transform applies to each primary and backup group and its fallback chain, while preserving deployment parameters and metadata.

- [ ] **Step 4: Persist both generated YAML configurations**

```python
CONFIG_PATH = ROOT / "config" / "litellm.yaml"
CLAUDE_CONFIG_PATH = ROOT / "config" / "litellm-claude.yaml"

def save_gateway_configs(configs: dict[str, dict[str, Any]]) -> None:
    save_yaml_config(CONFIG_PATH, configs["openai"])
    save_yaml_config(CLAUDE_CONFIG_PATH, configs["claude"])
```

Replace existing write sites that persist the generated route configuration so they call `save_gateway_configs`.

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `D:/conda/envs/llm-stack-local/python.exe -m unittest admin-panel/tests/test_claude_model_discovery.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the routing generator**

```powershell
git add admin-panel/app.py admin-panel/tests/test_claude_model_discovery.py config/litellm-claude.yaml.example
git commit -m "feat: generate dedicated Claude gateway routes"
```

### Task 2: Manage the Dedicated Claude LiteLLM Process

**Files:**
- Modify: `.env.example`
- Modify: `scripts/start-litellm.ps1`
- Modify: `scripts/stop-llm-stack.ps1`
- Modify: `scripts/start-all.ps1`
- Modify: `README.md`

- [ ] **Step 1: Write a failing PowerShell validation test or command fixture**

Create `scripts/tests/test-dual-litellm.ps1` with assertions that `Start-LiteLLMInstance` receives `litellm`, `4100`, and `config/litellm.yaml`, then `litellm-claude`, `4101`, and `config/litellm-claude.yaml`.

- [ ] **Step 2: Run the validation to verify it fails**

Run: `powershell -ExecutionPolicy Bypass -File scripts/tests/test-dual-litellm.ps1`

Expected: FAIL because `Start-LiteLLMInstance` does not exist.

- [ ] **Step 3: Start both isolated LiteLLM instances from the existing script**

```powershell
$openAiPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudePort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")

Start-LiteLLMInstance -ServiceName "litellm" -ConfigPath "$root/config/litellm.yaml" -Port $openAiPort
Start-LiteLLMInstance -ServiceName "litellm-claude" -ConfigPath "$root/config/litellm-claude.yaml" -Port $claudePort
```

The helper must use separate PID files and log files, reject occupied unmanaged ports, and make the existing OpenAI readiness endpoint pass before returning.

- [ ] **Step 4: Extend stop/start-all and defaults**

Add `CLAUDE_LITELLM_PORT=4101` to `.env.example`; stop both service names in `stop-llm-stack.ps1`; start the paired gateway script once from `start-all.ps1`; document the two endpoints in `README.md`.

- [ ] **Step 5: Run the PowerShell validation to verify it passes**

Run: `powershell -ExecutionPolicy Bypass -File scripts/tests/test-dual-litellm.ps1`

Expected: PASS.

- [ ] **Step 6: Commit the dual-process lifecycle**

```powershell
git add .env.example scripts README.md
git commit -m "feat: run isolated Claude Code gateway"
```

### Task 3: Write Claude Code Discovery Settings Safely

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `admin-panel/tests/test_claude_model_discovery.py`
- Modify: `admin-panel/static/index.html`

- [ ] **Step 1: Write a failing JSON merge test**

```python
def test_claude_code_settings_merge_preserves_existing_values_and_sets_discovery(self):
    with TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text('{"theme":"dark","env":{"KEEP":"yes"}}', encoding="utf-8")
        result = configure_claude_code_settings(settings_path, "http://127.0.0.1:4101", "sk-test")
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(saved["env"]["KEEP"], "yes")
        self.assertEqual(saved["env"]["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertTrue(result["backup_path"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:/conda/envs/llm-stack-local/python.exe -m unittest admin-panel/tests/test_claude_model_discovery.py -v`

Expected: FAIL because `configure_claude_code_settings` is undefined.

- [ ] **Step 3: Implement merge, backup, and API endpoint**

```python
def configure_claude_code_settings(settings_path: Path, base_url: str, auth_token: str) -> dict[str, str]:
    existing = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    backup_file(settings_path)
    env = ensure_mapping(existing.get("env"))
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    })
    existing["env"] = env
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"settings_path": str(settings_path), "backup_path": str(backup_path)}
```

Expose a POST endpoint that resolves the local user settings path, reads the already-stored RelayDeck master key without returning it, and calls this helper. Reject malformed existing JSON without overwriting it.

- [ ] **Step 4: Add a focused panel action**

Add a `配置 Claude Code 自动发现` command next to the existing client access configuration. It must call the endpoint, show the backup path on success, and state that Claude Code must be restarted. Do not expose the gateway key in the UI response.

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `D:/conda/envs/llm-stack-local/python.exe -m unittest admin-panel/tests/test_claude_model_discovery.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the settings integration**

```powershell
git add admin-panel/app.py admin-panel/static/index.html admin-panel/tests/test_claude_model_discovery.py
git commit -m "feat: configure Claude Code gateway discovery"
```

### Task 4: End-to-End Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Start both gateways and verify their model lists**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-litellm.ps1
Invoke-RestMethod -Headers @{ Authorization = "Bearer $env:LITELLM_MASTER_KEY" } -Uri http://127.0.0.1:4100/v1/models
Invoke-RestMethod -Headers @{ Authorization = "Bearer $env:LITELLM_MASTER_KEY" } -Uri http://127.0.0.1:4101/v1/models
```

Expected: `4100` has native IDs only; `4101` has only `claude-relaydeck-*` IDs.

- [ ] **Step 2: Verify a Claude alias routes to the same model group**

Run an Anthropic Messages request through `4101/v1/messages` using one discovered alias and an OpenAI request through `4100/v1/chat/completions` using its native public model. Confirm both return a response and record the expected route metadata.

- [ ] **Step 3: Run the full automated suite**

Run:

```powershell
D:/conda/envs/llm-stack-local/python.exe -m unittest discover -s admin-panel/tests -v
node --test admin-panel/tests/*.test.js
```

Expected: all Python and JavaScript tests pass.

- [ ] **Step 4: Update usage documentation and commit**

Document the port split, one-click Claude Code setup, `/model` restart requirement, and alias use in subagent frontmatter. Commit the final documentation and verification changes.

