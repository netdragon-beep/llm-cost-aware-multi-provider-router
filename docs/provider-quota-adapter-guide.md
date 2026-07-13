# RelayDeck Local 供应商额度适配脚本指南

状态：使用中  
适用范围：`quota-adapters/` 下的自定义额度脚本  
目标读者：需要为新供应商接入“余额 / 套餐 / 额度”抓取能力的维护者

## 1. 这份文档解决什么问题

当某个供应商不能直接用内置适配方式获取额度时，我们需要为它补一个脚本适配器。

这份文档定义的是一套统一规则，让后面接新供应商时遵循同一个约定：

- 什么情况应该写自定义脚本
- 脚本从哪里拿配置
- 脚本应该输出什么 JSON
- 管理面怎样识别脚本结果
- 出问题时应该怎么调试

## 2. 什么时候需要写新脚本

优先不要急着写脚本，先判断以下几种情况：

1. 如果供应商兼容 OpenAI 账单接口，优先尝试 `openai_compatible_balance`
2. 如果供应商和灵算 / autocode 这类站点结构接近，优先尝试现有 `lingsuan-web`、`autocode-web`、`lingsuan-admin`、`autocode-admin`
3. 只有当现有适配模式都不适用时，再写 `custom-script`

常见需要写脚本的场景：

- 供应商有私有后台接口，但返回结构和现有站点完全不同
- 供应商必须组合多个接口才能算出余额
- 供应商需要特殊认证头、Cookie 或自定义签名
- 供应商只在页面接口里暴露套餐 / 钱包额度

## 3. 总体约定

脚本放置目录：

- [`quota-adapters/`](../quota-adapters/)

管理面配置方式：

1. 打开某个 `API Profile`
2. 在“计费与额度”里设置
3. `quota.adapter = custom-script`
4. `quota.script_name = your_script.py`
5. 可选：`quota.script_timeout_sec = 60`

支持脚本类型：

- `.py`
- `.ps1`
- `.cmd`
- `.bat`

推荐优先使用 Python，因为当前项目内置的示例和调试体验都更友好。

## 4. 输入契约

脚本会收到一份 JSON 上下文。

传入方式有两种：

- `stdin`
- 环境变量 `RELAYDECK_QUOTA_CONTEXT`

建议脚本优先从 `stdin` 读取，拿不到时再读环境变量。

