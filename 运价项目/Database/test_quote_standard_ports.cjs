const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/index.html','utf8');
const selection=source.slice(source.indexOf('function quotePortForSelection'),source.indexOf('function clearQuoteSelection'));
const groups=[{n:'South China Group',m:'Yantian,Shekou'}];
const badge={};
const context={quoteEditorSelect(){},
 S:{portIdx:new Map(),sales:{appliedQuery:{tab:'rates',params:new URLSearchParams('pol=Yantian%2CShekou&pod=Santos')},byKey:{key:{content_key:'key',pol:'South China Group',pod:'Santos'}},quoteSelection:new Map()}},
 gpOf:(value)=>groups.find(group=>group.n===value)||null,
 gpMembers:group=>group.m.split(','),
 foldKey:value=>String(value||'').toLowerCase().trim(),
 URLSearchParams,$:()=>badge,currentCatTab:()=> 'rates',updateQuoteSelectionCount:()=>{}
};
vm.createContext(context);vm.runInContext(selection,context);
const checkbox={dataset:{quoteKey:'key'},checked:true};
context.toggleQuoteSelection(checkbox);
const selected=context.S.sales.quoteSelection.get('key');
assert.equal(selected.quotePol,'Yantian / Shekou','all queried standard ports matching the selected group are carried');
assert.equal(selected.quotePod,'Santos','a non-group port keeps its queried standard name');
context.S.sales.appliedQuery.params=new URLSearchParams('pol=Yantian&pod=Other');
assert.equal(context.quotePortForSelection({pol:'South China Group'},'pol'),'Yantian');
assert.equal(context.quotePortForSelection({pod:'South China Group'},'pod'),'South China Group','unmatched query ports do not replace the group');
assert.equal(context.quotePortForSelection({pol:'Shanghai'},'pol'),'Shanghai','ordinary ports remain unchanged');
console.log('PASS: selected quotation carries only queried standard ports that belong to its displayed port group.');
