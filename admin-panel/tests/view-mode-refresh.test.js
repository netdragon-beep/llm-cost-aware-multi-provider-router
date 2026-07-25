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
