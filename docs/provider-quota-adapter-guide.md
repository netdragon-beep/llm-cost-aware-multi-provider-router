# RelayDeck 供应商额度适配器指南

## 1. 设计原则

每个供应商网站拥有独立的认证方式、额度接口和返回结构，因此 RelayDeck
不提供可由用户随意切换的通用“额度获取方式”。系统根据供应商域名自动绑定
唯一的额度适配器：

- `lingsuan.top` 自动使用内置 `lingsuan-web`。
- `auto-code.net` 自动使用内置 `autocode-web`。
- 其他供应商通过脚本清单中的 `supported_hosts` 自动绑定。
- 没有匹配项时显示“暂未安装额度适配器”，不会盲目探测通用账单接口。

同一供应商域名下的全部 API 共用一份额度快照和登录凭据。凭据由 Windows
DPAPI CurrentUser 加密保存，不写入普通项目配置。

## 2. 文件结构

在 [`quota-adapters/`](../quota-adapters/) 中创建同名脚本和清单：

```text
adapter_supplier_x.py
adapter_supplier_x.adapter.json
```

## Browser SSO adapter contract

Supported website suppliers share one user-facing login flow: RelayDeck opens
an isolated Edge or Chrome profile, the user completes the supplier login,
and the adapter identifies a successful same-origin session. RelayDeck then
stores only the supplier token or cookie through Windows DPAPI CurrentUser.

Add this optional manifest section when a custom quota adapter supports the
standard browser flow:

```json
{
  "browser_sso": {
    "portal_hosts": ["supplier.example"],
    "login_path": "/dashboard",
    "auth_me_path": "/api/v1/auth/me",
    "cookie_domains": ["supplier.example"],
    "google_rejection_check": true
  }
}
```

The browser driver validates HTTPS and exact approved hosts before capturing
anything. It accepts a successful response from `auth_me_path`, extracts a
Bearer token from that request when present, and keeps only cookies whose
domain is listed in `cookie_domains`. The quota script remains responsible
for parsing balance, subscription, recharge, and pricing data.

支持 `.py`、`.ps1`、`.cmd` 和 `.bat`，推荐使用 Python。可以直接复制：

- [`adapter_template.py`](../quota-adapters/adapter_template.py)
- [`adapter_template.adapter.json`](../quota-adapters/adapter_template.adapter.json)

## 3. 清单格式

```json
{
  "version": 1,
  "id": "supplier-x",
  "display_name": "Supplier X 网站额度",
  "supported_hosts": ["supplier-x.example", "api.supplier-x.example"],
  "capabilities": ["fetch_quota", "fetch_pricing", "refresh_session"],
  "credential_permissions": ["auth_token", "session_cookie"]
}
```

字段说明：

- `id`：稳定且唯一的适配器标识。
- `display_name`：管理页显示名称。
- `supported_hosts`：自动绑定的供应商域名；主域名同时匹配其子域名。
- `capabilities`：当前脚本支持的能力说明。
- `credential_permissions`：脚本执行时允许读取的最小凭据集合。

可申请的凭据字段包括 `login_email`、`login_password`、`auth_token`、
`refresh_token`、`session_cookie`、`totp_secret` 和 `api_key_value`。不要申请
脚本不需要的字段。

内置适配器优先于自定义脚本。如果两个自定义脚本声明相同域名，当前按文件名
排序后的第一个匹配项生效，因此不应重复声明。

## 4. 输入契约

RelayDeck 通过标准输入传入一个 JSON 对象：

```json
{
  "profile": {
    "id": "api_xxx",
    "label": "Supplier X API",
    "supplier_id": "supplier_xxx",
    "api_base": "https://api.supplier-x.example/v1",
    "api_key_env": "SUPPLIER_X_API_KEY",
    "api_key_value": "<仅在清单授权后提供>",
    "custom_llm_provider": "openai",
    "quota": {
      "adapter": "custom-script",
      "script_name": "adapter_supplier_x.py",
      "currency": "CNY",
      "auth_token": "<仅在清单授权后提供>"
    }
  },
  "window": {
    "start_at": "2026-07-01T00:00:00+08:00",
    "end_at": "2026-07-20T12:00:00+08:00"
  }
}
```

