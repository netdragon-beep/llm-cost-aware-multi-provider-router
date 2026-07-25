# 供应商独立浏览器 SSO 设计

## 目标

在 RelayDeck 控制面板中为供应商建立独立的浏览器登录会话。用户只需在首次授权或供应商要求重新验证时，在可见浏览器窗口中完成 Google SSO；RelayDeck 不读取或保存 Google 密码，只获取供应商最终签发的 Token、Cookie 或刷新凭据，并继续使用 Windows DPAPI CurrentUser 加密保存。

第一阶段内置支持 `lingsuan.top`。浏览器生命周期、任务状态和凭据落库由核心模块管理；供应商特有的登录地址、成功判定和凭据提取逻辑放在独立适配器中，为后续供应商扩展保留边界。

## 非目标

- 不读取或复用用户日常 Edge/Chrome 配置目录。
- 不保存 Google 密码、验证码或 TOTP 历史值。
- 不尝试绕过验证码、二步验证或 Google 风控。
- 不保证登录永不失效；需要人工验证时必须明确提示用户。
- 第一阶段不开发浏览器扩展，也不把夸克等非标准 Chromium 浏览器列为正式支持项。

## 用户流程

1. 用户在“供应商共享额度与登录”中选择“灵算网站后台”；额度始终按供应商共享，自动额度监控开关不影响登录授权。
2. 用户点击“建立浏览器登录”。
3. 控制面板调用后台启动接口，立即获得一个登录任务 ID，并显示“等待登录”。
4. 后台启动未附加 Playwright、远程调试或 WebDriver 参数的系统 Edge/Chrome，使用 RelayDeck 独立资料目录打开 `https://lingsuan.top/dashboard`。
5. 用户在该窗口中完成 Google SSO。成功进入灵算后台后，用户关闭该窗口并在控制面板点击“我已完成登录”。
6. 后台随后使用 Playwright 接管同一独立资料目录，在无交互模式下验证 `/api/v1/auth/me` 并提取灵算签发的授权信息。
7. 核心模块将授权信息写入 DPAPI 凭据库，更新 SSO 状态，然后关闭浏览器窗口。
8. 控制面板轮询任务状态，最终显示“登录有效”，并允许立即刷新额度。

用户可以随时取消正在进行的登录任务、重新授权，或清除某个供应商的独立浏览器会话。清除浏览器会话不自动删除已保存的供应商 Token；清除供应商登录凭据时则同时提供是否清理浏览器会话的明确选项。

## 架构

### 浏览器会话管理器

新增独立模块负责：

- 为每个规范化供应商域名维护一个登录任务和互斥锁。
- 在工作线程中启动系统浏览器和后续 Playwright 校验，避免阻塞 FastAPI 请求线程。
- 将浏览器资料保存在 `data/browser-sessions/<supplier-hash>/`。
- 人工 Google 登录优先使用系统 Edge，其次使用系统 Chrome；Playwright 只负责登录后的供应商会话校验和静默续期。
- 对外只返回任务状态、时间和安全的错误分类，不返回请求头、Cookie 或 Token。

同一个供应商同一时间只允许一个浏览器任务。管理页服务重启后，遗留的 `running` 状态自动转为 `interrupted`，浏览器资料仍可用于下次续期。

### 供应商 SSO 适配器

适配器接口至少提供：

- `login_url`：首次打开的供应商页面。
- `is_authenticated(page, response)`：判断供应商登录是否完成。
- `extract_supplier_credentials(context, request)`：提取供应商自己的授权信息。
- `validate_credentials(credentials)`：使用供应商身份接口验证凭据。

灵算适配器监听同源 `/api/v1/auth/me` 请求。只接受 `lingsuan.top` 的 HTTPS 请求和响应，忽略第三方页面、Google 请求及其他域名，避免采集 Google 授权信息。

### 凭据与浏览器资料

- 灵算 `auth_token`、`refresh_token`、`session_cookie` 继续写入 `credential-vault.json`，由 DPAPI CurrentUser 加密。
- 浏览器资料目录加入 `.gitignore`，并限制为当前 Windows 用户访问。
- Chromium 自身负责加密浏览器 Cookie；RelayDeck 不直接读取 Google Cookie 数据库。
- 日志、任务状态、API 响应和前端均不得出现 Token、Cookie、Authorization 请求头或浏览器存储内容。
- 成功提取供应商凭据后，尽量清理灵算源站中重复保存的明文 Token；Google 会话仍由独立 Chromium 资料管理。

