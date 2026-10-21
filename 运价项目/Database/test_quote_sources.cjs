const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/index.html','utf8');
function functionSource(name){
 const start=source.indexOf('function '+name);
 const end=source.indexOf('\nfunction ',start+1);
 return source.slice(start,end<0?undefined:end);
}
const context={
 REALTIME_CONTAINER_LABELS:{1:'20GP',11:'40GP',12:'40HQ/HC',20:'40NOR'},
 REALTIME_VIA_DIRECT:1,REALTIME_VIA_TRANSFER:2,
 COMPANY_PRICE_SORT_FIELDS:{'20GP':'price_20gp','40HQ/HC':'price_40hq','40NOR':'price_40nor','45HQ/HC':'price_45hq'},
 $:()=>({value:''}),
 companyPriceMap:item=>Object.fromEntries((item.product_price||[]).map(price=>[price.price_unit,price.currency_code+' '+price.show_price])),
 companyTerminalName:info=>info.terminal_en_name,
 companyIsoDate:value=>'2026-10-20',
 S:{sales:{quoteSelection:new Map(),quoteSpaceSelection:new Map(),companyQuoteItems:new Map(),realtimeQuoteItems:new Map()}},
};
vm.createContext(context);vm.runInContext(functionSource('companyVisiblePriceUnits')+functionSource('companyPriceValue')+functionSource('realtimeContainerLabel')+functionSource('realtimeRouteMode')+functionSource('companyQuoteSelection')+functionSource('realtimeQuoteSelection'),context);
assert(source.includes('data-quote-source="company"'),'company freight rows expose a selection control');
assert(source.includes('data-quote-source="realtime"'),'live capacity rows expose a selection control');
const companyItem=context.companyQuoteSelection({pair:'p1',fresh:true,item:{
 carrier_info:{carrier_code:'MSK'},from_terminal_info:{terminal_en_name:'Shanghai'},to_terminal_info:{terminal_en_name:'Santos'},
 etd_list:['2026-10-20'],line_code:'DIRECT',product_price:[{price_unit:'20GP',currency_code:'USD',show_price:4500}]
}},'company-key');
assert.equal(companyItem.quoteKey,'company-key');
assert.equal(companyItem.carrier,'MSK');
assert.equal(companyItem.pol,'Shanghai');
assert.equal(companyItem.pod,'Santos');
assert.equal(companyItem.cab['20GP'],'USD 4500');
assert.deepEqual(Array.from(companyItem.quotePriceLines),['USD 4500 / 20GP']);

const slot=context.realtimeQuoteSelection({row:{pair:'p2',fresh:true,item:{}},containerId:1,quote:{
 carrierName:'CMA',pol:{terminalEnName:'Ningbo'},pod:{terminalEnName:'Santos'},etd:'2026-10-21',eta:'2026-11-18',
 vessel:'VESSEL',voyage:'V001',totalOceanPriceUsd:1800,totalPrice:'USD 2050',scheduleWeek:'W43',viaWay:1,
 transDay:28,cutOffTime:'2026-10-19 18:00',inventory:6,obtainDate:1792560000000
}},'slot-key');
assert.equal(slot.quoteKey,'slot-key');
assert.equal(slot.pol,'Ningbo');
assert.equal(slot.pod,'Santos');
assert.equal(slot.vesselVoyage,'VESSEL / V001');
assert.equal(slot.inventory,6);
assert.equal(slot.totalOceanPriceUsd,1800);
assert.equal(slot.totalPrice,'USD 2050');
assert.equal(slot.transportMode,'直达');
assert.equal(slot.containerType,'20GP');

const transferSlot=context.realtimeQuoteSelection({row:{pair:'p3',fresh:true,item:{}},quote:{
 carrierName:'HMM',pol:{terminalEnName:'Dalian'},pod:{terminalEnName:'Pecem'},viaWay:2,
 viaTerminals:[{terminalName:'Ulsan',terminalCode:'KRUSN'}]
}},'transfer-key');
assert.equal(transferSlot.transportMode,'中转 · Ulsan','viaWay=2 and terminalName must be shown as a transfer route');

const detailContext={
 REALTIME_CONTAINER_LABELS:{1:'20GP',11:'40GP',12:'40HQ/HC',20:'40NOR'},
 REALTIME_VIA_DIRECT:1,REALTIME_VIA_TRANSFER:2,
 esc:value=>String(value),
 companyIsoDate:value=>String(value||''),
 realtimeFields:(title,fields)=>title+':'+fields.map(([label,value])=>label+':'+(value==null||value===''?'—':value)).join('|'),
};
vm.createContext(detailContext);vm.runInContext(functionSource('realtimeContainerLabel')+functionSource('realtimeRouteMode')+functionSource('realtimeQuoteDetails'),detailContext);
const transferDetails=detailContext.realtimeQuoteDetails({viaWay:2,viaTerminals:[{terminalName:'Ulsan'}]});
assert(transferDetails.includes('中转/直达:中转 · Ulsan'),'expanded details must identify transfer mode and port');

const keyContext={S:{sales:{quoteKeyRegistry:new Map(),quoteKeySequence:0}}};
vm.createContext(keyContext);vm.runInContext(functionSource('quoteSelectionKey'),keyContext);
const opaqueKey=keyContext.quoteSelectionKey('realtime','internal-content-identifier');
assert.equal(opaqueKey,'realtime-1');
assert.equal(keyContext.quoteSelectionKey('realtime','internal-content-identifier'),opaqueKey,'row keys remain stable across result rerenders');
assert(!opaqueKey.includes('internal-content-identifier'),'row keys do not expose quote details in markup attributes');

const actions={quoteEditorSelect(){},quotePortForSelection:item=>item,updateQuoteSelectionCount(){},currentCatTab:()=> 'rates',renderQuotePage(){}};
const toggleContext={...context,...actions,S:{sales:{byKey:{local:{pol:'Shanghai',pod:'Santos'}},quoteSelection:new Map(),quoteSpaceSelection:new Map(),companyQuoteItems:new Map([['company',companyItem]]),realtimeQuoteItems:new Map([['slot',slot]]),quoteView:'rates'}}};
vm.createContext(toggleContext);vm.runInContext(functionSource('toggleQuoteSelection'),toggleContext);
toggleContext.toggleQuoteSelection({dataset:{quoteSource:'company',quoteKey:'company'},checked:true});
toggleContext.toggleQuoteSelection({dataset:{quoteSource:'realtime',quoteKey:'slot'},checked:true});
assert.equal(toggleContext.S.sales.quoteSelection.size,1,'company-system rates enter the freight table');
assert.equal(toggleContext.S.sales.quoteSpaceSelection.size,1,'carrier inventory enters the independent capacity table');
assert.equal(toggleContext.S.sales.quoteView,'space','selecting live capacity opens its table view');
console.log('PASS: company freight and live capacity rows normalize into their distinct quotation records.');
