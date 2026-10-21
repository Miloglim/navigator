const fs = require('fs');
const assert = require('assert');

const source = fs.readFileSync('web/index.html', 'utf8');
const flatStart = source.indexOf('function salesFlatDetailHtml');
const flatEnd = source.indexOf('function renderSalesTable', flatStart);
const flat = source.slice(flatStart, flatEnd);
assert.ok(flatStart >= 0 && flatEnd > flatStart);
assert.ok(flat.indexOf("imagesBlock(r.images,120)") < flat.indexOf("fieldValue('remark',r.remark)"));
assert.ok(flat.indexOf('<span class="lbl">入库时间</span>') < flat.indexOf('<span class="lbl">消息时间</span>'), 'space detail puts entry time after the primary message-time column');

const tableStart = source.indexOf('function renderSalesTable');
const tableEnd = source.indexOf('function highlightPorts', tableStart);
const table = source.slice(tableStart, tableEnd);
assert.match(table, /<th>消息时间<\/th>/, 'flat sales table labels the primary timestamp as message time');
assert.match(table, /data-label="消息时间"[^\n]*salesTableTimeHtml\(r\.message_time\)/, 'space summary renders message time');
assert.doesNotMatch(table, /data-label="入库"[^\n]*salesTableTimeHtml\(r\.synced_at\)/, 'space summary no longer renders entry time');

const groupedStart = source.indexOf('function detailRow');
const groupedEnd = source.indexOf('function rowByDid', groupedStart);
const grouped = source.slice(groupedStart, groupedEnd);
assert.ok(groupedStart >= 0 && groupedEnd > groupedStart);
assert.match(grouped, /termsBlock\+rawBlock\+srcBlock\+imgBlock\+remarkBlock/, 'rate detail shows the source message directly below route-service terms');
assert.ok(grouped.indexOf("kv('入库时间',displayMinuteTime(q.synced_at))") < grouped.indexOf("kv('消息时间',displayMinuteTime(q.message_time))"), 'rate detail puts entry time after the primary message-time column');

const foldedStart = source.indexOf('function renderSalesFolded');
const foldedEnd = source.indexOf('function detailRow', foldedStart);
const folded = source.slice(foldedStart, foldedEnd);
assert.match(folded, /<th>消息时间<\/th>/, 'grouped rate table labels the primary timestamp as message time');
assert.match(folded, /data-label="消息时间"[^\n]*salesTableTimeHtml\(q\.message_time\)/, 'rate summary renders message time');
assert.doesNotMatch(folded, /data-label="入库"[^\n]*salesTableTimeHtml\(q\.synced_at\)/, 'rate summary no longer renders entry time');

console.log('PASS: sales rate and space prioritize message time and place remarks below images.');
