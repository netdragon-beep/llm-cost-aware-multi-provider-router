# 供应商真实成本归因与网关用量设计

## 目标

第二阶段把第一阶段的价格版本和供应商共享额度连接到真实 LiteLLM 请求。系统记录请求命中的 API、公共模型、上游模型、路由绑定、价格版本、Token、耗时和状态，但不保存 Prompt 或回复正文。

统计必须区分两种现金支出：

- 按量充值：多次充值按实际支付人民币除以到账额度与赠送额度的总和，形成供应商额度池的加权平均人民币成本。
- 订阅套餐：实际支付人民币在套餐有效日期内按自然日摊销；每天的摊销成本按当日请求的价格成本权重分配。没有价格时回退到 Token 权重。

不同价格版本的用量不合并计算单价。汇总可以相加总成本，但必须保留每条请求绑定的 `pricing_version_id`。

## 数据模型

`recharge_records` 扩展为供应商成本批次，新增：

- `billing_type`: `topup` 或 `subscription`。
- `service_start` / `service_end`: 订阅有效期，首尾日期均计费。
- `allocation_mode`: 按量充值固定为 `weighted-credit`，订阅固定为 `daily-amortized`。

旧充值记录迁移后默认为 `topup`，继续按供应商共享，不再按单个 API 重复汇总。

`usage_events` 新增：

- `platform_key`
- `api_profile_id`
- `model_route_id`
- `route_binding_id`
- `pricing_version_id`
- `cash_cost_cny`
- `cost_attribution_status`

数据库初始化使用幂等列迁移，兼容现有 `usage.db`。

## 请求归因

生成 LiteLLM 配置时，把内部标识写入每个 deployment 的 `model_info`。自定义 LiteLLM 回调从最终命中的 deployment 读取标识，并执行：

1. 提取请求 ID、真实上游模型、Token、响应成本、耗时和状态。
2. 以 API Profile 和上游模型查找请求时刻有效的价格版本。
3. 使用该版本计算 `estimated_cost`，并保存 `pricing_version_id`。
4. 对按量充值按供应商加权额度成本换算 `cash_cost_cny`。
5. 订阅日摊销在统计时动态分配，避免当天新增请求后历史记录比例失真。

同一来源和请求 ID 只写入一次。回调异常只写日志，不能阻断模型响应。

## 统计口径

供应商统计返回：

- `gateway_requests`: 真实网关请求数。
- `priced_requests`: 已绑定价格版本的请求数。
- `pricing_coverage`: 价格绑定覆盖率。
- `topup_cash_cost_cny`: 按量充值归因成本。
- `subscription_amortized_cny`: 查询周期内订阅摊销成本。
- `attributed_cash_cost_cny`: 两者合计。
- `unattributed_requests`: 因缺少价格或成本批次无法归因的请求数。

订阅没有请求的日期仍计入供应商期间支出，但不伪造请求级成本。界面明确区分“已摊销但无请求”和“待归因”。

## 界面

供应商面板新增默认收起的“实际支出与成本归因”：

- 新增成本批次时选择“按量充值”或“订阅套餐”。
- 按量充值填写支付金额、到账额度、赠送额度和单位。
- 订阅填写支付金额、服务开始和结束日期。
- 显示加权额度成本、当前日摊销、已归因现金成本、价格覆盖率和最近请求。

现有“真实余额校准”继续只负责额度轮次，不参与现金成本，除非同时填写了正数支付金额和到账额度。

## 安全与失败处理

- 不保存 API Key、Cookie、Token、Prompt 或回复正文。
- 找不到绑定时仍记录安全的请求摘要，状态标记为 `missing-binding`。
- 找不到价格时记录 Token，状态标记为 `missing-price`。
- 找不到匹配成本批次时状态标记为 `missing-cost-basis`。
- 回调写库失败不得影响 LiteLLM 返回。

## 验证

测试覆盖幂等迁移、加权成本、订阅跨日摊销、价格版本有效期、请求去重、回调字段提取、配置注入、接口校验和前端空状态。最后运行全部 Python/Node 测试、Python 编译、内联 JavaScript 语法、DOM ID、清单 JSON 和浏览器核验。
