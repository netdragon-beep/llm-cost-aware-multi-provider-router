# RelayDeck Local 用量、成本与额度能力实施方案

状态：正式开发方案  
日期：2026-06-24  
适用范围：RelayDeck Local 本地管理面、LiteLLM 网关接入、路由配置管理态  
文档目标：在不修改本次代码的前提下，把“用量 / 成本 / 额度 / 性价比”能力整理为一份贴合当前项目现状的实施方案，作为后续开发的统一依据。

## 1. 结论摘要

基于当前代码现状，后续开发不应再把“模型路由双视图重构”当成 Phase 1 的主目标，因为这部分基础能力已经存在：

- `admin-panel/app.py` 已支持管理态 `suppliers / api_profiles / model_routes / route_bindings` 的读取、保存、兼容旧结构和生成 `litellm.yaml`。
- `admin-panel/static/index.html` 已有“按模型 / 按供应商”双视图入口，以及“先配供应商 API，再挂模型上游”的交互方向。
- `config/relaydeck-state.json` 已在真实数据中使用新管理态结构，并且 `api_profiles` 上已经预留了 `pricing / quota / accounting / usage` 扩展对象。

因此，这个能力的第一阶段重点应该调整为：

1. 补齐本地用量账本与手工成本数据。
2. 把现有测试请求变成可统计的 usage 事件。
3. 在当前控制面板中新增“用量与额度”区块，而不是重做整个路由页面。
4. 后续再接入 LiteLLM spend tracking 和余额适配器。

## 2. 当前代码现状

### 2.1 已有能力

当前项目已经具备以下基础：

- 管理态统一入口：
  - `GET /api/state`
  - `POST /api/save`
  - `POST /api/save-routing-draft`
- 路由配置核心结构：
  - `suppliers`
  - `api_profiles`
  - `model_routes`
  - `route_bindings`
- 旧结构兼容：
  - 可从扁平 `providers` 推导新管理态。
  - 可从新管理态回扁平 `providers` 用于生成 `litellm.yaml`。
- 路由配置体验基础：
  - 当前页面已经有“按模型 / 按供应商”切换。
  - 已支持 API profile 级别的探测与模型列表读取。
- 测试链路：
  - `POST /api/test/direct`
  - `POST /api/test/gateway`
  - `POST /api/check/providers-v2`
  - `POST /api/check/api-profiles`
  - `GET /api/models`

### 2.2 当前缺口

当前还没有以下能力：

- 没有独立的 usage 数据库存储。
- 测试请求返回结果不会落本地 usage 事件。
- 没有充值记录、余额快照、价格配置的正式读写接口。
- 没有“用量与额度”汇总接口。
- 没有基于 LiteLLM spend tracking 的真实业务流量接入。
- `api_check_providers` / `api_check_providers_v2` 中的 `token_cost` 仍是占位值。
- 没有正式的前端区块来展示：
  - 今日 / 本月 Token
  - Provider 成本对比
  - 额度进度
  - 充值记录
  - 风险告警

### 2.3 现状约束

后续方案必须接受以下现实约束：

- 当前路由配置的事实主源已经不是单纯的 `providers`，而是管理态四对象结构。
- `config/relaydeck-state.json` 中已经存在 `api_key_value`，说明密钥当前仍可能被状态文件持久化，安全方案需要收敛，但本次文档不要求改代码。
- 项目运行环境是本地 Windows，自托管、轻依赖、可恢复比复杂平台化能力更重要。
- 第一阶段如果强依赖 LiteLLM 数据库，会拖慢交付；因此应先做本地账本，再接 LiteLLM spend tracking。

## 3. 目标与非目标

### 3.1 目标

1. 在当前管理面中新增“用量与额度”能力，并直接复用现有管理态与页面结构。
2. 建立本地 usage 账本，先覆盖控制台发起的测试请求，再逐步接入 LiteLLM 真实流量。
3. 支持手工配置价格、手工录入人民币充值记录、手工或半自动记录额度余额。
4. 支持按 `api_profile` 和 `model_route` 归因成本，而不是继续围绕旧式扁平 provider 行做设计。
5. 形成明确的 phase、接口列表、数据结构、组件列表、验收标准。

