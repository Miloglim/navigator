// Behavioral regressions using the shipped module with disposable DOM/fetch adapters.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/admin-terminal.js','utf8');
const stylesheet=fs.readFileSync('web/admin-terminal.css','utf8');
const indexHtml=fs.readFileSync('web/index.html','utf8');
function fixture(){
  const elements=new Map();
  const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',value:'old-filter',classList:{add(){},remove(){},toggle(){}},addEventListener(){},before(){},replaceChildren(){},remove(){}});return elements.get(id);};
  const pending=[];const roles=[];const profileCalls=[];
  const context={window:{},document:{getElementById:element,querySelectorAll:()=>[],createComment:()=>({}),activeElement:null},
    S:{auth:{role:'admin',user_id:'admin'},role:'account',accountTab:'system'},
    URLSearchParams,DOMException,console,setTimeout,clearTimeout,
    fetch:url=>new Promise(resolve=>pending.push({url,resolve:data=>resolve({ok:true,json:async()=>({ok:true,data})})})),
    switchRole:role=>{roles.push(role);context.S.role=role;},loadAccountProfile:async()=>{profileCalls.push("profile");},loadGrowth:async()=>{profileCalls.push("growth");},toast(){}};
  vm.createContext(context);
  // Test-only access to closures; production public surface is unchanged.
  vm.runInContext(source.replace('return {enter:module=>','window.testHooks={enter,navigate,loadEvents,loadList,loadApiMonitor,loadRuntimeHealth,getState:()=>state};return {enter:module=>'),context);
  return {context,element,pending,roles,profileCalls,h:context.window.testHooks};
}
async function run(){
  const summary={dau:2,daily:[{page_opens:5,searches:3,zero_result_searches:1}]};
  const timeline={rows:[],total:0};
  {
    const order=["['profile','我的账号']","['overview','管理概览']","['people','人员与账号']","['roles','角色与权限']","['publishers','报价发布人']","['events','安全与操作记录',true]","['runtime','运行监控',true]"];
    for(let i=1;i<order.length;i++)assert.ok(source.indexOf(order[i-1])<source.indexOf(order[i]),'admin navigation order');
    assert.match(source,/at-nav-separate/);
    assert.doesNotMatch(source,/<th>涉及航线<\/th>/);
    assert.doesNotMatch(source,/<h3>实际涉及航线<\/h3>/);
    assert.doesNotMatch(source,/人员与工作资料/);
    assert.doesNotMatch(source,/experience:'业务经验'/);
    assert.match(source,/data-at-action="rename_login"/);
    assert.match(source,/activityTimeline:'\/api\/admin\/activity\/timeline'/);
    assert.match(source,/apiEvents:'\/api\/admin\/monitor\/api-events'/);
    assert.match(source,/runtimeHealth:'\/api\/admin\/monitor\/health'/);
    assert.match(source,/id="at-event-search"/);
    assert.match(source,/id="at-event-days"/);
    assert.match(source,/id="at-event-category"/);
    assert.match(stylesheet,/\.at-toolbar \.at-check input\[type="checkbox"\]\{flex:none;min-width:0;width:14px;height:14px;margin:0;padding:0\}/);
    for(const label of ['访问记录','业务查询','报价表','账号安全','管理操作'])assert.match(source,new RegExp(label));
    assert.match(source,/cell_edited:'编辑报价字段'/);
    assert.match(indexHtml,/\/api\/activity\/quote-usage/);
    assert.match(indexHtml,/Telemetry must never interrupt local quotation editing/);
    assert.match(indexHtml,/\(\(t === 'system' \|\| t === 'publishers'\) && S\.auth\.role !== 'admin'\)/);
    assert.match(indexHtml,/const allowedTabs = \['profile', \.\.\.\(S\.auth\?\.role === 'admin' \? \['system', 'publishers'\] : \[\]\)\]/);
    assert.match(indexHtml,/async function loadPublisherAccounts\(\) \{\s+if \(S\.auth\?\.role !== 'admin'\) return;/);
  }
  {
    const f=fixture();const loading=f.h.navigate('runtime');
    assert.equal(f.roles.length,0);assert.equal(f.pending.length,2,'runtime defaults to the user-activity tab');
    f.pending[0].resolve(summary);f.pending[1].resolve(timeline);await loading;
    assert.match(f.element('at-main').innerHTML,/运行监控/);
  }
  {const f=fixture();await f.h.navigate('settings');assert.equal(f.roles.length,0);assert.equal(f.pending.length,0);}
  {
    const f=fixture();const slow=f.h.navigate('events');await f.h.navigate('roles');
    const expected=f.element('at-main').innerHTML;
    assert.equal(f.pending.length,2,'events page loads summary and unified timeline');
    f.pending[0].resolve(summary);f.pending[1].resolve(timeline);await slow;
    assert.equal(f.element('at-main').innerHTML,expected,'late event response must not replace roles');
  }
  {
    const f=fixture();f.h.getState().module='events';
    const first=f.h.loadEvents();f.h.getState().eventsOffset=20;const second=f.h.loadEvents();
    assert.equal(f.pending.length,4,'each timeline load makes two requests');
    f.pending[2].resolve(summary);f.pending[3].resolve({...timeline,total:22});await second;
    const expected=f.element('at-main').innerHTML;
    f.pending[0].resolve(summary);f.pending[1].resolve({...timeline,total:99});await first;
    assert.equal(f.element('at-main').innerHTML,expected,'old page must not overwrite newer event page');
  }
  {
    const f=fixture();f.h.getState().module='events';const loaded=f.h.loadEvents();
    f.pending[0].resolve(summary);f.pending[1].resolve({rows:[{event_id:'act_1',source:'activity',category:'query',action:'sales_search',user_id:'usr_sales',display_name:'销售测试',role:'sales',actor_name:'销售测试',details:{pod:'ECPOS'},result_count:0,client_ip:'10.0.0.8',created_at:'2026-10-10T09:30:00+08:00'}],total:1});await loaded;
    const output=f.element('at-main').innerHTML;
    assert.match(output,/今日活跃/);assert.match(output,/业务查询/);assert.match(output,/ECPOS/);assert.match(output,/无结果/);assert.match(output,/10\.0\.0\.8/);
  }
  {
    const f=fixture();const state=f.h.getState();
    const first=f.h.navigate('events');f.pending[0].resolve(summary);f.pending[1].resolve(timeline);await first;
    assert.match(f.element('at-main').innerHTML,/id="at-event-hide-admin"/);
    state.eventsOffset=20;
    f.element('admin-terminal-root').onchange({target:{id:'at-event-hide-admin',checked:true}});
    assert.equal(state.eventsOffset,0);
    assert.match(f.pending[2].url,/hide_admin=1/);
    assert.match(f.pending[3].url,/hide_admin=1/);
    f.pending[2].resolve(summary);f.pending[3].resolve(timeline);
    await new Promise(setImmediate);
    assert.match(f.element('at-main').innerHTML,/id="at-event-hide-admin" checked/);
    state.module='runtime';state.runtimeTab='users';
    const second=f.h.loadEvents();
    assert.match(f.pending[4].url,/hide_admin=1/);
    assert.match(f.pending[5].url,/hide_admin=1/);
    f.pending[4].resolve(summary);f.pending[5].resolve(timeline);await second;
    assert.match(f.element('at-main').innerHTML,/隐藏管理员记录/);
  }
  {
    const f=fixture();const state=f.h.getState();state.module='runtime';state.runtimeTab='api';
    const loaded=f.h.loadApiMonitor();assert.equal(f.pending.length,2);
    f.pending[0].resolve({requests:2,failures:1,users:1,average_ms:34});
    f.pending[1].resolve({rows:[{request_id:'r1',created_at:'2026-10-15T09:30:00+08:00',method:'POST',path:'/api/pending/approve',status_code:500,duration_ms:34,user_id:'usr_sales',role:'sales',display_name:'销售测试',client_ip:'10.0.0.8'}],total:1});await loaded;
    const output=f.element('at-main').innerHTML;
    assert.match(output,/用户行为/);assert.match(output,/接口调用/);assert.match(output,/POST/);assert.match(output,/api\/pending\/approve/);assert.match(output,/500/);assert.match(output,/失败调用/);
  }
  {
    const f=fixture();const state=f.h.getState();state.module='runtime';state.runtimeTab='health';
    const loaded=f.h.loadRuntimeHealth();f.pending[0].resolve({status:'ok',service:'online',uptime_seconds:3660,database:{connected:true,engine:'postgresql',probe_ms:12}});await loaded;
    const output=f.element('at-main').innerHTML;
    assert.match(output,/服务与数据库/);assert.match(output,/连接正常/);assert.match(output,/PostgreSQL/);assert.match(output,/1小时/);
  }
  {
    const f=fixture();await f.h.navigate('profile');
    assert.deepEqual(f.profileCalls,['profile','growth'],'admin profile must load growth after mounting the card');
  }
  {
    const f=fixture();f.h.getState().module='events';await f.h.enter();
    assert.equal(f.h.getState().module,'profile','opening account management must default to my account');
    assert.deepEqual(f.profileCalls,['profile','growth']);
  }
  console.log('PASS: navigation order/divider, simplified account file, rename control, stale requests');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
