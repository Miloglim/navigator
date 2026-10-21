const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/index.html','utf8');

assert(!source.includes('id="company-rates-enabled"'),'company rates should no longer require an optional selection');
assert(source.includes('<label for="sf-carrier">船司</label>'),'sales carrier filter should be labeled 船司');
assert(!source.includes('id="carrier-realtime-enabled"'));
assert(source.includes('id="company-rate-results"'));
assert(source.includes('id="sales-source-local"'));
assert(source.includes('id="sales-source-local-body"'));
assert(source.includes('钉钉群聊运价'));
assert(!source.includes('id="realtime-container"'),'realtime must reuse the main container selection');
assert(source.includes('aria-label="柜型，单选，可不选"'));
assert(source.includes('onchange="syncSalesCtypes(this)"'));
assert(source.includes('id="company-credential-token" type="password"'));
assert(source.includes('/api/admin/company-credentials'));
assert(source.includes("token.value=''"));

const company=source.slice(source.indexOf('function companyDateRange'),source.indexOf('/* =================== ROLE SWITCH'));
const salesSearchSource=source.slice(source.indexOf('function salesSearch()'),source.indexOf('function clearSalesSearch()'));
const elements=new Map();
function element(id){
  if(!elements.has(id)) elements.set(id,{id,value:'',checked:false,hidden:false,innerHTML:'',textContent:'',disabled:false,dataset:{},attributes:{},querySelector:()=>null,querySelectorAll:()=>[],classList:{toggle(){}},setAttribute(key,value){this.attributes[key]=value;}});
  return elements.get(id);
}
const calls=[];
const sessionValues={};
const ctypeInputs=[{value:'20GP',checked:false},{value:'40HQ/HC',checked:false},{value:'40NOR',checked:false},{value:'45HQ/HC',checked:false}];
const context={
  S:{auth:{user_id:'usr-1'},profile:{display_name:'Sales',preferences:{routes:[],groups:[],ctypes:[],start_tab:'rates'}},
     sales:{tab:'rates',polTags:['CNSHA'],podTags:['USLAX'],carrier:'COSCO',sorts:{rates:null,space:null},queried:false,page:2,groupScope:null,quoteSelection:new Map(),quoteSpaceSelection:new Map(),companyQuoteItems:new Map(),realtimeQuoteItems:new Map(),quoteKeyRegistry:new Map(),quoteKeySequence:0}},
  $:element,esc:v=>String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),currentCatTab:()=> 'rates',toast:(message)=>calls.push(['toast',message]),
  fetch:async(url,options)=>{calls.push(['fetch',url,options]);return {json:async()=>({ok:true,data:{task_id:'crt-1',cached:null}})};},
  setTimeout:()=>{},salesExactFilters:()=>true,commitAllTags:()=>{},renderPersonalScope:()=>{},
  salesLoad:()=>calls.push(['local']),startCompanyLookups:()=>calls.push(['company']),
  sessionStorage:{getItem:key=>sessionValues[key]||null,setItem:(key,value)=>{sessionValues[key]=value;}},
  document:{querySelector:()=>null,querySelectorAll:selector=>selector==='.sales-ctype-options input'?ctypeInputs:[]},Date,JSON,Promise,URLSearchParams,
};
element('carrier-realtime-enabled').checked=true;
element('sf-valid').value='2026-10-13';
element('sf-ctype').value='40HQ/HC';
element('realtime-weight').value='10000';
vm.createContext(context);
const validity=source.slice(source.indexOf('function salesValidityHtml'),source.indexOf('const ETD_MON'));
const ctypeSync=source.slice(source.indexOf('function syncSalesCtypes'),source.indexOf('function salesExactFilters'));
const salesQuery=source.slice(source.indexOf('function salesQueryParams'),source.indexOf('function salesRequestParams'));
const selectionHeaders=source.slice(source.indexOf('function quoteSelectAllHeader('),source.indexOf('function quotePickClick('));
vm.runInContext(selectionHeaders+company+validity+salesSearchSource+ctypeSync+salesQuery,context);


