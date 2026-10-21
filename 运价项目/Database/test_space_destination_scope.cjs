const fs = require('fs');
const assert = require('assert');

const source = fs.readFileSync('web/index.html', 'utf8');
assert.match(source, /data-ac="lane"[^>]*placeholder="航线或服务"/);
assert.match(source, /<th>目的范围<\/th>/);
assert.match(source, /const scopeField = r\.pod \? 'pod' : r\.route \? 'route' : 'service_name'/);
assert.match(source, /航线 · /);
assert.match(source, /服务 · /);
assert.match(source, /目的港、航线、航线服务至少填写一项，销售才能查到这条舱位/);
console.log('PASS: space destination scope is searchable and visible.');
