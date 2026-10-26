// Isolated DOM and API harness: never calls a live service or business database.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.value = ''; this.text = ''; this.attrs = {}; }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1] || null; }
  append(...nodes) { for (const n of nodes) { n.remove(); n.parent = this; this.children.push(n); } }
  replaceChildren(...nodes) { for (const n of this.children) n.parent = null; this.children = []; this.text = ''; this.append(...nodes); }
  insertBefore(n, cursor) { n.remove(); n.parent = this; this.children.splice(cursor ? this.children.indexOf(cursor) : this.children.length, 0, n); }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] || null; }
  addEventListener() {}
}
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const now = Date.now() / 1000;
const sample = {run_id:'A',task_name:'Test A',status:'running',health:'running',started_at:now-60,seen_at:now,activity_at:now,signal_age_seconds:0,activity_age_seconds:0,control_ready:true,source:'pipeline',activity:'Actual step'};
let outage = false, posts = [];
const eventRows = [{seq:1,at:now,source:'pipeline',activity:'Actual step'}];
const context = vm.createContext({document:{getElementById:element,createElement:()=>new Element()},console:{error(){}},setTimeout,clearTimeout,setInterval(){},AbortController,sessionStorage:{getItem(){return null},setItem(){}},fetch:async (url,opts) => {
  if(outage) throw Error('isolated outage');
  if(opts.method==='POST') posts.push(JSON.parse(opts.body));
  return {ok:true,json:async()=>({ok:true,data:url==='/api/runs'?[sample]:url.startsWith('/api/events')?eventRows:{action:opts.method==='POST'?posts.at(-1).action:posts.at(-1)?.action||'resume'}})};
}});
const html = fs.readFileSync(__dirname+'/web/index.html','utf8');
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
const run = code => vm.runInContext(code,context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('pause').disabled,false);
  const button = element('runs').children[0], event = element('timeline').children[0];
  await run('refresh()');
  assert.equal(element('runs').children[0],button,'polling retains task nodes');
  assert.equal(element('timeline').children[0],event,'polling retains event nodes');
  eventRows.unshift({seq:2,at:now+1,source:'agent',activity:'Next actual step'});
  await run('refresh()');
  assert.equal(element('timeline').children.length,2,'new event appears on next poll');
  assert.equal(element('timeline').children[1],event,'old event is retained when prepending');
  element('event-source').value='agent';run('renderEvents()');
  assert.equal(element('timeline').children.length,1,'event source filter');
  element('event-source').value='';run('renderEvents()');
  element('search').value='no matching task';
  run('renderRuns();renderDetail();renderEvents()');
  assert.equal(element('pause').disabled,true);
  assert.equal(element('details').children.length,0);
  element('search').value='';await run('refresh()');
  assert.equal(element('details').children.length,9,'details restore after empty filter');
  const restoredEvent = element('timeline').children[0];
  outage=true;await run('refresh()');
  assert.equal(element('offline-note').hidden,false);
  assert.equal(element('pause').disabled,true);
  assert.equal(element('timeline').children[0],restoredEvent,'outage retains last timeline');
  outage=false;await run('refresh()');
  assert.equal(element('offline-note').hidden,true);
  sample.health='disconnected';await run('refresh()');
  assert.equal(element('pause').disabled,true,'stale task cannot be controlled');
  sample.health='running';sample.completed=0;sample.check_failed=undefined;await run('refresh()');
  assert.equal(element('details').children[6].children[1].textContent,'0');
  assert.equal(element('details').children[7].children[1].textContent,'尚未上报');
  await run('control("pause")');
  assert.deepEqual(posts[0],{run_id:'A',action:'pause'});
  assert.equal(element('resume').disabled,false);
  assert.equal(element('pause').disabled,true);
  await run('control("resume")');
  assert.equal(posts.at(-1).action,'resume');
  sample.status='completed';sample.health='completed';await run('refresh()');
  assert.equal(element('resume').disabled,true);
  assert.equal(posts.length,2,'controls were tested only against isolated API');
  console.log('Frontend checks passed: stable polling, filters, unknown counts, outage recovery, control gates.');
})().catch(e=>{console.error(e);process.exitCode=1});