适配器不读取凭据库文件，也不从命令行参数或环境变量接收密钥。

## 5. 输出契约

### Multiple quota sources

If a supplier exposes both a pay-as-you-go balance and a subscription, return
them as separate objects in `quota_items`. RelayDeck stores and renders each
source independently; it does not add their totals together.

```json
{
  "quota_items": [
    {"id": "wallet", "type": "balance", "label": "Pay-as-you-go", "remaining": 6.9},
    {"id": "monthly-plan", "type": "subscription", "label": "Monthly plan", "total": 1000, "used": 320, "remaining": 680, "period_end": "2026-07-31"}
  ]
}
```

标准输出必须只包含一个 JSON object：

```json
{
  "status": "ok",
  "adapter": "custom-script",
  "currency": "CNY",
  "balance_total": 100,
  "balance_used": 37.5,
  "balance_remaining": 62.5,
  "message": "额度已刷新",
  "credential_updates": {},
  "raw_summary": {
    "source": "supplier-x-balance-api"
  }
}
```

状态值：

- `ok`：成功取得可用额度。
- `partial`：只取得部分额度字段。
- `auth_required`：需要重新建立供应商登录态。
- `unsupported`：供应商当前接口不支持额度查询。
- `error`：网络、解析或执行失败。

金额字段无法获得时使用 `null`，不要伪造为 `0`。`raw_summary` 应提供可用于
排错的非敏感信息。

## 6. 凭据更新

若供应商接口返回新 token，脚本可通过 `credential_updates` 返回：

```json
{
  "credential_updates": {
    "auth_token": "new-token"
  }
}
```

RelayDeck 只接受清单中已授权的字段，并将其重新加密写入凭据库。

## 7. 安全要求

- 不得在 stdout、stderr、`message` 或 `raw_summary` 中输出凭据。
- 不得把凭据写入项目配置、日志或临时调试文件。
- 浏览器登录驱动只捕获目标供应商签发的登录态，不捕获 Google 密码、验证码、
  Cookie 或二次验证内容。
- 自定义脚本属于本机受信任代码，安装前必须审查来源。

## 8. 接入与验证

1. 复制模板并填写真实 `supported_hosts`。
2. 确认脚本从 stdin 读取 JSON，并只向 stdout 输出一个 JSON object。
3. 重启管理页，让 RelayDeck 重新扫描清单。
4. 打开对应供应商额度面板，确认显示“已自动绑定”。
5. 点击“刷新供应商额度”，检查状态、金额和错误提示。
6. 使用未知测试域名验证其显示“暂未安装额度适配器”，且不会执行脚本。

管理页不再保存或选择 `adapter`、`script_name`、手工余额、OpenAI 兼容账单、
管理员 API Key 等旧配置；旧字段在状态迁移时会被清理。

## 9. 可选价格目录

清单声明 `fetch_pricing` 后，脚本可以在结果中增加 `pricing_catalog`。未声明该能力时，RelayDeck 会忽略价格字段：

```json
{
  "pricing_catalog": [
    {
      "api_profile_id": "api_xxx",
      "upstream_model": "gpt-5.4",
      "group_name": "Pro 分组",
      "base_input_per_1m": 10,
      "base_output_per_1m": 40,
      "multiplier": 0.5,
      "currency": "USD",
      "effective_from": "2026-07-20T00:00:00+08:00",
      "confidence": "exact"
    }
  ]
}
```

核心根据供应商的价格自动化策略创建候选或生效版本。只修改 `group_name` 不会触发价格变化；缺失价格字段不得用零覆盖旧值。