## 自动续期

普通余额刷新优先使用 DPAPI 中现有的供应商凭据。若灵算返回明确的 `401/403`：

1. 供应商没有正在运行的 SSO 任务时，创建一次后台续期任务。
2. 使用独立浏览器资料启动 Chromium，先尝试访问灵算后台。
3. 若现有 Google/灵算会话能够无交互恢复登录，则更新 DPAPI 凭据并重试一次额度请求。
4. 若出现登录页、账号选择、验证码或二步验证，则停止静默续期，将状态设为 `interaction_required`。
5. 管理页在供应商卡片中显示红色“需要重新授权”，用户点击后启动可见浏览器继续登录。

每次余额刷新最多触发一次续期和一次请求重试，避免错误状态下循环启动浏览器。并发的五分钟定时刷新共享同一个供应商续期任务。

## 后台接口

- `POST /api/supplier-sso/{supplier_key}/start`：启动可见登录任务。
- `GET /api/supplier-sso/{supplier_key}/status`：读取安全状态。
- `POST /api/supplier-sso/{supplier_key}/complete`：确认系统浏览器登录已完成，进入会话校验阶段。
- `POST /api/supplier-sso/{supplier_key}/cancel`：取消当前任务。
- `DELETE /api/supplier-sso/{supplier_key}/session`：清除独立浏览器资料。

状态包含 `idle`、`starting`、`waiting_for_user`、`validating`、`authenticated`、`interaction_required`、`cancelled`、`interrupted`、`browser_missing`、`timed_out` 和 `error`。错误只返回分类和可操作提示。

## 控制面板

在供应商级“加密登录凭据”区域增加：

- “建立浏览器登录”主按钮和等待阶段的“我已完成登录”按钮。
- “重新授权”“取消登录”“清除浏览器会话”状态相关操作。
- SSO 状态标签和最近授权时间。
- 登录进行中每两秒轮询一次状态；任务结束或页面离开时停止轮询。

切换到“灵算网站后台”时显示针对当前状态的下一步提示，但不自动打开浏览器。打开外部登录窗口必须由用户点击按钮触发。

## 安装与兼容性

第一阶段将 `playwright` 加入 `llm-stack-local` Conda 环境，并通过安装脚本执行 Chromium 浏览器安装。启动时检测浏览器运行文件；缺失时控制面板显示安装说明，不在额度刷新任务中临时下载。

人工登录浏览器顺序为：

1. 本机 Microsoft Edge。
2. 本机 Google Chrome。

Playwright 自带 Chromium 不再用于 Google 人工登录，只作为无交互校验和静默续期的兼容运行时。

自定义 Chromium 可执行文件属于后续实验性功能，不进入第一阶段。

## 故障与恢复

- 浏览器缺失：状态为 `browser_missing`，提供安装命令，不影响手工 Token 和其他额度方式。
- 用户关闭窗口：状态为 `cancelled` 或 `interaction_required`，不覆盖已有有效凭据。
- 登录超时：默认十分钟停止任务，不删除浏览器资料和旧凭据。
- 凭据验证失败：不写入 DPAPI，保留旧凭据并记录安全错误摘要。
- 管理页或服务重启：任务转为 `interrupted`，用户可重新启动。
- 供应商页面变化：仅灵算适配器失败，不影响其他供应商和 LiteLLM 路由。

## 测试

- 单元测试浏览器任务状态机、供应商互斥、取消、超时和重启恢复。
- 使用假 Playwright 驱动测试成功登录、需要交互、验证失败和敏感信息不出现在状态中。
- 测试灵算适配器只采集 `lingsuan.top` 同源授权，不采集 Google 请求。
- 测试 DPAPI 写入、旧凭据保留和成功后原子更新。
- 第二阶段测试余额 `401` 只触发一次续期、并发刷新共享任务、失败后不循环。
- 前端测试按钮状态、轮询停止、错误提示和凭据字段保持只写。
- 浏览器人工验收覆盖首次 Google 登录、关闭窗口、再次静默续期和“需要重新授权”流程。

## 分阶段交付

第一阶段完成手动发起的独立浏览器登录、灵算凭据提取、DPAPI 保存、状态展示和手动刷新额度。第二阶段接入余额 `401` 自动静默续期。这样可以先验证灵算实际 SSO 行为，再把浏览器任务接入五分钟定时刷新，降低一次性改动风险。
