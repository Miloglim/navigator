const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');

assert.doesNotMatch(html, /id="adv-toggle"|id="adv-panel"|function toggleAdv\(|function updateAdvancedIndicator\(/);
assert.match(html, /<input id="sf-group" type="hidden"[^>]*data-ac="group"/);
assert.match(html, /function jumpGroup\(g,kind,scope=\[\]\)/);
assert.match(html, /S\.sales\.appliedQuery=\{tab,params:new URLSearchParams\(p\)\}/);

console.log('Advanced query control removed; internal group jump and quote context retained');
