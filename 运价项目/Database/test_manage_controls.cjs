const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/index.html', 'utf8');
const match = html.match(/function switchManage\([^]*?\n}/);
assert.ok(match, 'switchManage source exists');

const typeButtons = [button(), button()];
const viewButton = button(true);
const selectors = [];
const context = vm.createContext({
  mgKind: 'rates',
  mgPage: 4,
  document: {
    querySelectorAll(selector) {
      selectors.push(selector);
      return selector === '[data-mk]' ? typeButtons : [viewButton];
    },
  },
  initManageFilters() {},
  loadManage() {},
});

function button(active = false) {
  return {
    active,
    classList: {
      toggle(name, enabled) {
        assert.equal(name, 'active');
        this.owner.active = enabled;
      },
      owner: null,
    },
  };
}
for (const item of [...typeButtons, viewButton]) item.classList.owner = item;

vm.runInContext(match[0], context);
context.switchManage('space', typeButtons[1]);

assert.equal(context.mgKind, 'space');
assert.equal(context.mgPage, 1);
assert.deepEqual(selectors, ['[data-mk]']);
assert.equal(typeButtons[0].active, false);
assert.equal(typeButtons[1].active, true);
assert.equal(viewButton.active, true, 'type switch must preserve view selection');
console.log('PASS: manage type switch preserves independent view state');
