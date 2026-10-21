// Isolated dashboard checks: no auth, server or database writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
function source(name) {
  const compact = html.match(new RegExp('(?:async )?function ' + name + '\\([^\n]*\\}'));
  if (compact) return compact[0];
  const re = new RegExp('(?:async )?function ' + name + '\\([^]*?\\n}');
  const m = html.match(re); assert.ok(m, name); return m[0];
}
const elements = {};
const el = id => elements[id] ||= {value:'',innerHTML:'',textContent:'',dataset:{},classList:{toggle(){}},addEventListener(type,fn){this[type]=fn;}};
el('pd-status').value='awaiting';
const calls=[];
const ctx = vm.createContext({S:{auth:{role:'admin'}},opsOwnership:()=> 'all',opsUrl:url=>url,console, CSS:{escape:String},
  esc: s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  $:el, document:{querySelector(){return el('layout');},querySelectorAll(){return [];}},
  localStorage:{setItem(k,v){calls.push(['store',k,v]);}},
  fmtValidRange:(a,b)=>[a,b].join(' / '),fmtEtd:s=>s,dispName:s=>s,
  fmtTime:s=>s,pdConfBadge:()=>'',groupConfRank:()=>0,pdImagesBlock:()=>'',carrierWebsite:s=>s,
  PD_SAVE_PENDING:{},collectCardFields:()=>({}),PD_VIEW_KEY:'view',PD_VIEW_SOURCE:'source',PD_VIEW_ENTRIES:'entries',PD_PAGE:50,PD_SOURCE_PAGE:10,
  PD:{kind:'',byid:{},open:{},snap:{},sync:{},view:'entries',page:3},
  CTYPE_UI_ORDER:['20GP','40HQ/HC','40NOR','45HQ/HC'],
  pdFlushSave:async id=>{calls.push(['flush',id]);return true;},
  pdBindLive(){},pdEditRow(){},pdPaintCur(){},pdFocusAfterRender(){},
  renderHintsBar:async()=>{},renderPdPager:n=>calls.push(['pager',n]),pdBulkCount(){},refreshQueueDots(){},
  loadReviewIssues:async()=>{},
  showGrowthFeedback(){},
  approveRow:id=>calls.push(['approve',id]),rejectPending:id=>calls.push(['reject',id]),pdSwitchKind(){},
});
for (const fn of ['nonUsdPriceCurrency','nonUsdPricePlaceholder','has45HighCube','quote45CornerHtml','pdFitTables','addOverflowTitles','opsIdParams','opsIdSearch','opsIdClear','idChip','idCode','pdConfRank','pdGroupCellsHtml','pdEntryCellsHtml','pdEntriesHtml','pdCardHtml','pdGroupCardHtml','pdSourceView','pdPageSize','renderPdPager','pdViewControls','pdSaveBeforeDisplayChange','pdSetView','loadPending','pdBindGroup','renderPending']) vm.runInContext(source(fn),ctx);
const record = (id,status='passed',review='awaiting')=>({id,entry_id:'frt_'+id,kind:'rates',check_status:status,review_status:review,errors:[],payload:{'消息ID':'same','起运港':'A','目的港':'B','柜型':'40HQ/HC','海运费USD':id===1?0:1900,'来源群':'<img src=x onerror=alert(1)>'}});
for (const fn of ['pdNormCtype','pdRatesByRow']) vm.runInContext(source(fn),ctx);
const products = ['Ipanema','Carioca','Santana'].map((service,i)=>{
  const r=record(i+10); r.payload['航线服务']=service; return r;
});
for (const terminal of ['PUERTO SEGURO FLUVIAL','TERPORT VILLETA']) {
  const r=record(products.length+10);r.payload['目的码头']=terminal;products.push(r);
}
assert.equal(Object.keys(ctx.pdRatesByRow(products)).length,5,'source view preserves same-port service and terminal products');
assert.equal(Object.values(ctx.pdRatesByRow(products)).reduce((n,r)=>n+Object.keys(r.cabs).length,0),5,'no container cell overwritten');
const emptyContainer = record(90,'failed'); emptyContainer.payload['柜型']='';
const emptyContainerGroup = Object.values(ctx.pdRatesByRow([emptyContainer]))[0];
assert.deepEqual(Array.from(emptyContainerGroup.extras || [],r=>r.id),[90],'unrecognized container rows stay addressable outside fixed container cells');
assert.equal(Object.keys(emptyContainerGroup.cabs).length,0,'empty container cannot occupy a normal quote slot');
emptyContainer._gkey='same'; emptyContainer.group={total:1,bad:1};
const emptyContainerHtml=ctx.pdGroupCardHtml([emptyContainer]);
assert.ok(emptyContainerHtml.includes('class="pd-unresolved-row"'),'empty container renders as an explicit unresolved row');
assert.ok(emptyContainerHtml.includes('data-ract="delete" data-rid="90"'),'empty container row exposes single-record delete');
assert.ok(!emptyContainerHtml.includes('title="点击添加 20GP 价格"'),'empty container row no longer impersonates an empty quote matrix');
const childHeaderSource=source('pdCardHtml');
assert.ok(childHeaderSource.includes('pd-child-head'),'expanded entry uses a subordinate header style');
assert.ok(childHeaderSource.includes('pd-child-kind'),'entry type control has a stable width hook');
assert.ok(childHeaderSource.includes('pd-child-title'),'entry identity has a stable width hook');
assert.ok(childHeaderSource.includes('idChip(idCode(r), idCode(r))'),'entry identity remains copyable after moving into the child header');
(async()=>{
  el('pd-id-query').value='  组：shared % / ?  ';
  assert.equal(ctx.opsIdParams('pd').id_query,'组：shared % / ?');
  el('pd-id-query').value='frt_test';
  assert.equal(ctx.opsIdParams('pd').id_query,'frt_test');
  el('pd-id-query').value='';

  const rows=[record(1),record(2,'failed'),record(3,'passed','approved')];
  ctx.fetch=async url=>{calls.push(['fetch',url]);return {json:async()=>ctx.PD.view==='source'?{ok:true,rows,total:1,record_total:3,pagination_unit:'groups'}:{ok:true,rows,total:103}};};
  const snapshot=JSON.stringify(rows.map(r=>[r.payload,r.check_status,r.review_status]));
  await ctx.renderPending({ok:true,total:103,rows});
  const output=el('pd-cards').innerHTML;
  assert.equal((output.match(/class="pd-entry-row"/g)||[]).length,3,'same container/source records remain independent');
  assert.ok(output.includes('0<small>USD</small>'),'zero is not missing');
  assert.ok(output.includes('&lt;img'),'source escaped');
  assert.ok(!output.includes('<img src=x'),'source cannot inject markup');
  const variant = record(4);
  variant.payload['航线服务'] = 'Ipanema';
  variant.payload['目的码头'] = '<Terminal>';
  assert.ok(ctx.pdEntryCellsHtml(variant).includes('服务：Ipanema'));
  assert.ok(ctx.pdEntryCellsHtml(variant).includes('码头：&lt;Terminal&gt;'));
  assert.ok(ctx.pdEntryCellsHtml(rows[1]).includes('disabled title="校验通过后才能放行"'));
  assert.ok(!ctx.pdEntryCellsHtml(rows[2]).includes('data-ract="approve"'));
  assert.ok(ctx.pdGroupCellsHtml(rows[1]).includes('data-ract="delete"'),'merged view exposes row delete for an empty or invalid pending row');
  assert.ok(!ctx.pdGroupCellsHtml(rows[2]).includes('data-ract="delete"'),'approved history stays protected from row delete');
  assert.equal(snapshot,JSON.stringify(rows.map(r=>[r.payload,r.check_status,r.review_status])),'display preserves business state');
  assert.equal(el('pd-count').textContent,'共 103 条 · 本页 3 条');
  ctx.PD.open={1:1}; ctx.PD.page=3;
  ctx.pdGroupCardHtml=()=>'<div>source</div>';
  await ctx.pdSetView('source');
  assert.equal(ctx.PD.page,1,'new pagination unit starts at first page');
  assert.ok(calls.find(c=>c[0]==='flush'),'saves settle before repaint');
  assert.equal(ctx.PD.view,'source');
  assert.ok(el('pd-cards').innerHTML.includes('source'));
  assert.equal(el('pd-count').textContent,'共 3 条 / 1 组');
  await ctx.pdSetView('entries');
  assert.equal((el('pd-cards').innerHTML.match(/class="pd-entry-row"/g)||[]).length,3);
  el('pd-gedit-1').dataset.viewBaseline='{}';
  const flushes=calls.filter(c=>c[0]==='flush').length;
  await ctx.pdSetView('source');
  assert.equal(calls.filter(c=>c[0]==='flush').length,flushes,'unchanged editor never forces save');
  ctx.PD.open={}; ctx.PD.kind='group_suggestion';ctx.pdViewControls();assert.equal(el('pd-view-tabs').hidden,true);assert.equal(el('pd-view-control').hidden,true);
  ctx.PD.kind='';el('pd-status').value='rejected';ctx.pdViewControls();assert.equal(el('pd-view-tabs').hidden,true);assert.equal(el('pd-view-control').hidden,true);
  el('pd-status').value='awaiting';el('pd-check').value='failed';
  el('pd-id-query').value='组:shared %';
  ctx.fetch=async url=>{calls.push(['fetch',url]);return {json:async()=>({ok:true,rows:[],total:103})};};
  ctx.PD.page=3;
  await ctx.loadPending();
  const url=calls.filter(c=>c[0]==='fetch').at(-1)[1];
  assert.ok(url.includes('id_query='+encodeURIComponent('组:shared %')),'encoded exact identity reaches API');
  assert.ok(url.includes('limit=10') && url.includes('offset=20') && url.includes('view=source') && url.includes('check=failed'),'source view pages groups');
  assert.ok(el('pd-pager').innerHTML.includes('第 3 / 11 页'));
  ctx.PD.view='entries';await ctx.loadPending();
  const entryUrl=calls.filter(c=>c[0]==='fetch').at(-1)[1];
  assert.ok(entryUrl.includes('limit=50') && entryUrl.includes('offset=100') && !entryUrl.includes('view=source'));
  assert.ok(el('pd-pager').innerHTML.includes('第 3 / 3 页'));
  const rb={getAttribute:k=>k==='data-ract'?'approve':'2'};
  const t={closest:s=>s==='[data-ract]'?rb:s==='[data-entry-id]'?{dataset:{entryId:'2'}}:null};
  el('pd-cards').click({target:t});
  assert.deepEqual(calls.filter(c=>c[0]==='approve'),[['approve',2]],'only clicked row dispatched');
  vm.runInContext(source('pdRejectAll'),ctx);
  ctx.ensureToken=async()=>{};ctx.requireToken=()=>true;
  ctx._reviewer=()=> 'test-reviewer';ctx.pdAfterAct=()=>{};
  ctx.toast=(...args)=>calls.push(['toast',...args]);
  ctx.pdRejectionReason=async()=> '目的港需核对';
  ctx.PD.groups={source:rows};
  ctx.postJSON=async(url,body)=>{calls.push(['post',url,JSON.parse(JSON.stringify(body))]);return {ok:true,rejected:[1,2],skipped:[]};};
  await ctx.pdRejectAll('source');
  const rejection=calls.filter(c=>c[0]==='post').at(-1);
  assert.equal(rejection[1],'/api/pending/reject_group');
  assert.deepEqual(rejection[2].ids,[1,2],'all awaiting rows including failed, excluding approved');
  assert.equal(rejection[2].group_key,undefined,'no first-row group key');
  const posts=calls.filter(c=>c[0]==='post').length;
  ctx.pdRejectionReason=async()=>null;await ctx.pdRejectAll('source');
  vm.runInContext(source('rejectPending'),ctx);
  ctx.pdRejectionReason=async()=> '原图无NOR报价，误生成条目，不重建';
  await ctx.rejectPending(1413);
  const singleRejection=calls.filter(c=>c[0]==='post').at(-1);
  assert.equal(singleRejection[1],'/api/pending/reject');
  assert.equal(singleRejection[2].id,1413,'single rejection never expands to source group');
  assert.equal(singleRejection[2].note,'原图无NOR报价，误生成条目，不重建');
  const singlePosts=calls.filter(c=>c[0]==='post').length;
  ctx.pdRejectionReason=async()=>null;await ctx.rejectPending(1413);
  const rejectionSource=source('pdRejectionReason');
  assert.ok(rejectionSource.includes('可选') && rejectionSource.includes('可留空'),'rejection reason is optional');
  const rejectEvents={input:{},form:{},dialog:{}};
  let enterPrevented=false;
  const rejectInput={value:'  目的港需核对  ',focus(){},addEventListener(type,fn){rejectEvents.input[type]=fn;}};
  const rejectForm={
    addEventListener(type,fn){rejectEvents.form[type]=fn;},
    requestSubmit(){rejectEvents.form.submit({preventDefault(){}});}
  };
  const rejectDialog={
    className:'',innerHTML:'',removed:false,
    setAttribute(){},showModal(){},remove(){this.removed=true;},
    addEventListener(type,fn){rejectEvents.dialog[type]=fn;},
    querySelector(selector){
      if(selector==='h2') return {textContent:''};
      if(selector==='input') return rejectInput;
      if(selector==='form') return rejectForm;
      if(selector==='button[type="button"]') return {addEventListener(){}};
      return null;
    }
  };
  const rejectCtx=vm.createContext({document:{createElement:()=>rejectDialog,body:{appendChild(){}}}});
  vm.runInContext(source('pdRejectionReason'),rejectCtx);
  const rejectPromise=rejectCtx.pdRejectionReason('驳回原因');
  rejectEvents.input.keydown({key:'Enter',preventDefault(){enterPrevented=true;}});
  assert.equal(await rejectPromise,'目的港需核对','Enter confirms the trimmed single-line reason');
  assert.equal(enterPrevented,true,'Enter cannot create a newline');
  assert.equal(rejectDialog.removed,true,'confirmed dialog closes');
  const bulkButton={disabled:false,textContent:''};
  const bulkUrls=[];
  const bulkCtx=vm.createContext({opsUrl:url=>url,console:{error(){}},$:()=>bulkButton,fetch:async url=>{
    bulkUrls.push(url);
    const kind=new URL(url,'http://fixture').searchParams.get('kind');
    return {json:async()=>({ok:true,total:kind==='rates'?20:kind==='space'?8:99})};
  }});
  vm.runInContext(source('pdBulkCount'),bulkCtx);
  await bulkCtx.pdBulkCount();
  assert.equal(bulkButton.textContent,'一键放行整组已通过 (28 条)');
  assert.equal(bulkButton.disabled,false);
  assert.equal(bulkUrls.length,2,'only rates and space count; group suggestions excluded');
  assert.ok(bulkUrls.every(url=>new URL(url,'http://fixture').searchParams.get('whole_passed')==='1'),'count uses complete-source gate');
  bulkCtx.fetch=async()=>({json:async()=>({ok:false,error:'offline'})});
  await bulkCtx.pdBulkCount();
  assert.equal(bulkButton.disabled,true,'failed counts cannot leave an actionable stale button');
  ctx.PD_FAILED_GROUP_COPY_PAGE_SIZE=2;
  vm.runInContext(source('pdCopyFailedGroupIds'),ctx);
  let copiedFailedGroups='';
  const failedGroupUrls=[];
  ctx.opsUrl=url=>url+'&ownership=all';
  ctx.writeClipboardText=async text=>{copiedFailedGroups=text;};
  ctx.fetch=async url=>{
    failedGroupUrls.push(url);
    const offset=Number(new URL(url,'http://fixture').searchParams.get('offset'));
    const rows=offset===0
      ? [{payload:{'消息ID':'msgA=='}},{payload:{'消息ID':'msgA=='}},{payload:{'消息ID':'msgB+/=='}}]
      : [{payload:{'消息ID':'msgC=='}},{payload:{}}];
    return {json:async()=>({ok:true,total:4,rows})};
  };
  await ctx.pdCopyFailedGroupIds();
  assert.equal(copiedFailedGroups,'组:msgA==\n组:msgB+/==\n组:msgC==','copies unique failed source IDs one per line');
  assert.equal(failedGroupUrls.length,2,'copies failed groups across every source page');
  assert.ok(failedGroupUrls.every(url=>url.includes('status=awaiting')&&url.includes('check=failed')&&url.includes('view=source')),'copy uses failed source-group semantics');
  assert.ok(failedGroupUrls.every(url=>url.includes('ownership=all')),'copy respects current ownership scope');
  assert.ok(calls.filter(c=>c[0]==='toast').at(-1).slice(1).join(' ').includes('跳过 1 组'),'copy reports groups without message IDs');
  const issueEls={'pd-review-issues':{open:false},'pd-review-issues-new':{hidden:true,textContent:''},'pd-review-issues-read':{hidden:true}};
  const issueStorage={};
  const issueRows=[{
    source_group:'<script>',message_id:'msg-1',message_text:'<img onerror=x>',
    reasons:['<bad>'],images:[],updated_at:'2026-10-13 08:00:00'
  }];
  const issueCtx=vm.createContext({
    $:id=>issueEls[id] ||= {textContent:'',innerHTML:''},
    S:{auth:{user_id:'usr-reviewer'}},PD:{reviewIssueRows:[]},REVIEW_ISSUE_RESOLVE_CONCURRENCY:4,esc:ctx.esc,imagesBlock:()=>'',toast(){},
    localStorage:{getItem:key=>issueStorage[key]||null,setItem:(key,value)=>{issueStorage[key]=value;}},
    fetch:async()=>({json:async()=>({ok:true,data:{total:1,rows:issueRows}})}),
    postJSON:async(path,body)=>{calls.push(['issue-post',path,body]);return {ok:true,data:{resolved:true}};}
  });
  for (const fn of ['reviewIssueSeenKey','reviewIssueIdentity','loadReviewIssueSeen','saveReviewIssueSeen','reviewIssueNewRows']) vm.runInContext(source(fn),issueCtx);
  vm.runInContext(source('loadReviewIssues'),issueCtx);
  vm.runInContext(source('pdMarkAllReviewIssuesRead'),issueCtx);
  vm.runInContext(source('resolveReviewIssue'),issueCtx);
  await issueCtx.loadReviewIssues();
  assert.equal(issueEls['pd-review-issues-title'].textContent,'解析异常 · 1 条');
  assert.equal(issueEls['pd-review-issues-new'].textContent,'新增 1');
  assert.equal(issueEls['pd-review-issues-new'].hidden,false,'collapsed panel highlights unseen issues');
  assert.equal(issueEls['pd-review-issues-read'].hidden,false,'mark-all-processed action is visible with open issues');
  let readPrevented=false,readStopped=false;
  await issueCtx.pdMarkAllReviewIssuesRead({preventDefault(){readPrevented=true;},stopPropagation(){readStopped=true;}});
  const bulkResolved=calls.filter(c=>c[0]==='issue-post').at(-1);
  assert.equal(bulkResolved[1],'/api/review-issues/resolve','one-click action resolves the issue instead of only changing local state');
  assert.equal(JSON.stringify(bulkResolved[2]),JSON.stringify({source_group:'<script>',message_id:'msg-1'}));
  assert.equal(readPrevented&&readStopped,true,'mark-all-processed does not toggle the details panel');
  issueRows[0].updated_at='2026-10-13 08:30:00';
  issueEls['pd-review-issues'].open=true;
  await issueCtx.loadReviewIssues();
  assert.ok(issueEls['pd-review-issues-list'].innerHTML.includes('&lt;img onerror=x&gt;'));
  assert.ok(!issueEls['pd-review-issues-list'].innerHTML.includes('<script>'));
  assert.ok(issueEls['pd-review-issues-list'].innerHTML.includes('pd-issue-new'),'unseen issue is visibly marked when opened');
  issueEls['pd-review-issues'].open=false;
  await issueCtx.loadReviewIssues();
  assert.equal(issueEls['pd-review-issues-new'].hidden,true,'opening the panel marks the current issue version as seen');
  issueRows[0].updated_at='2026-10-13 09:00:00';
  await issueCtx.loadReviewIssues();
  assert.equal(issueEls['pd-review-issues-new'].hidden,false,'updated issue becomes new again');
  await issueCtx.resolveReviewIssue({disabled:false,dataset:{group:'turkey_price',message:'msg-1'}});
  assert.equal(calls.filter(c=>c[0]==='issue-post').at(-1)[1],'/api/review-issues/resolve');
  console.log('PASS: independent rows, state preservation, escaping, actions, view switch and filtered pagination');
})().catch(e=>{console.error(e);process.exitCode=1;});