(async()=>{
  ctypeInputs[0].checked=true;
  context.syncSalesCtypes(ctypeInputs[0]);
  assert.equal(element('sf-ctype').value,'20GP');
  ctypeInputs[1].checked=true;
  context.syncSalesCtypes(ctypeInputs[1]);
  assert.equal(ctypeInputs[0].checked,false,'selecting a container clears the previous selection');
  assert.equal(element('sf-ctype').value,'40HQ/HC');
  ctypeInputs[1].checked=false;
  context.syncSalesCtypes(ctypeInputs[1]);
  assert.equal(element('sf-ctype').value,'','clearing the selected container means no filter');
  assert.equal(context.salesQueryParams().has('ctype'),false,'no selected container means no query filter');
  element('sf-ctype').value='40HQ/HC';
  assert.equal(context.salesQueryParams().get('ctype'),'40HQ/HC','the query carries one selected container');
  const payload=context.companyQueryPayload(false);
  context.S.sales.carrier='COSCO,MAERSK';
  assert.deepEqual(Array.from(context.companyQueryPayload(false).carriers),['COSCO','MAERSK']);
  assert.deepEqual(Array.from(context.companyQueryPayload(true).carriers),['COSCO','MAERSK']);
  context.S.sales.carrier='COSCO';
  assert.deepEqual(Array.from(payload.pol),['CNSHA']);
  assert.deepEqual(Array.from(payload.pod),['USLAX']);
  assert.deepEqual(Array.from(payload.carriers),['COSCO']);
  assert.equal(payload.valid_from,1791820800000);
  assert.equal(payload.valid_to,undefined,'company validity includes selected date and later dates');
  element('realtime-etd').value='2026-10-17';
  const realtimePayload=context.companyQueryPayload(true);
  assert.equal(realtimePayload.container_id,12,'realtime payload uses the main selected container type');
  assert.equal(realtimePayload.valid_from,1792166400000,'realtime ETD must not borrow rate validity');
  assert.equal(realtimePayload.valid_to,undefined,'realtime includes selected ETD and later sailings');
  element('realtime-etd').value='';
  assert.equal(context.companyQueryPayload(true).valid_from,undefined,'missing ETD must not silently use validity');
  element('realtime-etd').value='2026-10-17';
  context.salesSearch();
  assert.equal(calls[0][0],'local','local query must start first');
  assert.equal(calls[1][1],'/api/company-rates/query','company lookup starts independently after local query');
  calls.length=0;
  await context.startCompanyRateTask('company');
  assert.equal(calls[0][1],'/api/company-rates/query');
  calls.length=0;
  await context.startCompanyRateTask('realtime');
  assert.equal(calls[0][1],'/api/company-rates/realtime','realtime uses the main container filter');
  assert.equal(JSON.parse(calls[0][2].body).container_id,12);
  element('sf-ctype').value='';
  calls.length=0;
  await context.startCompanyRateTask('realtime');
  assert.equal(calls[0][0],'toast','realtime requires an explicit main container selection');
  assert.equal(calls.some(call=>call[0]==='fetch'),false,'missing container must not create an external task');
  element('sf-ctype').value='45HQ/HC';
  calls.length=0;
  await context.startCompanyRateTask('realtime');
  assert.equal(calls[0][0],'toast','unsupported main container is explained');
  assert.equal(calls.some(call=>call[0]==='fetch'),false,'unsupported container must not create an external task');
  calls.length=0;
  context.startCompanyLookups();
  assert.equal(calls[0][0],'fetch','company lookup starts automatically without a selection control');
  assert.equal(calls[0][1],'/api/company-rates/query','automatic company lookup uses the company freight endpoint');
  context.S.sales.podTags=[];
  calls.length=0;
  context.startCompanyLookups();
  assert.equal(calls.length,0,'company query is skipped when a required port is missing');
  assert(element('company-rate-company').innerHTML.includes('需同时选择起运港和目的港'),'missing company query scope is explained in its source section');
  context.S.sales.podTags=['USLAX'];
  element('sf-ctype').value='';
  context.renderCompanyTask('company',{status:'complete',query:{resolved_mappings:[{kind:'船司',input:'MSK',code:'MAERSK',id:4550}]},updated_at:1791942720,results:[{pair_key:'8951:8760',fresh:true,items:[{
    carrier_info:{carrier_code:'MAERSK'},from_terminal_info:{terminal_en_name:'SHANGHAI'},to_terminal_info:{terminal_en_name:'SANTOS'},
    line_code:'ASAS2',valid_from:1791993600000,valid_to:1792512000000,product_price:[
      {price_unit:'20GP',currency_code:'USD',show_price:5002},{price_unit:'40GP',currency_code:'USD',show_price:5302},
      {price_unit:'40HQ',currency_code:'USD',show_price:5302},{price_unit:'NOR',currency_code:'USD',show_price:5229}
    ]}]}]});
  const rendered=element('company-rate-company').innerHTML;
  assert(rendered.includes('SHANGHAI')&&rendered.includes('SANTOS'));
  assert(rendered.includes('20GP')&&rendered.includes('40HQ/HC')&&rendered.includes('40NOR'));
  assert(rendered.includes('$5002')&&rendered.includes('$5302')&&rendered.includes('$5229'));
  assert(rendered.includes('class="data-table sales-rate-table"'));
  assert(rendered.includes('sales-source-toggle')&&rendered.includes('公司运价'),'company results have a collapsible source heading');
  assert(rendered.includes('已完成 · 1 条'),'source heading summarizes status and result count');
  assert(!rendered.includes('8951:8760'),'internal port IDs must not be shown');
  assert(rendered.includes('查看港口与船司对应关系')&&rendered.includes('4550')&&rendered.includes('MSK'));
  assert(!rendered.includes('ASAS2'),'details are rendered only when opened');
  context.renderCompanyTask('company',{status:'partial',results:[{pair_key:'8951:8760',items:[{
    product_id:195045140,product_price:[{price_unit:'40HQ',currency_code:'USD',show_price:5600}],
    _company_associated_unpriced_products:['195045128','195148795','195150111']
  }]}]});
  const associatedNotice=element('company-rate-company').innerHTML;
  assert(!associatedNotice.includes('班期编号仅关联到有价记录'),'associated schedule details stay out of the sales result view');
  assert(associatedNotice.includes('部分完成 · 1 条'),'unpriced associations retain the partial task state');
  context.renderCompanyTask('company',{status:'partial',results:[{pair_key:'8951:8760',items:[{
    product_id:195045140,product_price:[{price_unit:'40HQ',currency_code:'USD',show_price:5600}],
    schedule_list:[{product_id:'195045128'}],_company_missing_products:['195045128','999']
  }]}]});
  const cachedNotice=element('company-rate-company').innerHTML;
  assert(!cachedNotice.includes('班期编号仅关联到有价记录'),'legacy cached tasks also hide association details');
  assert(cachedNotice.includes('另有 1 个班期产品尚未取得价格'),'legacy cached tasks must keep truly unassociated products marked missing');
  element('sf-ctype').value='20GP';
  const selectedContainerGrid=context.companyRateGrid([{fresh:true,item:{carrier_info:{carrier_code:'MAERSK'},from_terminal_info:{terminal_en_name:'SHANGHAI'},to_terminal_info:{terminal_en_name:'SANTOS'},product_price:[
    {price_unit:'20GP',currency_code:'USD',show_price:5002},{price_unit:'40HQ',currency_code:'USD',show_price:5302}
  ]}}]);
  assert(selectedContainerGrid.includes('data-company-sort="price_20gp"'));
  assert.equal((selectedContainerGrid.match(/data-company-sort="price_/g)||[]).length,4,'company and local tables reserve identical price columns');
  assert(selectedContainerGrid.includes('data-company-sort="price_40hq" aria-sort="none" style="visibility:hidden"'),'unselected price headers reserve their shared column positions');
  assert(selectedContainerGrid.includes('data-label="40HQ/HC" class="price" style="visibility:hidden"'),'unselected price cells reserve their shared column positions');
  assert(source.includes('.company-rate-source .sales-source-body { padding-inline:12px; }'),'company table body uses the same horizontal inset as the local results table');
  assert.deepEqual(Object.keys(Array.from(context.S.sales.companyQuoteItems.values())[0].cab),['20GP'],'company quotation keeps only the selected container');
  element('sf-ctype').value='';
  context.setSalesSourceCollapsed('company',true);
  assert.equal(element('company-rate-company-body').hidden,true,'company source can collapse independently');
  assert.equal(sessionValues['sales-source-collapsed-company'],'1','collapse state stays in the page session');
  context.setSalesSourceCollapsed('local',true);
  context.toggleSalesSourceSection('local');
  assert.equal(element('sales-source-local-body').hidden,false,'local source can reopen from its actual visible state');
  assert.equal(sessionValues['sales-source-collapsed-local'],'0','local collapse state persists for the page session');
  const realtimeRows=context.realtimeRateList([{fresh:true,item:{carrier:{carrierCode:'MAERSK'},result:[
    {etd:1792166400000,vessel:'Vessel A',voyage:'641W',totalOceanPriceUsd:4650},
    {etd:1792944000000,vessel:'Vessel B',voyage:'644W',totalOceanPriceUsd:5005}
  ]}}],1);
  assert(realtimeRows.includes(context.quoteSelectAllHeader('舱位')),'live-capacity selection header uses the shared select-all control');
  assert(!realtimeRows.includes('<th style="width:8%"'),'live-capacity selection column must not use a percentage width');
  assert(!realtimeRows.includes('<th style="width:9%"'),'live-capacity data columns must not add widths beyond the shared table width');
  assert.equal((realtimeRows.match(/class="qrow"/g)||[]).length,2);
  assert(realtimeRows.includes('sales-rate-table realtime-rate-table'));
  assert(realtimeRows.includes('2026-10-17')&&realtimeRows.includes('2026-10-26'));
  assert(realtimeRows.includes('USD 4650')&&realtimeRows.includes('USD 5005'));
  assert(realtimeRows.includes('class="price realtime-ocean-price has-price"'),'returned ocean price receives the emphasis style');
  assert(realtimeRows.includes('20GP海运费')&&!realtimeRows.includes('40HQ海运费'));
  const noOceanPrice=context.realtimeRateList([{fresh:true,item:{result:[{vessel:'No price'}]}}],1);
  assert(noOceanPrice.includes('class="price realtime-ocean-price"'),'missing ocean price keeps the column layout without the price highlight');
  assert(!noOceanPrice.includes('class="price realtime-ocean-price has-price"'),'missing ocean price is not highlighted as a quote');
  context.renderCompanyTask('realtime',{status:'complete',query:{container_id:1},results:[{pair_key:'8951:8760',fresh:true,items:[{
    carrier:{carrierCode:'MAERSK'},result:[
      {etd:1792166400000,vessel:'Vessel A',voyage:'641W',totalOceanPriceUsd:4650},
      {etd:1792944000000,vessel:'Vessel B',voyage:'644W',totalOceanPriceUsd:5005}
    ]
  }]}]});
  const realtimeResult=element('company-rate-realtime').innerHTML;
  assert(realtimeResult.includes('已完成 · 2 条'),'live-capacity heading counts visible sailing quotes, not result containers');
  assert.equal((realtimeResult.match(/class="qrow"/g)||[]).length,2,'live-capacity heading matches rendered rows');
  const detail=context.companyRateDetails({product_id:42,line_code:'ASAS2',trans_day:0,valid_from:1792627200000,valid_to:1793404800000,comments_list:[
    {productId:42,commentsSchedule:'周一(EF4) 周四(EL2)',comments:'CURRENT <script>bad()</script>\nsecond line',internal_comments:'internal',defaultFreeTime:'14 days'},
    {productId:41,comments:'OTHER'}],future_field:'new field retained'});
  assert(detail.includes('company-detail-header')&&detail.includes('company-detail-body'),'company detail uses a compact header and readable body');
  assert(detail.includes('有效期')&&detail.includes('班期')&&detail.includes('周一(EF4) 周四(EL2)'));
  assert(detail.includes('对外通知')&&detail.includes('内部备注'),'company detail separates external and internal copy');
  assert(detail.includes('Free Time相关')&&detail.includes('默认免费Free Time：14 days'),'free-time terms stay under one heading');
  assert(detail.includes('收起'),'expanded company detail can be closed from its header');
  assert(detail.includes('14 days')&&detail.includes('内部备注'));
  assert(detail.includes('&lt;script&gt;')&&!detail.includes('<script>'));
  assert(detail.includes('CURRENT')&&!detail.includes('OTHER'),'only the current product note appears in the detail');
  assert(!detail.includes('其他产品或未明确归属的备注')&&!detail.includes('更多产品信息'));
  assert(!detail.includes('new field retained')&&!detail.includes('company-detail-more'));
  const structuredSchedule=context.companyRateDetails({product_id:42,comments_list:[{productId:42,commentsSchedule:[
    {schedule:6,line_code:'ASAS2'},{schedule:2,line_code:'NEOASAS'},{schedule:2,line_code:'NEOASAS'}
  ]}]});
  assert(structuredSchedule.includes('周六(ASAS2) · 周二(NEOASAS)'),'structured weekdays and service names remain readable without duplicates');
  assert(!structuredSchedule.includes('[object Object]'),'structured schedule never leaks object text');
  assert(source.includes('#view-sales .realtime-rate-table .qrow > td.realtime-ocean-price'),'ocean price has its own layout priority');
  assert(source.includes('@container sales-results (max-width:1080px)'),'live-capacity table switches layout before price is clipped');
  assert(context.salesValidityHtml({valid_from:'2099-01-01',valid_to:'2099-02-01'},true).includes('sales-validity-future'));
  assert(!context.salesValidityHtml({valid_from:'2000-01-01',valid_to:'2099-02-01'},true).includes('sales-validity-future'));
  for(const tab of ['space','group','dyn']) {
    context.currentCatTab=()=>tab;
    context.syncCompanySourceControls();
    assert(element('company-rate-results').hidden,'company results must be hidden on '+tab);
    context.renderCompanyTask('company',{status:'complete',results:[]});
    assert(element('company-rate-results').hidden,'late results must not reopen on '+tab);
  }
  console.log('PASS: automatic company lookup, required scope, query payload, and local-first ordering.');
})().catch(error=>{console.error(error);process.exitCode=1;});
