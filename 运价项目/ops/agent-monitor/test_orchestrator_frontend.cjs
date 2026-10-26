// Isolated DOM execution: verifies group projection and polling without live data writes.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.text='';this.checked=false;this.value='';this.dataset={};this.parent=null;}
 get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}
 set textContent(v){this.text=String(v);this.children=[];}
 append(...ns){for(const n of ns){n.parent=this;this.children.push(n);}}
 replaceChildren(...ns){this.children=[];this.text='';this.append(...ns);}
 get firstChild(){return this.children[0]||null;}
 get nextSibling(){if(!this.parent)return null;return this.parent.children[this.parent.children.indexOf(this)+1]||null;}
 insertBefore(n,c){n.remove();n.parent=this;let i=c?this.children.indexOf(c):this.children.length;this.children.splice(i,0,n);}
 remove(){if(this.parent){const a=this.parent.children;a.splice(a.indexOf(this),1);this.parent=null;}}
}
const elements=new Map(),element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let requests=[],failure=false;
let detail={id:'batch1',lane:'group:test',mode:'production-batch',status:'NEEDS_REVIEW',created:123,tasks:[{stage:'reconcile',status:'HELD',error:'watermark retained'}],events:[],children:[{id:'msg1',lane:'message:test:source1',failed:1,waiting:1},{id:'msg2',lane:'message:test:source2',failed:0,waiting:0}]};
let health={worker_alive:true,production_cutover:true,queue:{messages:{FAILED:26}},groups:[{name:'测试群',run_id:'batch1',status:'needs_review',failed_messages:26,running_messages:0,followup:1}]};
const context=vm.createContext({Date,console:{error(){}},document:{getElementById:element,createElement:()=>new Element()},setInterval(){},fetch:async url=>{requests.push(url);return {ok:true,json:async()=>failure?{ok:false,error:'offline'}:{ok:true,data:url.includes('/run?')?detail:url.endsWith('/runs')?[detail]:health}};}});
const html=fs.readFileSync(__dirname+'/../freight-orchestrator/web/index.html','utf8');
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
(async()=>{
 await new Promise(r=>setImmediate(r));await vm.runInContext('refresh()',context);
 assert.equal(element('runs').children.length,1);assert.match(element('runs').textContent,/测试群.*有待核消息.*26 条证据待核/);
 assert.equal(element('children').children.length,1,'default hides completed source messages');
 assert.match(element('description').textContent,/水位.*其他新消息继续处理/);
 assert.ok(!requests.some(u=>u.endsWith('/runs')),'default never fetches the large history payload');
 const node=element('runs').firstChild;await vm.runInContext('refresh()',context);assert.equal(element('runs').firstChild,node,'poll retains group DOM');
 element('child-filter').value='all';vm.runInContext('renderDetail()',context);assert.equal(element('children').children.length,2);
 health.groups[0].status='processing';health.groups[0].running_messages=1;await vm.runInContext('refresh()',context);assert.match(element('title').textContent,/处理中/);
 failure=true;await vm.runInContext('refresh()',context);assert.match(element('connection').textContent,/连接中断.*自动重连/);assert.equal(element('runs').children.length,1);
 failure=false;element('show-tests').checked=true;await vm.runInContext('refresh()',context);assert.ok(requests.some(u=>u.endsWith('/runs')));
 console.log('PASS: one row per group, held evidence, default failure filter, compact polling, stable DOM, outage snapshot and optional history.');
})().catch(e=>{console.error(e);process.exitCode=1;});
