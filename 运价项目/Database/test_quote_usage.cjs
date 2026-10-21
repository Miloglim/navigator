const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/index.html','utf8');
const start=source.indexOf('const QUOTE_EDITOR_VERSION='),end=source.indexOf('\nfunction detailRow(',start);
const storage=new Map(),events=[],timers=new Map();let timerId=0;
const ctx={S:{auth:{user_id:'usr_alpha'},sales:{quoteSelection:new Map(),quoteSpaceSelection:new Map()}},
 localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
 document:{addEventListener(){}},$:()=>null,console,toast(){},updateQuoteSelectionCount(){},
 ETD_MON:['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],
 quoteDays:value=>value?String(value)+' DAYS':'',nonUsdPriceCurrency:()=>'',foldKey:value=>String(value||'').toLowerCase(),
 esc:value=>String(value??''),fetch:(url,options)=>{events.push({url,payload:JSON.parse(options.body)});return Promise.resolve({ok:true});},
 setTimeout:callback=>{const id=++timerId;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id)};
vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
const item={id:17,carrier:'PIL',pol:'Qingdao',pod:'Santos',cab:{'20GP':5400},etd:'2026-10-20',transit_days:31};
ctx.quoteEditorSelect('rates',item,true);
assert.equal(events[0].payload.action,'item_selected');
assert.deepEqual(Object.keys(events[0].payload).sort(),['action','field_type','item_count','quote_kind']);
const row=ctx.quoteEditorLoad().rates.rows[0];
ctx.quoteEditorEdit('rates',row.id,'c3','PRIVATE-PRICE-9876');
for(const callback of timers.values())callback();timers.clear();
const edit=events.find(event=>event.payload.action==='cell_edited');
assert.equal(edit.payload.field_type,'price');
assert.equal(edit.payload.item_count,1);
assert(!JSON.stringify(events).includes('PRIVATE-PRICE-9876'));
ctx.quoteEditorEdit('rates',row.id,'c8','PRIVATE-PORT-ALIAS');
const beforeSwitch=events.length;ctx.S.auth.user_id='usr_beta';
for(const callback of timers.values())callback();timers.clear();
assert.equal(events.length,beforeSwitch,'pending edits must not cross account switches');
assert(events.every(event=>event.url==='/api/activity/quote-usage'));
console.log('PASS: quote usage events contain only allowlisted metadata and stay account-isolated.');
