const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync('web/index.html', 'utf8');
assert(source.includes('id="sales-source-local-head"'));

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    hidden: false,
    textContent: '',
    attributes: {},
    classList: { toggle() {} },
    setAttribute(key, value) { this.attributes[key] = value; },
  });
  return elements.get(id);
}

const context = {
  $: element,
  sessionStorage: { getItem() { return '1'; }, setItem() {} },
};
vm.createContext(context);
const functions = source.slice(
  source.indexOf('function salesSourceCollapseKey'),
  source.indexOf('function syncCompanySourceControls'),
);
vm.runInContext(functions, context);

context.updateLocalSourceHeader('dyn', null, '已完成');
assert.equal(element('sales-source-local-head').hidden, true);
assert.equal(element('sales-source-local-body').hidden, false);

context.updateLocalSourceHeader('group', null, '已完成');
assert.equal(element('sales-source-local-head').hidden, true);
assert.equal(element('sales-source-local-body').hidden, false);

context.updateLocalSourceHeader('rates', 3, '已完成');
assert.equal(element('sales-source-local-head').hidden, false);
assert.equal(element('sales-source-local-title').textContent, '钉钉群聊运价');

console.log('sales local source header tests passed');
