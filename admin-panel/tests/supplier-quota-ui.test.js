const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');


const html = fs.readFileSync(path.resolve(__dirname, '..', 'static', 'index.html'), 'utf8');
const scriptStart = html.lastIndexOf('<script>') + '<script>'.length;
const scriptEnd = html.indexOf('</script>', scriptStart);
const script = html.slice(scriptStart, scriptEnd);


test('frontend state loads and saves supplier quota configuration', () => {
  assert.match(script, /supplierQuotas:\s*\{\}/);
  assert.match(script, /supplier_quotas:\s*supplierQuotasForSave\(\)/);
  assert.match(script, /state\.supplierQuotas\s*=\s*data\.supplier_quotas\s*\|\|\s*\{\}/);
});

test('supplier platform exposes automatic browser SSO state', () => {
  assert.match(script, /function supplierQuotaPanelTemplate\(group\)/);
  assert.match(script, /const browserSso = quota\.browser_sso/);
  assert.match(script, /const ssoSupported = Boolean\(browserSso\.id\)/);
  assert.match(script, /data-action="start-supplier-sso"/);
  assert.match(script, /data-action="clear-supplier-sso-session"/);
});

test('supplier SSO uses dedicated status, completion, cancellation, and reset endpoints', () => {
  assert.match(script, /function startSupplierSso\(/);
  assert.match(script, /function completeSupplierSso\(/);
  assert.match(script, /function cancelSupplierSso\(/);
  assert.match(script, /function clearSupplierSsoSession\(/);
  assert.match(script, /\/api\/supplier-sso\//);
  assert.match(script, /\/api\/supplier-credentials\/\$\{encodeURIComponent\(key\)\}/);
});

test('supplier SSO polling stops on terminal status and page unload', () => {
  assert.match(script, /supplierSsoPollers\s*=\s*new Map\(\)/);
  assert.match(script, /function stopSupplierSsoPolling\(/);
  assert.match(script, /window\.setInterval\([^]*2000/);
  assert.match(script, /interaction_required/);
  assert.match(script, /beforeunload[^]*stopAllSupplierSsoPolling/);
});

test('view switching preserves supplier state and does not expose generic adapter controls', () => {
  assert.match(script, /openSupplierPlatformKeys:\s*\[\]/);
  assert.match(script, /state\.openSupplierPlatformKeys\.includes\(group\.key\)/);
  assert.doesNotMatch(script, /data-quota-key="adapter"/);
  assert.doesNotMatch(html, /<option value="openai-compatible"/);
  assert.doesNotMatch(html, /<option value="portal_admin_key"/);
  assert.doesNotMatch(html, /<option value="custom-script"/);
});

test('automatic monitoring remains separate from shared quota ownership', () => {
  assert.match(script, /data-supplier-quota-key=/);
  assert.match(script, /data-quota-key="enabled"/);
  assert.match(html, /5 分钟/);
  assert.doesNotMatch(html, /请先启用供应商共享额度/);
});

test('supplier quota panel renders independent balance and subscription sources', () => {
  assert.doesNotMatch(script, /data-quota-key="billing_mode"/);
  assert.match(script, /quota_items/);
  assert.match(script, /quotaItems\.map/);
  assert.match(script, /quotaItem\.type === 'subscription'/);
  assert.match(script, /period_end/);
});

test('manual token and password controls are not rendered in the supplier panel', () => {
  assert.match(script, /const credentialPanel = ''/);
  assert.doesNotMatch(script, /const credentialPanel = adapterSupported/);
  assert.doesNotMatch(script, /data-credential-key="auth_token"/);
  assert.doesNotMatch(script, /data-credential-key="session_cookie"/);
  assert.doesNotMatch(script, /data-credential-key="login_password"/);
});

test('browser SSO wording is supplier-neutral', () => {
  assert.match(script, /使用系统浏览器完成 \$\{escapeHtml\(browserSso\.display_name \|\| adapterLabel\)\} 登录/);
  assert.match(script, /第一步打开系统浏览器并登录 \$\{browserSso\.display_name \|\| adapterLabel\}/);
});

test('supplier SSO exposes an editable portal base and the effective login URL', () => {
  assert.match(script, /data-quota-key="portal_base"/);
  assert.match(script, /data-portal-login-path/);
  assert.match(script, /browserSso\.portal_base/);
  assert.match(script, /browserSso\.login_path/);
  assert.match(script, /field === 'portal_base'/);
  assert.match(script, /实际打开/);
  assert.doesNotMatch(script, /data-action="check-supplier-portal"/);
});

test('conversation tests are attached to individual available model rows', () => {
  assert.doesNotMatch(script, /data-action="check-api-profile-dialog"/);
  assert.match(script, /data-action="check-api-model-dialog"/);
  assert.match(script, /function runSingleBindingCheck\(/);
  assert.match(script, /function handleRoutingAction\([^]*check-api-model-dialog/);
  assert.match(script, /const modelLabel = binding\.upstream_model/);
  assert.match(script, /测试结果：\$\{profile\.label \|\| 'API'\} \/ \$\{modelLabel\}/);
});

test('conversation tests expose a bottom floating status notice', () => {
  assert.match(html, /id="floating-action-notice"/);
  assert.match(html, /id="btn-close-floating-action-notice"/);
  assert.match(script, /function showFloatingActionNotice\(/);
  assert.match(script, /showFloatingActionNotice\(pendingMessage/);
});

test('successful supplier SSO automatically refreshes shared quota with user feedback', () => {
  assert.match(script, /function supplierProfileIdsForPlatform\(/);
  assert.match(script, /function refreshSupplierQuotaAfterSso\(/);
  assert.match(script, /登录成功，正在自动刷新额度/);
  assert.match(script, /登录成功，额度刷新成功/);
  assert.match(script, /refreshSupplierQuotaAfterSso\(key\)/);
});

test('state loading repairs a missed authenticated SSO quota refresh', () => {
  assert.match(script, /function scheduleAuthenticatedSupplierQuotaRefreshes\(/);
  assert.match(script, /authUpdatedAt > latestCheckedAt/);
  assert.match(script, /scheduleAuthenticatedSupplierQuotaRefreshes\(\)/);
  assert.match(script, /supplierSsoQuotaRefreshes\.has\(key\)/);
});

test('supplier panel renders inline shared quota progress', () => {
  assert.match(script, /function supplierInlineQuotaProgressTemplate\(/);
  assert.match(script, /supplierUsageDashboardRow\(platformKey\)/);
  assert.match(script, /quota_items/);
  assert.match(script, /quotaItems\.map/);
  assert.match(script, /percent\.toFixed\(1\)/);
  assert.match(script, /adapterSupported\s*\?\s*supplierInlineQuotaProgressTemplate\(platformKey\)\s*:\s*''/);
});

test('supplier pricing policy and version actions are exposed', () => {
  assert.match(script, /data-quota-key="pricing_automation"/);
  assert.match(script, /\/api\/supplier-pricing\//);
  assert.match(script, /activate-pricing-version/);
  assert.match(script, /reject-pricing-version/);
  assert.match(script, /recalculate-cost-performance/);
  assert.match(script, /重新统计性价比/);
  assert.match(script, /性价比已重新统计/);
});

test('supplier panel exposes cost batches and attribution coverage', () => {
  assert.match(script, /function supplierCostAttributionCardTemplate\(/);
  assert.match(script, /data-action="save-supplier-cost-batch"/);
  assert.match(script, /data-cost-key="billing_type"/);
  assert.match(script, /data-cost-key="service_start"/);
  assert.match(script, /data-cost-key="service_end"/);
  assert.match(script, /supplierCostAttributionCardTemplate/);
  assert.match(script, /data-action="update-recharge-payment"/);
  assert.match(script, /PATCH/);
});

test('cost entry is tied to detected recharge events instead of a blank top-up form', () => {
  assert.match(script, /data-recharge-payment-id/);
  assert.match(script, /data-recharge-payment-key="paid_amount_cny"/);
  assert.match(script, /没有待补充的按量充值事件/);
  assert.match(script, /function updateRechargeRecordPayment\(/);
  assert.match(script, /supplierRechargePaymentInput\(/);
  assert.doesNotMatch(script, /<label>成本批次类型<\/label>/);
});

test('cost performance chart excludes models without confirmed cash cost', () => {
  assert.match(script, /function modelCostBarChartTemplate\(/);
  assert.match(script, /item\.cost_source === 'actual-cash'/);
  assert.match(script, /usage-comparison-chart/);
  assert.match(script, /柱长越长代表单位 Token 成本越低/);
});

test('subscription entry only renders when a subscription quota is detected', () => {
  assert.match(script, /quotaItems\.some\(item => String\(item\?\.type \|\| ''\)\.toLowerCase\(\) === 'subscription'\)/);
  assert.match(script, /const subscriptionEntryPanel = hasDetectedSubscription/);
  assert.match(script, /\$\{subscriptionEntryPanel\}/);
});

test('cost performance analysis is available from the sidebar in a dedicated dialog', () => {
  assert.match(html, /id="btn-show-cost-analysis"/);
  assert.match(html, /id="cost-analysis-overlay"/);
  assert.match(html, /id="cost-analysis-chart"/);
  assert.match(html, /id="cost-analysis-table"/);
  assert.match(script, /function renderCostAnalysisDialog\(/);
  assert.match(script, /showCostAnalysisDialog\(/);
  assert.match(script, /btn-close-cost-analysis/);
});

test('supplier cost panel exposes subscription utilization and expiry-loss metrics', () => {
  assert.match(script, /subscription_usage/);
  assert.match(script, /consumed_cost_cny/);
  assert.match(script, /full_use_cost_per_credit_cny/);
  assert.match(script, /projected_expiry_loss_cny/);
  assert.match(script, /用满/);
  assert.match(script, /预计到期损失/);
});
