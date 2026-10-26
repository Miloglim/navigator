const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/index.html','utf8');
[...source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).filter(Boolean)
 .forEach((script,index)=>new vm.Script(script,{filename:`inline-${index}.js`}));
for(const id of ['sf-group','sf-valid','sf-time-from','sf-time-to']){
 assert(source.includes(`id="${id}"`),`missing retained hidden filter: ${id}`);
}
for(const marker of ['id="adv-toggle"','id="adv-panel"','id="sf-keyword"','id="sf-search-original"','id="sales-advanced-search"']){
 assert(!source.includes(marker),`hidden keyword feature must not render: ${marker}`);
}
const query=source.slice(source.indexOf('function salesQueryParams'),source.indexOf('function salesSearch()'));
const load=source.slice(source.indexOf('async function salesLoad'),source.indexOf('function salesFlatDetailHtml('));
const headers=source.slice(source.indexOf('const SALES_SORT_LABELS'),source.indexOf('function salesQueryParams'));
const company=source.slice(source.indexOf('let companyDetailItems'),source.indexOf('function renderCompanyTask'));
const currencyHelpers=source.slice(source.indexOf('function currencyCode'),source.indexOf('function explicitCurrencyCode'));
const elements=new Map(),pending=[],renders=[];
const context={S:{useDefaults:false,profile:null,sales:{tab:'rates',polTags:[],podTags:[],sorts:{rates:{field:'price_20gp',direction:'asc'},space:null},queried:true,groups:[],rows:[],page:1,loadRevision:0,tabCache:{}}},URLSearchParams,PAGE_SIZE:50,SALES_HIDDEN_TABS:new Set(),SALES_PORT_FILTER_MAX:3,
 $:id=>{if(!elements.has(id))elements.set(id,{value:'',innerHTML:'',textContent:'',hidden:false,title:'',setAttribute(){}});return elements.get(id);},
 document:{querySelectorAll:()=>[]},noticeSetMode:()=>{},canView:()=>true,canOpenSalesTab:()=>true,salesExactFilters:()=>true,currentCatTab:()=> 'rates',hasSalesFilter:()=>false,esc:String,
 updateLocalSourceHeader:()=>{},fetch:url=>new Promise(resolve=>pending.push({url,resolve})),renderSalesFolded:()=>renders.push(context.S.sales.groups),renderPager:()=>{}};
vm.createContext(context);vm.runInContext(currencyHelpers+query+load+headers+company,context);
(async()=>{
 const slow=context.salesLoad();context.S.sales.sorts.rates={field:'price_20gp',direction:'desc'};const fast=context.salesLoad();
 assert(pending[0].url.includes('direction=asc'));assert(pending[1].url.includes('direction=desc'));
 pending[1].resolve({json:async()=>({ok:true,total:1,groups:['latest-desc']})});await fast;
 pending[0].resolve({json:async()=>({ok:true,total:1,groups:['stale-asc']})});await slow;
 assert.deepEqual(context.S.sales.groups,['latest-desc']);assert.equal(renders.length,1,'stale result must not render');
 context.salesLoad=()=>{};
 context.S.sales.sorts.rates=null;context.S.sales.page=3;
 context.salesToggleSort('etd');assert.equal(context.S.sales.sorts.rates.direction,'asc');assert.equal(context.S.sales.page,1);
 assert(context.salesSortHeader('etd').includes('aria-sort="ascending"'));
 context.salesToggleSort('etd');assert.equal(context.S.sales.sorts.rates.direction,'desc');
 context.salesToggleSort('etd');assert.equal(context.S.sales.sorts.rates,null);
 assert(context.salesQueryParams().toString().includes('sort=synced_at'));
 context.S.sales.polTags=['A','B','C','D'];context.S.sales.podTags=['W','X','Y','Z'];
 assert.equal(context.salesQueryParams().get('pol'),'A,B,C','POL query is capped at three values');
 assert.equal(context.salesQueryParams().get('pod'),'W,X,Y','POD query is capped at three values');
 context.S.sales.polTags=[];context.S.sales.podTags=[];
 context.$('sf-global-query').checked=true;
 assert.equal(context.salesQueryParams().get('global_query'),'1','global query toggle is carried explicitly');
 context.$('sf-global-query').checked=false;
 assert(context.salesSortHeader('synced_at').includes('aria-sort="descending"'));
 context.salesToggleSort('price_20gp');context.salesToggleSort('validity');assert.equal(context.S.sales.sorts.rates.field,'validity');
 const companyRows=[
   {item:{carrier_info:{carrier_code:'MSC'},from_terminal_info:{name:'Qingdao'},to_terminal_info:{name:'Santos'},valid_from:3,product_price:[{price_unit:'20GP',show_price:5100}]},created_at:30},
   {item:{carrier_info:{carrier_code:'CMA'},from_terminal_info:{name:'Ningbo'},to_terminal_info:{name:'Paranagua'},valid_from:1,product_price:[{price_unit:'20GP',show_price:4900}]},created_at:20}
 ];
 context.companyRateRows=companyRows;context.S.sales.companySort=null;
 assert.deepEqual(context.companySortedRows(companyRows).map(r=>r.item.carrier_info.carrier_code),['CMA','MSC'],'company default sort is carrier asc');
 const sameCarrier=[
   {item:{carrier_info:{carrier_code:'CMA'},valid_from:1,product_price:[]},created_at:10},
   {item:{carrier_info:{carrier_code:'CMA'},valid_from:3,product_price:[]},created_at:11},
   {item:{carrier_info:{carrier_code:'CMA'},valid_from:null,product_price:[]},created_at:12}
 ];
 assert.deepEqual(context.companySortedRows(sameCarrier).map(r=>r.item.valid_from),[3,1,null],'same carrier defaults to validity desc with empty dates last');
 assert(context.companySortHeader('carrier').includes('aria-sort="ascending"'),'company carrier header shows default ascending state');
 context.companyToggleSort('carrier');assert.equal(context.S.sales.companySort.direction,'desc','clicking the active default carrier order reverses it');
 context.companyToggleSort('carrier');assert.equal(context.S.sales.companySort,null,'second carrier click restores the default ascending order');
 context.companyToggleSort('price_20gp');assert.equal(context.S.sales.companySort.field,'price_20gp');
 assert.deepEqual(context.companySortedRows(companyRows).map(r=>r.item.carrier_info.carrier_code),['CMA','MSC'],'company price sort asc');
 context.companyToggleSort('price_20gp');assert.equal(context.S.sales.companySort.direction,'desc');
 context.companyToggleSort('price_20gp');assert.equal(context.S.sales.companySort,null);
 assert.deepEqual(context.companySortedRows(companyRows).map(r=>r.item.carrier_info.carrier_code),['CMA','MSC'],'third click restores default carrier order');
 const missing={item:{carrier_info:{carrier_code:'ONE'},from_terminal_info:{name:'X'},to_terminal_info:{name:'Y'},product_price:[]},created_at:40};
 context.S.sales.companySort={field:'price_20gp',direction:'asc'};
 assert.equal(context.companySortedRows([missing,companyRows[0]])[0],companyRows[0],'empty company prices sort after priced rows');
 assert(context.companySortHeader('validity').includes('data-company-sort="validity"'),'company headers expose sort metadata');
 console.log('PASS: reversed async responses, tri-state ordering, default timestamp order, first-page reset, global query toggle.');
})().catch(error=>{console.error(error);process.exitCode=1;});