### 3.2 非目标

1. 本方案第一版不要求重写当前路由配置架构。
2. 不做自动充值、自动购买额度、自动登陆第三方站点。
3. 不做按用户 / 组织 / 项目的完整账单系统。
4. 不默认保存 Prompt 正文和响应正文。
5. 不自动调度 benchmark，不自动改路由优先级。

## 4. 设计原则

### 4.1 以当前管理态为主

路由主数据继续以 `relaydeck-state.json` 的管理态为准，不新增第二套路由主源。

### 4.2 以 `api_profile` 为成本归因主维度

后续所有成本、充值、额度、余额快照默认都归因到 `api_profile_id`，必要时补充：

- `supplier_id`
- `model_route_id`
- `route_binding_id`

原因：

- 当前代码已经把连接信息、已知模型、供应商归属聚合在 `api_profiles`。
- 手工充值和额度通常对应的是某个账号或某个 key，而不是某条模型绑定。
- 同一个 API profile 可被多个模型路由复用，更符合真实账务关系。

### 4.3 先本地账本，后外部数据源

推荐数据源优先级：

1. LiteLLM 实际返回的 usage / cost
2. 控制台测试接口返回的 usage
3. 本地价格表估算
4. 人工充值记录与人工额度记录
5. Provider 余额接口或官网接口

### 4.4 最小侵入当前前端

前端优先新增区块和弹窗，不先推翻当前路由 UI。

### 4.5 避免过度建模

第一版不单独建 `model_prices` 历史表，价格配置优先放在现有 `api_profiles[].pricing` 中；只有当后续需要价格历史或多来源比对时，再拆出独立表。

## 5. 总体架构

采用三层结构：

1. 数据采集层
   - 控制台测试接口
   - LiteLLM spend tracking
   - 手工充值录入
   - 手工额度录入
   - Provider 余额适配器
2. 本地归一化存储层
   - `data/relaydeck/usage.db`
3. 管理面聚合展示层
   - 用量概览
   - 额度进度
   - 成本对比
   - 充值记录
   - 风险提示

## 6. Phase 规划

## 6.1 Phase 0：数据契约冻结

目标：把“后续实现依赖的字段边界”先固定下来，避免开发过程中前后端和状态结构反复变化。

交付：

- 确认 `api_profiles.pricing / quota / accounting / usage` 字段契约。
- 确认 usage SQLite 表结构。
- 确认新增接口列表与返回格式。
- 确认归因主键优先级：
  - `api_profile_id`
  - `route_binding_id`
  - `model_route_id`
  - `supplier_id`

说明：

- 该阶段不需要调整当前路由保存方式。
- 继续沿用 `GET /api/state` + `POST /api/save` 的聚合保存模式。

## 6.2 Phase 1：本地用量账本与手工成本

目标：在不依赖 LiteLLM 数据库的前提下，先让控制台自己的测试流量可统计、可聚合、可展示。

交付：

- 新增本地 SQLite：`data/relaydeck/usage.db`
- 新增 usage 事件落库能力：
  - `POST /api/test/direct`
  - `POST /api/test/gateway`
- 新增价格配置读写能力，优先写回 `api_profiles[].pricing`
- 新增充值记录 CRUD
- 新增手工额度记录 / 快照写入
- 新增“用量与额度”前端区块

本阶段不做：

- 不接 LiteLLM 全局 spend tracking
- 不做自动余额拉取
- 不做复杂评分模型

## 6.3 Phase 2：聚合接口与页面落地

目标：让前端能稳定读取汇总视图，而不是在页面端拼装明细。

交付：

- 新增用量汇总接口
- 新增 Provider 对比接口
- 新增最近事件接口
- 新增风险告警聚合逻辑
- 前端补齐概览卡、进度条、对比表、充值入口、事件列表

说明：

- 路由页双视图继续沿用现有结构，只新增“价格 / 额度 / 充值摘要”展示位。

## 6.4 Phase 3：接入 LiteLLM spend tracking

