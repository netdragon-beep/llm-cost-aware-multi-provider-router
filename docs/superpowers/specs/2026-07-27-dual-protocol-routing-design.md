# 双协议模型路由设计

## 目标

让同一个公共模型路由可同时服务 OpenAI 兼容客户端和 Anthropic 兼容客户端，同时保持每个供应商 API 分组的真实上游协议独立可配。

## 边界

模型家族只描述模型的产品分类、展示与 Anthropic 客户端的默认 tier，不决定供应商 API 的请求协议。每一个 API 分组继续保存 `custom_llm_provider`，其值为 `openai` 或 `anthropic`；该字段是 LiteLLM 向上游发起请求时唯一的协议依据。

## 请求路径

```text
OpenAI 兼容客户端 / Codex
  -> 4100 OpenAI 网关
  -> 公共模型路由
  -> 按 API 分组 custom_llm_provider 调用上游

Anthropic 兼容客户端 / Claude Code
  -> 4101 Anthropic 入口
  -> 内部 4102 LiteLLM 路由（使用 Anthropic 客户端别名）
  -> 同一条公共模型的 API 分组
  -> 按 API 分组 custom_llm_provider 调用上游
```

4100 与 4102 均由 LiteLLM 生成配置。两者的上游 `litellm_params` 必须保持一致，区别仅是模型别名：4100 使用公共模型名，4102 使用 Claude 客户端可发现的别名。LiteLLM 负责客户端协议与上游协议之间的转换；RelayDeck 不根据模型家族覆写 API 分组协议。

## 行为规则

1. Claude 系列模型可以绑定 `openai` 或 `anthropic` 上游 API。
2. GPT、DeepSeek 等非 Claude 系列模型也可以通过 4101 的 Anthropic 兼容入口访问。
3. 路由优先级和回退链在两个客户端入口中保持相同顺序。
4. 管理台须显示“模型家族”“客户端兼容入口”和“上游协议”是三项独立信息，避免用户把模型类别误配为供应商协议。
5. 网络检查按 API 分组协议探测；`anthropic` 使用 `/v1/models`，`openai` 使用 `/models`。404 必须提示协议与上游不匹配，而不是建议更改模型家族。

## 验收

- Claude 系列绑定到 OpenAI 上游时，从 4100 发起请求不再被当作 Anthropic 上游请求。
- 相同绑定通过 4101 的 Anthropic 客户端别名可被发现并转发。
- Anthropic 上游和 OpenAI 上游在同一公共模型的优先级和 fallback 顺序在两个网关中一致。
- 管理台可明确看见每个 API 分组的上游协议及其与模型家族的独立性。
