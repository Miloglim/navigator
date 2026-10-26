const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/index.html', 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).filter(Boolean);
scripts.forEach((script, index) => new vm.Script(script, { filename: `index-inline-${index}.js` }));

assert(html.includes('同源消息部分解析待核'), 'partial message warning is missing');
assert(html.includes('放行本组已通过（'), 'passed-only group approval action is missing');
assert(!html.includes('部分解析需逐条确认'), 'legacy forced-review copy must not remain');
assert(html.includes("const partial = !passed && (p.partial_review === true"), 'passed rows must not be marked as incomplete');
assert(html.includes("!passed ? ' disabled title=\"本条校验未过，修正后才能放行\"' : ''"), 'individual approval must only be disabled for failed rows');
console.log('PASS: partial message hint, passed-only row/group approval, and failed-row guard');