目标：把 Open WebUI、Codex、Continue 等经 LiteLLM 产生的真实业务流量纳入同一套 usage 看板。

交付：

- 启动脚本支持 LiteLLM spend tracking 所需配置
- 管理面新增 LiteLLM usage 拉取 / 同步逻辑
- usage 事件支持来源 `litellm`
- 聚合接口优先使用 `actual_cost`

风险：

- LiteLLM 当前版本能力、字段完整度、Windows 启动方式要实测
- fallback 之后的命中 Provider 归因可能不完整，需要兼容降级

## 6.5 Phase 4：额度适配器

目标：从“只支持手工额度”升级到“手工额度 + 可选自动余额”。

交付：

- `manual` adapter
- `openai_compatible_balance` adapter
- 余额刷新接口
- 额度预警与预检提示

说明：

- 适配器失败不影响现有模型路由保存和使用。

## 6.6 Phase 5：性价比对比与建议

目标：把“统计数据”变成“可决策信息”。

交付：

- 同模型不同 Provider 对比表
- 每人民币 Token 产出
- 每百万 Token 实际人民币成本
- 价格异常、余额不足、成功率偏低、延迟偏高提示

说明：

- 第一版只提供建议，不自动改 `priority`。

## 7. 数据结构设计

### 7.1 管理态主数据

继续沿用当前管理态：

```json
{
  "routing_view_mode": "by_model",
  "suppliers": [],
  "api_profiles": [],
  "model_routes": [],
  "route_bindings": [],
  "router_settings": {},
  "litellm_settings": {},
  "providers": []
}
```

其中：

| 对象 | 当前状态 | 后续角色 |
| --- | --- | --- |
| `suppliers` | 已存在 | 供应商逻辑分组 |
| `api_profiles` | 已存在 | 成本、充值、额度归因主对象 |
| `model_routes` | 已存在 | 面向客户端的公共模型名 |
| `route_bindings` | 已存在 | 路由绑定与优先级 |
| `providers` | 兼容视图 | 仅作为扁平导出与 LiteLLM 生成中间层 |

### 7.2 `api_profiles` 扩展字段

当前状态文件中 `api_profiles` 已经带有空的 `pricing / quota / accounting / usage` 对象。后续正式约定如下：

```json
{
  "pricing": {
    "input_per_1m": 0,
    "output_per_1m": 0,
    "currency": "USD",
    "source": "manual",
    "updated_at": ""
  },
  "quota": {
    "enabled": false,
    "adapter": "manual",
    "currency": "CNY",
    "period": "monthly",
    "limit": 0,
    "manual_balance_total": 0,
    "manual_balance_remaining": 0,
    "warning_threshold_percent": 80,
    "critical_threshold_percent": 95,
    "last_checked_at": ""
  },
  "accounting": {
    "base_currency": "CNY",
    "manual_recharge_enabled": true,
    "actual_cost_source": "recharge_records"
  },
  "usage": {
    "track_enabled": true,
    "store_prompt": false,
    "store_response": false
  }
}
```

设计要求：

- 缺省字段必须自动补默认值。
- 未知字段必须保留，避免覆盖后续扩展。
- 第一版不把价格配置拆成独立配置文件。

### 7.3 本地 SQLite

路径：

```text
data/relaydeck/usage.db
```

推荐第一版只建 3 张主表。

#### 7.3.1 `usage_events`

用途：记录一次测试请求或一次 LiteLLM usage 事件。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | text | UUID |
| `created_at` | text | ISO 时间 |
| `source` | text | `admin_test_direct` / `admin_test_gateway` / `litellm` |
| `request_id` | text | 上游或 LiteLLM request id |
| `supplier_id` | text | 归属供应商 |
| `api_profile_id` | text | 归属 API profile |
| `model_route_id` | text | 归属模型路由 |
| `route_binding_id` | text | 命中绑定，未知可为空 |
| `public_model_name` | text | 公共模型名 |
| `upstream_model` | text | 上游模型 |
| `custom_llm_provider` | text | `openai` / `anthropic` |
| `prompt_tokens` | integer | 输入 token |
| `completion_tokens` | integer | 输出 token |
| `total_tokens` | integer | 总 token |
| `estimated_cost` | real | 本地估算成本 |
| `actual_cost` | real | 实际成本 |
| `currency` | text | 默认 `USD` |
| `latency_ms` | integer | 请求耗时 |
| `status` | text | `success` / `error` |
| `error_code` | text | 错误分类 |
| `raw_source_ref` | text | 原始来源标识，可为空 |

