// Browser-state regression without production data or network access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sales = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
const notices = fs.readFileSync(path.join(__dirname, 'web/notices.js'), 'utf8');
function source(text, name) {
  const match = text.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\n}'));
  assert.ok(match, name);
  return match[0];
}
const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {value:'',innerHTML:'',hidden:false,classList:{remove(){}},querySelectorAll(){return [];}});
  return elements.get(id);
};
const calls = [];
const S = {sales:{tab:'rates',sorts:{rates:null,space:null},polTags:[],podTags:[],carrier:'',queried:false,page:1},
  profile:{preferences:{routes:['Route A'],groups:['Group A']}},useDefaults:true,
  auth:{user_id:'fixture',role:'sales'},role:'sales'};
const ctx = vm.createContext({S,SALES_PORT_FILTER_MAX:3,$:el,URLSearchParams,JSON,document:{querySelectorAll(){return [];},body:{classList:{toggle(){}}}},
  esc:String,renderPersonalScope(){},salesExactFilters(){return true;},commitAllTags(){},startCompanyLookups(){},resetRealtimeEtd(){},salesLoad(){calls.push('salesLoad');}});
for (const name of ['salesQueryParams','salesRequestParams','resetPersonalQuery','salesSearch','clearSalesSearch']) vm.runInContext(source(sales,name),ctx);
let params = ctx.salesRequestParams();
assert.equal(params.get('display_mode'),'preferred');
assert.deepEqual(JSON.parse(params.get('display_routes')),['Route A']);
assert.deepEqual(JSON.parse(params.get('display_groups')),['Group A']);
el('sf-route').value='Route C'; el('sf-group').value='Group B';
ctx.salesSearch();
params = ctx.salesRequestParams();
assert.equal(params.get('route'),'Route C');
assert.equal(params.get('group'),'Group B');
assert.equal(params.has('display_mode'),false);
assert.equal(params.has('display_routes'),false);
ctx.clearSalesSearch();
assert.equal(ctx.salesRequestParams().get('display_mode'),'preferred');
assert.equal(el('sf-route').value,'');

const noticesUi = {sequence:0,page:1,filters:{groups:[],routes:[],tag:'',q:'',pending:false},searchActive:false};
const noticeRequests = [];
const noticeElements = new Map();
const ne = id => {
  if (!noticeElements.has(id)) noticeElements.set(id, {value:'',innerHTML:'',querySelector(){return null;},querySelectorAll(){return [];}});
  return noticeElements.get(id);
};
const nc = vm.createContext({S,NOTICE_UI:noticesUi,NOTICE_UI_PAGE_SIZE:20,NOTICE_DEMO_MODE:false,URLSearchParams,JSON,ne,
  currentCatTab(){return 'dyn';},noticeSetMode(){},noticeApi:async url=>{noticeRequests.push(url);return {rows:[],groups:[],routes:[],tags:[],total:0};},
  noticeOptions(){return '';},noticeIcon(){return '';},noticeBoardHtml(){return '';},nh:String,
  noticeScopeHtml(){return '';},noticeBindImages(){},noticeScheduleAlignment(){},
  noticeRenderComposeScope(){},noticeHighlightMatches(){},noticeRenderReviewQueue(){},
  renderPersonalScope(){},noticeError(error){throw error;}});
for (const name of ['noticeLoad','noticeChooseGroup','noticeClearSearch']) vm.runInContext(source(notices,name),nc);
(async () => {
  await nc.noticeLoad();
  assert.equal(new URLSearchParams(noticeRequests.at(-1).split('?')[1]).get('display_mode'),'preferred');
  nc.noticeChooseGroup('Group B');
  await new Promise(resolve=>setImmediate(resolve));
  params=new URLSearchParams(noticeRequests.at(-1).split('?')[1]);
  assert.deepEqual(JSON.parse(params.get('groups')),['Group B']);
  assert.equal(params.has('display_groups'),false);
  nc.noticeClearSearch();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(new URLSearchParams(noticeRequests.at(-1).split('?')[1]).get('display_mode'),'preferred');
  console.log('Display preference state passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
