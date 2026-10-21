const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
function source(name) {
  const match = html.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\n}'));
  assert.ok(match, name);
  return match[0];
}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'',innerHTML:'',classList:{remove(){},add(){}},focus(){},querySelectorAll(){return [];}});
  return elements.get(id);
}
const visits = [];
const recorded = [];
const S = {
  sales:{tab:'group',sorts:{rates:null,space:null},polTags:[],podTags:[],carrier:'',queried:false,page:1,groupScope:null,groupJumpName:null},
  profile:{preferences:{routes:['Route A'],groups:['Group A']}},useDefaults:true,
};
const context = vm.createContext({S,$:element,URLSearchParams,JSON,
  postJSON:async (path,body)=>{recorded.push([path,body]);return {ok:true,data:{click_count:1}};},
  document:{querySelector(selector){return {dataset:{tab:selector.includes('space')?'space':'rates'}};},querySelectorAll(){return [];}},
  renderPersonalScope(){},acSource(){return [];},foldKey:String,toast(){},commitAllTags(){},startCompanyLookups(){},resetRealtimeEtd(){},
  setCatTab(button){visits.push([button.dataset.tab,context.salesRequestParams()]);},salesLoad(){},
});
for (const name of ['salesQueryParams','salesRequestParams','salesExactFilters','jumpGroup','salesSearch','resetPersonalQuery','recordSourceGroupOpen','renderGroupCards']) {
  vm.runInContext(source(name), context);
}
const scope = Array.from(context.salesRequestParams().entries()).filter(([key]) => key.startsWith('display_'));
const specialName = `East's "test" <group>`;
context.jumpGroup(specialName,'space',scope);
assert.equal(visits.at(-1)[0],'space');
assert.equal(visits.at(-1)[1].get('group'),specialName);
assert.deepEqual(JSON.parse(visits.at(-1)[1].get('display_routes')),['Route A']);
assert.deepEqual(JSON.parse(visits.at(-1)[1].get('display_groups')),['Group A']);
assert.equal(context.salesExactFilters(),true);
element('sf-group').value='unlisted manually typed group';
assert.equal(context.salesExactFilters(),false);
element('sf-group').value=specialName;
context.jumpGroup('Group A','rates',scope);
assert.equal(visits.at(-1)[0],'rates');
context.jumpGroup('Group A','invalid',scope);
assert.equal(visits.length,2);
context.salesSearch();
assert.equal(context.salesRequestParams().has('display_mode'),false);
context.jumpGroup('Group A','space',scope);
context.resetPersonalQuery();
assert.equal(S.sales.groupScope,null);
assert.equal(S.sales.groupJumpName,null);
assert.equal(context.salesRequestParams().get('display_mode'),'preferred');
console.log('Source-group card navigation passed');
context.recordSourceGroupOpen('Group A').then(async()=>{
  assert.equal(recorded.length,1);
  assert.equal(recorded[0][0],'/api/sales/groups/visit');
  assert.equal(recorded[0][1].name,'Group A');
  S.sales.loadRevision=1;
  context.currentCatTab=()=> 'group';
  context.esc=String;
  context.displayMinuteTime=String;
  const buttons=[];
  element('table-container').querySelectorAll=()=>buttons;
  let order='personal_clicks';
  context.fetch=async()=>({json:async()=>({ok:true,data:{order,rows:[
    {name:'Group A',rates:2,space:0,latest:'2026-09-30 12:00:00'}]}})});
  async function clickCard(){
    buttons.length=0;
    buttons.push({dataset:{sourceName:'Group A',sourceKind:'rates'},addEventListener(type,handler){this.click=handler;}});
    await context.renderGroupCards();
    buttons[0].click();
    await Promise.resolve();
  }
  await clickCard();
  assert.equal(recorded.length,2);
  assert.equal(visits.at(-1)[0],'rates');
  order=undefined;
  await clickCard();
  assert.equal(recorded.length,2);
  assert.equal(visits.at(-1)[0],'rates');
  console.log('Source-group click tracking passed');
}).catch(error=>{console.error(error);process.exitCode=1;});