索引建议：

- `idx_usage_events_created_at`
- `idx_usage_events_api_profile_id_created_at`
- `idx_usage_events_model_route_id_created_at`

#### 7.3.2 `recharge_records`

用途：记录真实人民币充值。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | text | UUID |
| `created_at` | text | 创建时间 |
| `paid_at` | text | 实际支付时间 |
| `supplier_id` | text | 供应商 ID |
| `api_profile_id` | text | API profile ID |
| `paid_amount_cny` | real | 实际支付人民币 |
| `credited_amount` | real | 到账额度 |
| `credited_currency` | text | 到账单位 |
| `bonus_amount` | real | 赠送额度 |
| `bonus_currency` | text | 赠送单位 |
| `balance_after_recharge` | real | 充值后余额，可空 |
| `record_type` | text | `paid` / `trial` / `bonus` |
| `note` | text | 备注 |

索引建议：

- `idx_recharge_records_api_profile_id_paid_at`

#### 7.3.3 `provider_balance_snapshots`

用途：记录手工或自动的额度 / 余额快照。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | text | UUID |
| `checked_at` | text | 检测时间 |
| `supplier_id` | text | 供应商 ID |
| `api_profile_id` | text | API profile ID |
| `adapter` | text | `manual` / `provider_api` |
| `status` | text | `ok` / `unsupported` / `auth_failed` / `rate_limited` / `error` |
| `currency` | text | 币种 |
| `balance_total` | real | 总额度 |
| `balance_used` | real | 已用额度 |
| `balance_remaining` | real | 剩余额度 |
| `period_start` | text | 周期开始 |
| `period_end` | text | 周期结束 |
| `raw_summary` | text | 脱敏摘要 |

第一版明确不建：

- 不建 Prompt 正文表
- 不建响应正文表
- 不建价格历史表

## 8. 成本与额度计算规则

### 8.1 单次请求估算成本

```text
input_cost = prompt_tokens / 1_000_000 * input_per_1m
output_cost = completion_tokens / 1_000_000 * output_per_1m
estimated_cost = input_cost + output_cost
```

优先级：

1. 有 `actual_cost` 时，汇总成本优先取 `actual_cost`
2. 无 `actual_cost` 时，回退到 `estimated_cost`

### 8.2 基于充值记录的真实人民币成本

聚合层默认输出：

```text
effective_cny_per_1m_tokens = total_paid_cny / total_tokens_used * 1_000_000
effective_tokens_per_cny = total_tokens_used / total_paid_cny
```

说明：

- `paid_amount_cny` 才是实际成本基准。
- `credited_amount` 和 `bonus_amount` 只用于解释中转站扣费模式。
- 若一个 API profile 有多次充值，第一版按累计池模型计算，不做复杂 FIFO / LIFO 成本拆分。

### 8.3 额度进度

```text
used_percent = balance_used / balance_total * 100
```

状态分级：

- `< 80%`：正常
- `80% - 95%`：警告
- `>= 95%`：危险
- 无总额度：显示“仅本地估算”

## 9. 接口列表

### 9.1 继续复用的现有接口

| 接口 | 当前状态 | 用途 |
| --- | --- | --- |
| `GET /api/state` | 已有 | 页面初始化主数据 |
| `POST /api/save` | 已有 | 保存全量配置与重启 |
| `POST /api/save-routing-draft` | 已有 | 保存路由草稿 |
| `POST /api/test/direct` | 已有 | 直连测试，后续要接 usage 落库 |
| `POST /api/test/gateway` | 已有 | 网关测试，后续要接 usage 落库 |
| `GET /api/models` | 已有 | 读取 LiteLLM 模型列表 |
| `POST /api/check/providers-v2` | 已有 | 旧扁平 provider 视角检查 |
| `POST /api/check/api-profiles` | 已有 | API profile 视角检查 |

