const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('web/studio.html', 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).filter(Boolean);
scripts.forEach((script, index) => new vm.Script(script, { filename: `studio-inline-${index}.js` }));

for (const marker of ['阻塞处置', '按指令重解析', '保存修正并重验', '判定噪声', '打开待审核台账']) {
  assert(html.includes(marker), `missing trace resolution control: ${marker}`);
}
assert(html.includes("postTraceAction('amend_result'"), 'structured result amendment action missing');
assert(html.includes("postTraceAction('mark_noise'"), 'noise disposition action missing');
assert(!html.includes('allChildren.slice(0, 3)'), 'message children must not be capped at three');
assert(html.includes('GRAPH_CHILD_ROW_LIMIT'), 'multi-column message layout missing');
assert(html.includes("PARTIAL_REVIEW_COMPLETE:'部分已送审'"), 'partial review status label missing');
assert(html.includes('.child-row>span:nth-child(2)'), 'message tree overflow handling missing');
assert(html.includes("(partial||rb.ok)?'INGESTED'"), 'partial output must not look like waiting');
assert(html.includes("state.roots.filter(needsReview)"), 'partial child review filter missing');
assert(html.includes("$('m-issues').textContent=state.roots.filter(needsReview).length"), 'partial child top metric missing');
assert(html.includes("n.kind==='message'?'MSG'"), 'message graph node type missing');
assert(html.includes('if(n.runId)b.onclick=()=>openRun(n.runId,detail.id)'), 'message graph drill-down missing');
assert(html.includes("back.textContent='返回群链路'"), 'message graph return action missing');
assert(html.includes("document.createTextNode(' / 条目链路')"), 'message hierarchy breadcrumb missing');
assert(html.includes('function rootChildNodes(root)'), 'tree expansion source must be independent from current detail');
assert(html.includes("const childNodes=root.id===state.selectedRoot?rootChildNodes(root):reviewSelected?rootChildNodes(reviewRoot):[]"), 'selected root and nested review batch must keep their child lists expanded');
assert(html.includes("b.setAttribute('aria-current',child.id===state.selectedRun?'true':'false')"), 'selected child semantics missing');
assert(html.includes('<style id="tree-selection">'), 'selected tree row highlight missing');
assert(html.includes('state.detail.display_status'), 'selected child must prefer current detail state over a stale summary');
assert(html.includes("const displayStatus=d.display_status||d.status"), 'run header must use the derived message state');
assert(html.includes("traceTerminalStyle.textContent='.trace-map .flow-node.COMPLETED"), 'completed and ingested graph node colors missing');
assert(html.includes("active:'处理中'"), 'mixed running/queued filter must not be labelled running');
assert(html.includes("viewRoot.display_status||viewRoot.status"), 'review-needed historical roots must use their derived review status');
assert(html.includes('root.review_root'), 'historical reviews must be nested under one group row');
assert(html.includes("review.className='root-review-link'"), 'historical review drill-down control missing');
assert(html.includes("const pullText=viewRoot.pull_time?'最近拉取 '"), 'production tree latest pull label missing');
for (const marker of ['分片编排', 'shard-board', 'shard-card', 'shard_offsets_seconds', '个分片 ·', '展开查看群名单']) {
  assert(html.includes(marker), `missing shard orchestration marker: ${marker}`);
}

const childSource = html.match(/function rootChildNodes\(root\)\{[\s\S]*?\n\}/);
assert(childSource, 'rootChildNodes function missing');
const treeContext = vm.createContext({state: {selectedRoot: 'root', selectedRun: 'child-2', detail: null}});
vm.runInContext(childSource[0], treeContext);
const childNodes = treeContext.rootChildNodes({id: 'root', children: [
  {id: 'child-1', lane: 'message:group:msg-1', failed: false, waiting: false},
  {id: 'child-2', lane: 'message:group:msg-2', failed: true},
]});
assert.deepEqual(Array.from(childNodes, node => [node.id, node.title, node.status]), [
  ['child-1', 'msg-1', 'DONE'],
  ['child-2', 'msg-2', 'FAILED'],
]);
assert(html.includes('renderWatermarkTimeline'), 'watermark timeline renderer missing');
assert(html.includes('上次水位'), 'watermark timeline start label missing');
assert(html.includes('root-row.working'), 'working group animation missing');
assert(html.includes('child-main'), 'readable child message summary missing');
assert(html.includes('display_label'), 'derived child completion/ingested label missing');
assert(html.includes('pull_time'), 'child pull time missing');
assert(!html.includes("row('节点 ID'"), 'raw node identifiers must not dominate inspector');
assert(html.includes('阶段 ${item.completed}/${item.task_count}'), 'trigger stage count label missing');
assert(html.includes('fmtShortDateTime(item.created,true)'), 'trigger time missing');
assert(html.includes('card.title=`${item.trigger_key}'), 'raw trigger key should be available only as tooltip');
assert(html.includes("postTraceAction('acknowledge_review'"), 'review acknowledgement action missing');
assert(html.includes("REVIEWED:'已处置'"), 'reviewed status label missing');
console.log('PASS: Trace Studio resolution controls and inline script');
