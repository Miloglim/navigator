const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/index.html', 'utf8');
assert(source.includes('id="company-refresh-toggle" type="checkbox"'));
assert(source.includes('id="company-refresh-check"'));
assert(!source.includes('id="company-refresh-disable"'));
const start = source.indexOf('const COMPANY_TOKEN_LIFETIME_MS=');
const end = source.indexOf("setInterval(() => { if ($('company-token-refresh-status')", start);
assert(start >= 0 && end > start);

let enabled = true;
let canResume = false;
let openedUrl = '';
let authenticatedCheck = false;
const calls = [];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {checked:false, disabled:false, hidden:false, textContent:''});
  return elements.get(id);
}
const context = {
  $: element,
  Date,
  Math,
  Number,
  String,
  setInterval() {},
  window: { open: () => ({ opener: {}, close() {}, location: { replace: url => { openedUrl = url; } } }) },
  fetch: async () => ({ok:true, json:async () => ({ok:true, data:{
    auto_refresh:enabled, can_resume:canResume, configured:true, expires_at:new Date(Date.now()+86400000).toISOString(),
    last_success_at:new Date().toISOString(), connection_state:'idle', last_error:null,
  }})}),
  postJSON: async (path) => {
    calls.push(path);
    if (path.endsWith('/disable')) { enabled = false; canResume = true; }
    if (path.endsWith('/enable') || path.endsWith('/connect')) { enabled = true; canResume = false; }
    return {ok:true, data:{last_error:null, ...(path.endsWith('/connect') ? (authenticatedCheck ? {authenticated:true,auth_url:null} : {authenticated:false,auth_url:'https://login.dingtalk.com/oauth2/auth?x=ui'}) : {})}};
  },
  loadCompanyCredentialStatus() {},
  toast() {},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

(async () => {
  await context.loadCompanyTokenRefreshStatus();
  assert.equal(element('company-refresh-toggle').checked, true);
  assert.equal(element('company-refresh-now').disabled, false);
  assert.equal(element('company-refresh-connect').hidden, false);

  element('company-refresh-toggle').checked = false;
  await context.toggleCompanyTokenAutoRefresh(element('company-refresh-toggle'));
  assert.equal(calls.at(-1), '/api/admin/company-token-refresh/disable');
  assert.equal(element('company-refresh-toggle').checked, false);
  assert.equal(element('company-refresh-now').disabled, true);

  element('company-refresh-toggle').checked = true;
  await context.toggleCompanyTokenAutoRefresh(element('company-refresh-toggle'));
  assert.equal(calls.at(-1), '/api/admin/company-token-refresh/enable');
  assert.equal(element('company-refresh-toggle').checked, true);

  await context.refreshCompanyTokenNow();
  assert.equal(calls.at(-1), '/api/admin/company-token-refresh/now');

  enabled = false;
  canResume = false;
  await context.loadCompanyTokenRefreshStatus();
  element('company-refresh-toggle').checked = true;
  await context.toggleCompanyTokenAutoRefresh(element('company-refresh-toggle'));
  assert.equal(calls.at(-1), '/api/admin/company-token-refresh/connect');
  assert.match(openedUrl, /login\.dingtalk\.com/);
  authenticatedCheck = true;
  openedUrl = '';
  await context.checkCompanyKnowledgeLogin();
  assert.equal(openedUrl, '');
  console.log('company token refresh UI: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