### 9.2 第一阶段新增接口

推荐新增以下聚合接口，避免一开始把配置保存风格改成细粒度 REST。

| 接口 | 方法 | 用途 |
| --- | --- | --- |
| `/api/usage/summary` | `GET` | 概览卡与顶部指标 |
| `/api/usage/events` | `GET` | 最近 usage 事件 |
| `/api/usage/provider-comparison` | `GET` | 同模型 Provider 对比 |
| `/api/recharges` | `GET/POST/PATCH/DELETE` | 充值记录管理 |
| `/api/pricing` | `GET/POST` | 价格配置读写 |
| `/api/usage/refresh-balances` | `POST` | 刷新余额快照 |

### 9.3 接口返回建议

#### `GET /api/usage/summary?range=24h`

```json
{
  "range": "24h",
  "total_tokens": 0,
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "estimated_cost": 0,
  "actual_cost": 0,
  "cost_currency": "USD",
  "real_recharge_cost_cny": 0,
  "effective_cny_per_1m_tokens": 0,
  "effective_tokens_per_cny": 0,
  "by_provider": [],
  "by_model": [],
  "quota_alerts": []
}
```

#### `GET /api/usage/provider-comparison?model_name=gpt-5.5&range=7d`

```json
{
  "model_name": "gpt-5.5",
  "range": "7d",
  "items": [
    {
      "supplier_id": "",
      "api_profile_id": "",
      "supplier_name": "",
      "api_profile_label": "",
      "actual_cost_cny_per_1m": 0,
      "tokens_per_cny": 0,
      "estimated_cost_usd": 0,
      "success_rate": 0,
      "p95_latency_ms": 0,
      "balance_status": "unknown",
      "recommendation": ""
    }
  ]
}
```

#### `POST /api/recharges`

```json
{
  "api_profile_id": "api_supplier-owl-openai-https-api-cdn-owlai-tech-owl-api-key",
  "paid_at": "2026-06-24T10:00:00+08:00",
  "paid_amount_cny": 100,
  "credited_amount": 120,
  "credited_currency": "CREDIT",
  "bonus_amount": 20,
  "bonus_currency": "CREDIT",
  "balance_after_recharge": 120,
  "record_type": "paid",
  "note": "活动充值 100 送 20"
}
```

### 9.4 为什么第一阶段不改成全资源 REST 配置接口

不推荐在第一阶段新增：

- `GET /api/suppliers`
- `POST /api/suppliers`
- `GET /api/api-profiles`
- `POST /api/model-routes`

原因：

- 当前前端和后端已经围绕 `GET /api/state` + `POST /api/save` 打通。
- 先引入 usage / recharge / balance 相关聚合接口，风险更低。
- 等“用量与额度”能力稳定后，再决定是否把配置接口细化。

## 10. 前端组件设计

## 10.1 页面位置

新增一个独立区块：`用量与额度`

建议位置：

- 放在“预检”之后
- 放在“模型路由”之前

原因：

- 它既依赖配置，又服务于配置决策。
- 用户先看风险和成本，再决定要不要改路由。

## 10.2 组件列表

### 1. 概览卡 `UsageSummaryCards`

展示：

- 今日 Token
- 今日成本
- 本月累计人民币消耗
- 每人民币 Token 产出
- 风险项数量

### 2. 额度进度列表 `QuotaProgressPanel`

按 `api_profile` 展示：

- 供应商名
- API profile 标签
- 当前额度 / 剩余额度
- 累计充值人民币
- 最近检测时间
- 风险状态

### 3. Provider 对比表 `ProviderComparisonTable`

按模型名过滤或分组展示：

- 供应商
- API 标签
- 每人民币 Token
- 每百万 Token 实际人民币成本
- 标价输入 / 输出
- 近 7 天成功率
- P95 延迟
- 余额状态
- 建议标签

### 4. 充值记录弹窗 `RechargeRecordDialog`

动作：

- 新增记录
- 编辑最近记录
- 删除记录

展示：

