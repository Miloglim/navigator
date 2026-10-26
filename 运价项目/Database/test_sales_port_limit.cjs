const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/index.html', 'utf8');
function source(name) {
  const match = html.match(new RegExp('function ' + name + '\\([^\\n]*\\}'));
  assert.ok(match, 'missing function ' + name);
  return match[0];
}

assert.ok(html.includes('<span class="required-star" aria-hidden="true">*</span>起运港</label>'));
assert.ok(html.includes('<span class="required-star" aria-hidden="true">*</span>目的港</label>'));
assert.ok(!html.includes('最多 3 个'), 'helper copy is not rendered under the port labels');

const elements = {};
const element = id => elements[id] ||= { value: '', innerHTML: '', focus() {} };
const toasts = [];
const context = vm.createContext({
  SALES_PORT_FILTER_MAX: 3,
  S: { sales: { polTags: [], podTags: [], carrier: '', carrierTags: [] } },
  $: element,
  esc: value => String(value),
  normPortTok: value => String(value || '').trim(),
  normCarrierTok: value => String(value || '').trim(),
  clearRealtimeRequired() {},
  toast: (message, kind) => toasts.push([message, kind]),
});
vm.runInContext([source('_tags'), source('renderTags'), source('addTag')].join('\n'), context);

context.addTag('pol', 'Shanghai,Ningbo,Santos,Paranagua');
assert.deepEqual([...context.S.sales.polTags], ['Shanghai', 'Ningbo', 'Santos']);
assert.deepEqual(toasts.at(-1), ['起运港最多选择 3 个', 'error']);

context.addTag('pol', 'Ningbo, Shanghai');
assert.deepEqual([...context.S.sales.polTags], ['Shanghai', 'Ningbo', 'Santos'], 'duplicates do not consume the limit');

context.addTag('pod', 'Los Angeles,Long Beach,Oakland,Seattle');
assert.deepEqual([...context.S.sales.podTags], ['Los Angeles', 'Long Beach', 'Oakland']);
assert.deepEqual(toasts.at(-1), ['目的港最多选择 3 个', 'error']);

for (const carrier of ['MSK', 'CMA', 'COSCO', 'MSC']) context.addTag('carrier', carrier);
assert.deepEqual([...context.S.sales.carrierTags], ['MSK', 'CMA', 'COSCO', 'MSC'], 'carrier tags remain unrestricted');
assert.equal(context.S.sales.carrier, 'MSK,CMA,COSCO,MSC');

context.S.sales.polTags = ['A', 'B', 'C', 'D'];
context.renderTags('pol');
assert.deepEqual([...context.S.sales.polTags], ['A', 'B', 'C'], 'legacy state is trimmed on render');

console.log('PASS: POL and POD tags stop at three values; carrier tags remain unrestricted.');
