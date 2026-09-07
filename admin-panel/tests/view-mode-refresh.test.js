const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const htmlPath = path.resolve(__dirname, '..', 'static', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const scriptStart = html.lastIndexOf('<script>') + '<script>'.length;
const scriptEnd = html.indexOf('</script>', scriptStart);
const script = html.slice(scriptStart, scriptEnd);

function loadViewModeResolver() {
  const start = script.indexOf('function resolveRoutingViewMode');
  const end = script.indexOf('\n\n    async function loadState', start);
  assert.notEqual(start, -1, 'resolveRoutingViewMode must exist');
  assert.notEqual(end, -1, 'resolveRoutingViewMode must be defined before loadState');
  const context = {};
  vm.runInNewContext(script.slice(start, end), context);
  return context.resolveRoutingViewMode;
}

function loadRawModelFamilyResolver() {
  const start = script.indexOf('function rawModelFamily(profile, rawModel, matches = [])');
  const end = script.indexOf('\n\n    function rawModelFamilyOptions', start);
  assert.notEqual(start, -1, 'raw model family resolver must exist');
  assert.notEqual(end, -1, 'raw model family resolver must be defined before its options');
  const context = {
    normalizeBindingModelKey: value => String(value || '').trim().toLowerCase(),
    routeById: () => null,
    guessModelFamilyName: () => '',
  };
  return vm.runInNewContext(`(() => { const UNCATEGORIZED_MODEL_FAMILY = '未分类'; ${script.slice(start, end)}; return rawModelFamily; })()`, context);
}

test('runtime state refresh preserves the current supplier view', () => {
  const resolveRoutingViewMode = loadViewModeResolver();
  assert.equal(resolveRoutingViewMode('by_supplier', 'by_model', true), 'by_supplier');
});

test('initial state load uses the persisted backend view', () => {
  const resolveRoutingViewMode = loadViewModeResolver();
  assert.equal(resolveRoutingViewMode('by_model', 'by_supplier', false), 'by_supplier');
});

test('invalid view modes fall back to the model view', () => {
  const resolveRoutingViewMode = loadViewModeResolver();
  assert.equal(resolveRoutingViewMode('invalid', 'invalid', true), 'by_model');
});

test('loadState preserves runtime view by default and opts out on initial load', () => {
  assert.match(script, /async function loadState\(\{ preserveViewMode = true \} = \{\}\)/);
  assert.match(
    script,
    /state\.routingViewMode = resolveRoutingViewMode\(state\.routingViewMode, data\.routing_view_mode, preserveViewMode\);/,
  );
  assert.match(script, /loadState\(\{ preserveViewMode: false \}\)\.catch/);
});

test('model view always renders all public routes while published routes sort first', () => {
  assert.doesNotMatch(html, /id="model-route-pinned-only"/);
  const renderStart = script.indexOf('function renderRouting()');
  const renderEnd = script.indexOf('\n    function ', renderStart + 10);
  const renderRouting = script.slice(renderStart, renderEnd === -1 ? undefined : renderEnd);
  assert.doesNotMatch(renderRouting, /\.filter\(route => !pinnedOnly \|\| isPinnedModelRoute\(route\.id\)\)/);
  assert.match(renderRouting, /const routes = state\.modelRoutes\s*\.filter\(route => !search \|\| routeSearchText\(route\)\.includes\(search\)\)/);
  assert.match(renderRouting, /const aPinned = isPinnedModelRoute\(a\.id\) \? 1 : 0;/);
});

test('model route enable control uses a compact custom switch instead of the oversized native checkbox', () => {
  assert.match(script, /class="route-enabled-toggle"/);
  assert.match(script, /class="route-enabled-checkbox"/);
  assert.match(html, /\.route-enabled-checkbox\s*\{[\s\S]*?appearance:\s*none;/);
  assert.match(html, /\.route-enabled-checkbox:checked\s*\{[\s\S]*?background:/);
});

test('automatic balance refresh control uses a compact custom switch and explicit status badge', () => {
  assert.match(script, /wrapper\.className = 'auto-refresh-toggle'/);
  assert.match(script, /id="auto-refresh-balances-toggle" class="auto-refresh-checkbox"/);
  assert.match(script, /badge\.className = 'pill muted auto-refresh-status'/);
  assert.match(script, /label\.dataset\.state = state\.autoRefreshBalancesEnabled \? 'on' : 'off';/);
  assert.match(html, /\.auto-refresh-checkbox\s*\{[\s\S]*?appearance:\s*none;/);
  assert.match(html, /\.auto-refresh-status\[data-state="on"\]/);
});

test('model sync auto-binds unique exact public-name matches without publishing routes', () => {
  const syncStart = script.indexOf('function syncKnownModelsToRoutes');
  const syncEnd = script.indexOf('\n    function updateRouteField', syncStart);
  const syncBlock = script.slice(syncStart, syncEnd);
  assert.match(syncBlock, /routesByModelKey = new Map\(\)/);
  assert.match(syncBlock, /matchingRoutes\.length !== 1/);
  assert.match(syncBlock, /normalizeBindingModelKey\(binding\.upstream_model \|\| ''\) === modelKey/);
  assert.match(syncBlock, /createdBindings \+= 1/);
  assert.match(syncBlock, /state\.routeBindings\.push\(binding\)/);
  assert.doesNotMatch(syncBlock, /route\.gateway_enabled\s*=\s*true/);
});

test('gpt-5.6-sol is covered by the exact-match sync path', () => {
  const syncStart = script.indexOf('function syncKnownModelsToRoutes');
  const syncEnd = script.indexOf('\n    function updateRouteField', syncStart);
  const syncBlock = script.slice(syncStart, syncEnd);
  assert.match(syncBlock, /normalizeBindingModelKey\(route\.public_model_name \|\| ''\)/);
  assert.match(syncBlock, /binding\.upstream_model = String\(modelName \|\| ''\)\.trim\(\)/);
  assert.match(syncBlock, /state\.openRouteIds = \[\.\.\.new Set\(\[\.\.\.state\.openRouteIds, \.\.\.syncedRouteIds\]\)\]/);
});

test('API action scope preserves the current supplier expansion context', () => {
  assert.match(
    script,
    /function beginApiActionScope\(profileId\) \{[\s\S]*rememberRoutingLayout\(\)[\s\S]*openSupplierPlatformKeys/,
  );
  assert.match(
    script,
    /function applyApiActionScope\(\) \{[\s\S]*state\.openSupplierPlatformKeys = \[\.\.\.scope\.openSupplierPlatformKeys\][\s\S]*state\.openApiProfileIds/,
  );
});

test('view switching redraws only the routing section', () => {
  const toolbarStart = script.indexOf('function bindToolbar()');
  const toolbarEnd = script.indexOf('\n    function ', toolbarStart + 10);
  const toolbar = script.slice(toolbarStart, toolbarEnd === -1 ? undefined : toolbarEnd);
  assert.match(toolbar, /el\('btn-view-model'\)\.onclick = \(\) => \{\s*state\.routingViewMode = 'by_model';\s*renderRouting\(\);/);
  assert.match(toolbar, /el\('btn-view-supplier'\)\.onclick = \(\) => \{\s*state\.routingViewMode = 'by_supplier';\s*renderRouting\(\);/);
  assert.doesNotMatch(toolbar, /el\('btn-view-model'\)\.onclick[\s\S]*?renderAll\(\)/);
  assert.doesNotMatch(toolbar, /el\('btn-view-supplier'\)\.onclick[\s\S]*?renderAll\(\)/);
});

test('routing renders reuse binding indexes instead of scanning every binding per card', () => {
  assert.match(script, /bindingIndexes:\s*\{\s*byRouteId:\s*new Map\(\),\s*byApiProfileId:\s*new Map\(\)/);
  assert.match(script, /function rebuildBindingIndexes\(\)/);
  assert.match(script, /function bindingsForRoute\(routeId\)\s*\{\s*return state\.bindingIndexes\.byRouteId\.get\(routeId\) \|\| \[\];/);
  assert.match(script, /function bindingsForApiProfile\(apiProfileId\)\s*\{\s*return state\.bindingIndexes\.byApiProfileId\.get\(apiProfileId\) \|\| \[\];/);
  assert.match(script, /rebuildBindingIndexes\(\);\s*state\.env = data\.env/);
});

test('collapsed API cards render a lightweight shell until opened', () => {
  assert.match(script, /function apiProfileTemplate\(profile\)/);
  assert.match(script, /data-api-lazy-content=/);
  assert.match(script, /function hydrateApiProfileDetails\(/);
});

test('raw model candidates load only when their route section is opened', () => {
  assert.match(script, /data-route-raw-models-container=/);
  assert.match(script, /function hydrateRawModelCandidates\(/);
  assert.match(script, /foldKey\.startsWith\('route-raw-models-'/);
});

test('service startup polling updates only lightweight service status', () => {
  assert.match(script, /async function refreshServiceStatus\(\)/);
  assert.match(script, /api\('\/api\/service-status'\)/);
  assert.match(script, /await refreshServiceStatus\(\);/);
  const loopStart = script.indexOf('for (let index = 0; index < maxChecks; index += 1) {');
  const loopEnd = script.indexOf('\n          if (reachedTarget)', loopStart);
  assert.notEqual(loopStart, -1);
  assert.notEqual(loopEnd, -1);
  assert.doesNotMatch(script.slice(loopStart, loopEnd), /await loadState\(\);/);
});

test('gateway status dialog aggregates gateway services while Agent configuration stays separate', () => {
  assert.match(html, /id="service-management-overlay" data-open="false"/);
  assert.match(html, /id="service-management-services"/);
  assert.deepEqual(
    [...html.matchAll(/data-service-dialog="([^"]+)"/g)].map(([, service]) => service),
    ['gateway'],
  );
  assert.match(html, /<div class="sidebar-links">[\s\S]*<button class="sidebar-link"[^>]*data-service-dialog="gateway"[^>]*aria-haspopup="dialog"/);
  assert.match(html, /<button class="sidebar-link"[^>]*id="btn-show-litellm-connection-info"[^>]*aria-haspopup="dialog"/);
  assert.doesNotMatch(html, /class="status-grid"/);
  assert.doesNotMatch(html, /gateway-status-indicator|gateway-status-summary|service-info-row/);
  assert.doesNotMatch(html, /data-service-dialog="admin_panel"/);
  assert.doesNotMatch(html, /data-service-dialog="litellm"/);
  assert.match(script, /function renderServiceDialog\(/);
  assert.match(script, /function openServiceDialog\(/);
  assert.match(script, /btn-close-service-management/);
  assert.match(script, /function statusPill\(listening, port, label\)/);
  assert.match(script, /const GATEWAY_COMPONENT_META =/);
  assert.match(script, /function gatewayStatusDetails\(/);
  assert.match(script, /function gatewayStatusPill\(/);
  assert.match(script, /function renderGatewayServiceRows\(/);
  assert.match(script, /service-state service-state-ok/);
  assert.match(script, /service-state service-state-fail/);
  assert.match(script, /statusPill\(Boolean\(status\.listening\), port, meta\.label\)/);
  assert.doesNotMatch(script, /gateway-status-indicator|gateway-status-summary/);
  assert.match(script, /controlService: 'litellm'/);
  assert.match(script, /api\(`\/api\/service\/\$\{controlService\}\/\$\{action\}`/);
  assert.doesNotMatch(script, /LISTENING \$\{port\}/);
});

test('unknown synced models resolve to the uncategorized family', () => {
  const rawModelFamily = loadRawModelFamilyResolver();
  assert.equal(rawModelFamily({}, 'vendor-private-model'), '未分类');
});

test('model availability groups use the raw model family instead of a temporary mapping bucket', () => {
  const availabilityStart = script.indexOf('const availabilityGroups = new Map();');
  const availabilityEnd = script.indexOf('const availabilitySearch =', availabilityStart);
  assert.match(script.slice(availabilityStart, availabilityEnd), /const family = rawModelFamily\(profile, row\.rawModel, row\.matches\);/);
  assert.doesNotMatch(script, /const family = route\?\.model_family \|\| '待映射';/);
  assert.doesNotMatch(script, /未完成映射的模型会单独归入“待映射”/);
});

test('model sync saves the routing draft without an unconditional gateway restart', () => {
  const syncStart = script.indexOf("if (action === 'sync-api-models')");
  const syncEnd = script.indexOf("\n      if (action === 'repair-portal-login')", syncStart);
  assert.notEqual(syncStart, -1);
  assert.notEqual(syncEnd, -1);
  const syncBlock = script.slice(syncStart, syncEnd);
  assert.match(syncBlock, /正在请求供应商模型列表/);
  assert.match(syncBlock, /正在整理模型分类/);
  assert.match(syncBlock, /markRoutingDirty\(\{\s*autosave:\s*false\s*\}\)/);
  assert.match(syncBlock, /await saveRoutingDraft\(\{\s*silent:\s*false\s*\}\)/);
  assert.doesNotMatch(syncBlock, /await saveAll\(\)/);
});

test('model sync waits for an in-flight routing draft save before reporting success', () => {
  const saveStart = script.indexOf('async function saveRoutingDraft');
  const saveEnd = script.indexOf('\n    function defaultSupplier', saveStart);
  const saveBlock = script.slice(saveStart, saveEnd);
  assert.match(saveBlock, /if \(state\.routingSaving\) \{[\s\S]*state\.routingSaveQueued = true;[\s\S]*await new Promise/);
  assert.match(saveBlock, /return saveRoutingDraft\(\{ silent \}\)/);
  const syncStart = script.indexOf("if (action === 'sync-api-models')");
  const syncEnd = script.indexOf("if (action === 'repair-portal-login')", syncStart);
  const syncBlock = script.slice(syncStart, syncEnd);
  assert.match(syncBlock, /persistedBindingCount = bindingsForApiProfile\(profile\.id\)/);
  assert.match(syncBlock, /同步绑定未完整落盘/);
});