- 累计充值人民币
- 估算真实性价比

### 5. 最近事件列表 `UsageEventTable`

展示：

- 时间
- 来源
- 模型
- Provider
- Token
- 成本
- 耗时
- 状态

### 6. 路由页增强位

不是重写路由页，而是增强现有卡片：

- `api_profile` 卡片显示价格 / 额度 / 充值摘要
- `route_binding` 卡片显示最近 24 小时用量与单次测试成本

## 10.3 前端实现约束

- 保持当前“按模型 / 按供应商”双视图结构不动。
- 不在事件列表中显示 Prompt 和响应正文。
- 充值删除必须二次确认。
- 当没有 usage 数据时，空状态要明确提示“尚无测试或同步数据”。

## 11. 验收标准

### 11.1 Phase 1 验收

1. 新增 `data/relaydeck/usage.db`。
2. `POST /api/test/direct` 成功返回 usage 时，可写入 `usage_events`。
3. `POST /api/test/gateway` 成功返回 usage 时，可写入 `usage_events`。
4. 可为 `api_profile` 配置输入 / 输出单价。
5. 可为 `api_profile` 新增、编辑、删除充值记录。
6. 管理面能展示今日 / 本月 Token 与成本概览。
7. 管理面能展示按 `api_profile` 聚合的累计充值人民币与每人民币 Token 产出。
8. 默认不持久化 Prompt 和响应正文。

### 11.2 Phase 2 验收

1. 新增概览卡、额度进度、对比表、事件列表四类前端组件。
2. 路由页继续保持“按模型 / 按供应商”双视图可用。
3. `api_profile` 卡片可展示价格 / 额度 / 充值摘要。
4. 当无数据、无价格、无充值记录、无额度快照时，页面能正确显示空状态或缺失提示。

### 11.3 Phase 3 验收

1. LiteLLM 真实请求可以进入 usage 统计。
2. 汇总成本优先使用 `actual_cost`。
3. 同模型不同 Provider 的成本和成功率对比可见。

### 11.4 Phase 4 验收

1. 至少支持 `manual` 额度适配器。
2. 刷新余额失败不会影响路由保存和 LiteLLM 生成。
3. 额度接近阈值时，管理面有显式提示。

## 12. 风险与待确认事项

### 12.1 风险

1. 当前状态文件可能持久化密钥，后续安全收口需要单独排期。
2. 个别现有配置数据存在乱码或历史脏值，新增解析逻辑要容错。
3. LiteLLM fallback 后的命中绑定归因未必完整，需要接受部分事件只有 `model_route_id`、没有 `route_binding_id`。
4. 不同中转站余额接口差异大，自动化适配必须允许 `unsupported` 降级。
5. 流式响应不一定稳定返回 usage，事件落库要兼容空 usage。

### 12.2 待确认

1. 第一批必须支持的余额适配器名单。
2. 是否接受在第一阶段仅支持“累计池”方式计算真实人民币成本。
3. 是否需要在汇总中区分客户端来源，例如 Open WebUI、Codex、Continue。
4. 是否把价格主币种统一为 `USD`，而把真实性价比统一为 `CNY`。

## 13. 推荐落地顺序

推荐顺序如下：

1. 先冻结字段与 SQLite 结构。
2. 给测试接口补 usage 落库。
3. 做价格配置与充值记录接口。
4. 做用量汇总接口和前端区块。
5. 再接 LiteLLM spend tracking。
6. 最后补余额适配器和性价比建议。

这样做的好处是：

- 贴合当前代码，不重复做已完成的路由重构。
- 先交付能看见的价值。
- 风险集中在新增能力，不扰动现有路由主流程。
- 后续接 LiteLLM 和余额适配器时，数据模型已经稳定。

## 14. 本文档对应的开发边界

本文档明确约束后续实现：

- 路由主数据继续使用当前管理态结构。
- 第一阶段不重写配置保存模式。
- 第一阶段不要求变更当前前后端主架构。
- 新开发应优先围绕 usage / recharge / balance 三类增量能力展开。

这也是当前项目最符合代码现状、风险最小、收益最高的实施路线。
