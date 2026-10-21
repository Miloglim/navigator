const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync('web/index.html', 'utf8');
assert(/#view-sales \.feedback-confirm-dialog \{ position:fixed; inset:0;[^}]*margin:auto;/.test(source));
assert(/#view-sales \.feedback-old \{ position:relative; display:inline-block; width:fit-content; max-width:100%;/.test(source));
const helpers = source.slice(source.indexOf('const FEEDBACK_FIELDS ='), source.indexOf('function salesFlatDetailHtml('));
const cabinet = source.slice(source.indexOf('function isEuroBeirutQuote('), source.indexOf('function addOverflowTitles('));
const context = {
  S: {auth: {user_id: 'usr_a', role: 'sales'}},
  esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;'),
  jsArg: value => JSON.stringify(value).replace(/"/g, '&quot;'),
  displayMinuteTime: value => value,
  document: {addEventListener() {}, getElementById() { return null; }},
  window: {addEventListener() {}},
  clearTimeout() {},
};
vm.createContext(context);
vm.runInContext(helpers + cabinet, context);
const row = {entry_id: 'frt_1', cab: {'40HQ/HC': 2650}, cab_entry_id: {'40HQ/HC': 'frt_1'},
  feedback_by_entry: {frt_1: [{id:'fbc_1', submission_id:'fb_1', field:'price_usd', old_value:'2650', suggested_value:'2550',
    author_user_id:'usr_a', author_name:'销售甲', note:'客户确认', created_at:'2026-09-30 12:00:00'}]}};
const price = context.foldCabCell(row, '40HQ/HC');
assert(price.includes('feedback-old') && price.includes('$2650'));
assert(price.includes('feedback-new') && price.includes('$2550'));
assert(price.includes('class="feedback-dot"') && !price.includes('>用户修订</span>'));
assert(price.includes('data-feedback-id="fbc_1"'));
assert(price.includes('data-feedback-field="price_usd"') && price.includes('data-feedback-entry="frt_1"'));
assert(!price.includes('建议 $') && !price.includes('待航管核实'));
const other = {entry_id:'frt_2', cab:{'20GP':1800, '40HQ/HC':2600},
  cab_entry_id:{'20GP':'frt_2', '40HQ/HC':'frt_3'}, feedback_by_entry:{
    frt_2:[{id:'fbc_2',field:'price_usd',old_value:'1800',suggested_value:'1750'}],
    frt_3:[{id:'fbc_3',field:'price_usd',old_value:'2600',suggested_value:'2500'}]}};
assert(context.foldCabCell(other,'20GP').includes('data-feedback-id="fbc_2"'));
assert(!context.foldCabCell(other,'20GP').includes('fbc_3'));
assert(context.foldCabCell(other,'40HQ/HC').includes('data-feedback-id="fbc_3"'));
assert(context.feedbackFieldHtml('old','route',[{id:'fbc_4',field:'route',old_value:'old',suggested_value:'new'}]).includes('data-feedback-id="fbc_4"'));
assert(context.feedbackFieldHtml('old','route',[{id:'fbc_4',field:'route',stale:true}]).includes('data-feedback-id="fbc_4"'));
assert(context.foldCabCell({cab:{'20GP':1800},cab_entry_id:{'20GP':'frt_2'},feedback_by_entry:{frt_2:[
  {id:'fbc_stale',field:'price_usd',stale:true}]}},'20GP').includes('data-feedback-id="fbc_stale"'));
assert(context.foldCabCell({cab:{'20GP':null},cab_entry_id:{'20GP':'frt_2'},feedback_by_entry:{frt_2:[
  {id:'fbc_added',field:'price_usd',old_value:'',suggested_value:'1800'}]}},'20GP').includes('data-feedback-id="fbc_added"'));
assert(context.foldCabCell({pod:'Beirut',cab:{'20GP':3250},cab_entry_id:{'20GP':'frt_eur'},
  feedback_by_entry:{frt_eur:[{id:'fbc_eur',field:'price_usd',old_value:'3250',suggested_value:'3200'}]}},'20GP').includes('见原图（EUR）'));
assert(!context.foldCabCell({pod:'Beirut',cab:{'20GP':3250}},'20GP').includes('$3250'));
assert(source.includes('function editFeedbackTarget(target)'));
assert(source.includes('decorateFeedbackTargets(state,occupied,result.data.feedback)'));
assert(source.includes('function addFeedbackField(field)'));
assert(source.includes('state.missingFields=FEEDBACK_FIELDS'));
assert(!source.includes('class="feedback-editor-grid"'));
assert(!source.includes('feedback-extra-fields'));
assert(source.includes('data-feedback-field="free_days"'));
const flatDetail = source.slice(source.indexOf('function salesFlatDetailHtml('), source.indexOf('function renderSalesTable('));
const rateDetail = source.slice(source.indexOf('function detailRow('), source.indexOf('function rowByDid('));
assert(flatDetail.includes('${extraFields}<div class="sales-detail-section detail-grid">'));
assert(!flatDetail.includes('feedbackSectionHtml'));
assert(rateDetail.includes('<div class="sales-detail"><div class="feedback-editor-host"></div>'));
assert(rateDetail.includes("+extraBlock+termsBlock"));
assert(!rateDetail.includes('feedbackSectionHtml'));
assert(!source.includes('function feedbackSectionHtml('));
assert(source.slice(source.indexOf('function quoteRow('),source.indexOf('function detailRow(')).includes("' '+FEEDBACK_DOT_HTML"));
assert(source.includes('sales-feedback-button') && source.includes('openRateFeedback(this') && source.includes('openSpaceFeedback(this'));
assert(!source.includes('>反馈</button>'));
let popover;
context.document.body = {appendChild(element) { popover=element; }};
context.document.getElementById = () => popover;
context.document.createElement = () => ({dataset:{},style:{setProperty() {}},listeners:{},
  setAttribute() {},addEventListener(name,handler) { this.listeners[name]=handler; },
  getBoundingClientRect() { return {width:300,height:160}; }});
context.window.innerWidth=800;
context.window.innerHeight=600;
const anchor = {dataset:{feedbackId:'fbc_1'},getBoundingClientRect() { return {left:150,top:250,width:70,bottom:275}; }};
context.indexSalesFeedback([row]);
context.showFeedbackPopover(anchor);
assert(popover.innerHTML.includes('客户确认') && popover.innerHTML.includes('销售甲'));
assert(popover.innerHTML.includes('>销</span>') && popover.innerHTML.includes('>撤回</button>'));
assert(!popover.innerHTML.includes('>驳回</button>'));
row.feedback_by_entry.frt_1[0].author_avatar_id = 'av2-w01-1';
context.showFeedbackPopover(anchor);
assert(popover.innerHTML.includes('/assets/avatars/av2-w01-1.png'));
context.S.auth.role = 'operator';
context.showFeedbackPopover(anchor);
assert(popover.innerHTML.includes('>驳回</button>'));
context.S.auth = {user_id:'usr_b',role:'sales'};
context.showFeedbackPopover(anchor);
assert(!popover.innerHTML.includes('data-feedback-action='));
assert(context.feedbackExtraFieldsHtml({entry_id:'frt_1',feedback:row.feedback_by_entry.frt_1},
  [['40HQ/HC','frt_1']],[],[]).includes('data-feedback-id="fbc_1"'));
assert(context.feedbackExtraFieldsHtml({entry_id:'sp_1',feedback:[{id:'fbc_valid',field:'validity_raw',
  old_value:'本周',suggested_value:'下周'}]},[['舱位','sp_1']],['pol','pod'],[]).includes('data-feedback-id="fbc_valid"'));
assert(source.includes("event.target.closest('input,button,a,.feedback-anchor')"));

const confirmation = source.slice(source.indexOf('function showFeedbackConfirmation()'), source.indexOf('async function refreshFeedbackView('));
let closed = 0;
let toasted = 0;
let modalOpened = 0;
let noteFocused = 0;
let noteScrolled = 0;
const confirmHost = {innerHTML: '', showModal() { modalOpened += 1; }};
const noteError = {hidden: true};
const noteInput = {setAttribute(name,value) { this[name]=value; }, focus() { noteFocused += 1; },
  scrollIntoView() { noteScrolled += 1; }};
const confirmContext = {
  activeFeedbackEditor: {host: {querySelector: selector =>
    selector === '#feedback-note' ? noteInput : selector === '#feedback-note-error' ? noteError : confirmHost}},
  feedbackDraft: () => ({changes: {}, expected: {}, note: ''}),
  closeFeedbackEditor: () => { closed += 1; },
  toast: () => { toasted += 1; },
  esc: value => String(value),
  FEEDBACK_LABELS: {price_usd: '价格 USD'},
};
vm.createContext(confirmContext);
vm.runInContext(confirmation, confirmContext);
confirmContext.showFeedbackConfirmation();
assert.strictEqual(closed, 1);
assert.strictEqual(toasted, 0);
assert.strictEqual(modalOpened, 0);
confirmContext.feedbackDraft = () => ({changes: {price_usd: '2550'}, expected: {price_usd: '2650'}, note: ''});
confirmContext.showFeedbackConfirmation();
assert.strictEqual(noteInput['aria-invalid'], 'true');
assert.strictEqual(noteFocused, 1);
assert.strictEqual(noteScrolled, 1);
assert.strictEqual(noteError.hidden, false);
assert.strictEqual(toasted, 0);
assert.strictEqual(modalOpened, 0);
confirmContext.feedbackDraft = () => ({changes: {price_usd: '2550'}, expected: {price_usd: '2650'}, note: '客户确认'});
confirmContext.showFeedbackConfirmation();
assert.strictEqual(closed, 1);
assert.strictEqual(modalOpened, 1);
assert(confirmHost.innerHTML.includes('class="btn-submit" onclick="submitItemFeedback()"'));
assert(source.includes("querySelector('.feedback-confirm-dialog .btn-submit')"));

const buttonState = source.slice(source.indexOf('function setFeedbackButtonState('), source.indexOf('async function startFeedbackEditor('));
const icon = {innerHTML: ''};
const button = {isConnected: true, dataset: {}, title: '', labels: {},
  setAttribute(name,value) { this.labels[name]=value; }, querySelector() { return icon; }};
const iconContext = {};
vm.createContext(iconContext);
vm.runInContext(buttonState, iconContext);
iconContext.setFeedbackButtonState(button,true);
assert.strictEqual(button.dataset.editing, '1');
assert.strictEqual(button.labels['aria-label'], '确认修订');
assert.strictEqual(icon.innerHTML, '&#10003;');
iconContext.setFeedbackButtonState(button,false);
assert.strictEqual(button.dataset.editing, '0');
assert.strictEqual(button.labels['aria-label'], '编辑反馈');
assert.strictEqual(icon.innerHTML, '&#9998;');
const dismissed = [];
context.dismissItemFeedback = async (submissionId,action) => dismissed.push([submissionId,action]);
const withdrawButton = {dataset:{submissionId:'fb_1',feedbackAction:'withdraw'},disabled:false,isConnected:true};
popover.listeners.click({target:{closest:()=>withdrawButton}}).then(() => {
  assert.deepStrictEqual(dismissed,[['fb_1','withdraw']]);
  assert.strictEqual(withdrawButton.disabled,false);
  console.log('PASS: feedback popover actions, price overlay, permissions, and confirmation flow');
}).catch(error => { console.error(error); process.exitCode=1; });