输入大致结构如下：

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
      "script_name": "example_provider.py",
      "script_timeout_sec": 60,
      "portal_base": "https://example.com",
      "auth_token": "optional bearer token",
      "session_cookie": "optional session=..."
    }
  },
  "window": {
    "start_at": "2026-06-01T00:00:00+08:00",
    "end_at": "2026-06-26T09:00:00+08:00"
  }
}
```

脚本最常用到的字段：

- `profile.api_base`
- `profile.api_key_value`
- `profile.label`
- `profile.quota.portal_base`
- `profile.quota.auth_token`
- `profile.quota.session_cookie`
- `window.start_at`
- `window.end_at`

## 5. 输出契约

脚本必须向 `stdout` 打印且只打印一个 JSON object。

最小推荐输出：

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

字段说明：

- `status`
  - `ok`：成功拿到可展示额度
  - `unsupported`：接口能通，但当前脚本还无法提取出统一额度字段
  - `error`：认证失败、网络失败、站点返回异常等
- `adapter`
  - 建议填 `custom-script`
  - 也可以写更具体的名字，比如 `custom-script-foo`
- `currency`
  - 如 `CNY`、`USD`、`QUOTA`、`CREDIT`
- `balance_total`
  - 总额度，可为空
- `balance_used`
  - 已用额度，可为空
- `balance_remaining`
  - 剩余额度，可为空
- `message`
  - 给前端和日志看的说明文字
- `raw_summary`
  - 调试信息，强烈建议带上

如果只有两项，也允许：

- 只知道 `balance_total` 和 `balance_remaining`
- 只知道 `balance_used` 和 `balance_remaining`

后端会尽量补算剩余字段。

## 6. 推荐实现步骤

### 第一步：确认站点有没有可用接口

优先找这些接口类型：

- 用户信息
- 套餐概览
- 余额
- 用量
- 订阅进度
- 钱包 / 充值记录

优先从浏览器 `F12 -> Network` 下手，而不是盲猜 URL。

### 第二步：手工验证请求能通

先在浏览器里确认：

- 请求头里需要什么认证信息
- 是 `Bearer token` 还是 `Cookie`
- 返回的是 JSON 还是 HTML 壳页面

### 第三步：先抓原始响应，再做字段提取

先把站点返回塞进 `raw_summary`，再去算：

- `balance_total`
- `balance_used`
- `balance_remaining`

不要一上来就只返回最终数字，否则后面排错会很痛苦。

### 第四步：统一成框架能识别的输出

只要脚本输出符合第 5 节的 JSON 契约，管理面就能接进去展示。

## 7. 推荐的 `raw_summary` 结构

推荐按“接口名 -> 调用结果”组织：

```json
{
  "portal_base": "https://example.com",
  "auth_mode": "website_token",
  "balance": {
    "url": "https://example.com/api/v1/balance",
    "status_code": 200,
    "body": {
      "code": 0,
      "data": {
        "balance": 12.34
      }
    }
  },
  "usage": {
    "url": "https://example.com/api/v1/usage",
    "status_code": 200,
    "body": {
      "code": 0,
      "data": {
        "used": 87.66
      }
    }
  }
}
```

这样做的好处是：

- 后面从日志看得清楚
- 前端报错时能快速定位是哪条接口挂了
- 以后如果要把这个脚本升级成内置适配器，也更容易迁移

## 8. 错误处理建议

推荐按下面规则返回：

### 认证失败

返回：

```json
{
  "status": "error",
  "adapter": "custom-script",
  "message": "后台登录态无效：HTTP 401，请重新获取 auth_token 或 session_cookie。",
  "raw_summary": {
    "auth_me": {
      "status_code": 401
    }
  }
}
```

### 接口可通但字段无法识别

返回：

```json
{
  "status": "unsupported",
  "adapter": "custom-script",
  "message": "接口已返回，但当前脚本未识别出统一额度字段。",
  "raw_summary": {
    "balance": {
      "status_code": 200,
      "body": {
        "..."
      }
    }
  }
}
```

### 网络异常

返回：

```json
{
  "status": "error",
  "adapter": "custom-script",
  "message": "请求站点接口失败：SSL EOF",
  "raw_summary": {
    "error": "..."
  }
}
```

## 9. 调试 checklist

接新供应商时，建议按这个顺序排：

1. `portal_base` 是否正确
2. `auth_token` 是否过期
3. `session_cookie` 是否仍然有效
4. 站点返回的是 JSON 还是 HTML 登录页
5. 是否需要额外请求头，如 `x-api-key`、`Origin`、`Referer`
6. 数值字段到底代表“总额 / 已用 / 剩余”中的哪个
7. 返回时间是否为 UTC，需要不要换算显示

## 10. 一个最小 Python 模板

```python
import json
import sys
import urllib.request
import urllib.error


def read_context():
    raw = sys.stdin.read().strip()
    return json.loads(raw) if raw else {}


def request_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw_text": body[:500]}


def main():
    context = read_context()
    profile = context.get("profile") or {}
    quota = profile.get("quota") or {}

    portal_base = str(quota.get("portal_base") or "").strip()
    auth_token = str(quota.get("auth_token") or "").strip()

    if not portal_base or not auth_token:
        print(json.dumps({
            "status": "unsupported",
            "adapter": "custom-script",
            "message": "缺少 portal_base 或 auth_token。",
            "raw_summary": {
                "portal_base": portal_base
            }
        }, ensure_ascii=False))
        return

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "User-Agent": "RelayDeck-Local/1.0",
    }

    status_code, body = request_json(f"{portal_base}/api/v1/balance", headers)

    if status_code >= 400:
        print(json.dumps({
            "status": "error",
            "adapter": "custom-script",
            "message": f"余额接口失败：HTTP {status_code}",
            "raw_summary": {
                "balance": {
                    "status_code": status_code,
                    "body": body
                }
            }
        }, ensure_ascii=False))
        return

    remaining = float(body["data"]["balance"])

    print(json.dumps({
        "status": "ok",
        "adapter": "custom-script",
        "currency": "CNY",
        "balance_remaining": remaining,
        "message": "",
        "raw_summary": {
            "balance": {
                "status_code": status_code,
                "body": body
            }
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

## 11. 现有参考

可以直接参考这些文件：

- [`quota-adapters/README.md`](../quota-adapters/README.md)
- [`quota-adapters/example_lingsuan.py`](../quota-adapters/example_lingsuan.py)
- [`quota-adapters/adapter_78code_newapi.py`](../quota-adapters/adapter_78code_newapi.py)
- [`admin-panel/app.py`](../admin-panel/app.py:1277)

## 12. 维护约定

后面如果新增供应商适配，请尽量满足这几条：

- 脚本名带供应商标识，如 `adapter_supplier_xxx.py`
- 不把真实密钥硬编码进脚本
- 所有站点返回尽量放进 `raw_summary`
- 错误消息写人话，方便在管理面直接看懂
- 如果这个脚本已经足够通用，再考虑把它沉淀成内置适配模式，而不是长期只留在个人脚本里
