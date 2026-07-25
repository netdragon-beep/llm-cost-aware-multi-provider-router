# Claude Code Dual Gateway Design

## Goal

RelayDeck will expose the same published public-model routes through two local
LiteLLM gateway surfaces without changing the existing Codex/OpenAI contract.

- OpenAI gateway remains on port `4100` and exposes native public model IDs.
- Claude Code gateway runs on port `4101` and exposes only
  `claude-relaydeck-<native-model-id>` aliases.
- Both surfaces route to the same upstream APIs, priorities, fallback chains,
  health state, quota state, and cost attribution metadata.

## Routing

For every `gateway_enabled` public model that has at least one enabled route,
the generated configuration contains two independent LiteLLM model groups:

```text
gpt-5.6-sol                         OpenAI/Codex gateway
claude-relaydeck-gpt-5-6-sol         Claude Code gateway
```

Each group has equivalent deployments and fallback order. The Claude alias is
an API-facing group name only; it does not replace or rename the native public
model.

## Configuration Generation

One shared RelayDeck management state produces two generated files:

- `config/litellm.yaml` for the OpenAI gateway.
- `config/litellm-claude.yaml` for the Claude Code gateway.

The OpenAI configuration contains native groups only. The Claude configuration
contains aliases only. This prevents Claude Code discovery from receiving
native model names that it filters out and prevents OpenAI clients from seeing
Claude-specific aliases.

## Services

The existing LiteLLM service remains the OpenAI gateway on `LITELLM_PORT`
(default `4100`). A second managed LiteLLM process runs the Claude configuration
on `CLAUDE_LITELLM_PORT` (default `4101`). Start, stop, status, and reload
operations treat both processes as one gateway service.

## Claude Code Setup

RelayDeck will offer a single action that merges these values into the user
Claude Code settings file while preserving unrelated settings and creating a
timestamped backup:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4101",
    "ANTHROPIC_AUTH_TOKEN": "<RelayDeck gateway key>",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"
  }
}
```

It intentionally does not set `ANTHROPIC_MODEL`: users select any discovered
Claude alias in `/model`, and subagent definitions can set their own alias.

## Validation

Automated tests must prove that:

1. Native and Claude configurations contain mutually exclusive group names.
2. A Claude alias has the same upstream deployment parameters and fallback
   order as its native public-model route.
3. Unpublished or disabled model groups are absent from both configurations.
4. Claude Code settings merging preserves existing keys and creates the three
   discovery environment values.
5. The two service configurations use the expected separate ports.

## Constraints

- No existing Codex/OpenAI endpoint or native model ID changes.
- No secrets are committed; configuration continues to reference environment
  variable names.
- Claude aliases must start with `claude-` so Claude Code gateway discovery
  accepts them.
