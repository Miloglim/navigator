const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('web/index.html', 'utf8');
const source = html.slice(html.indexOf('async function submitAuth(ev)'), html.indexOf('async function logoutAuth()'));

async function scenario(registerResult, loginResult) {
  const fields = new Map();
  const field = id => {
    if (!fields.has(id)) fields.set(id, {value: '', textContent: '', disabled: false, hidden: false});
    return fields.get(id);
  };
  field('auth-name').value = 'new-sales';
  field('auth-password').value = 'fixture-password';
  field('auth-confirm').value = 'fixture-password';
  const calls = [];
  const context = {
    $: field,
    authMode: 'register',
    S: {auth: null},
    loginRequest: async (path, _headers, body) => {
      calls.push({path, body: JSON.parse(body)});
      return path === '/api/auth/register' ? registerResult : loginResult;
    },
    openAuthMode: mode => {
      context.authMode = mode;
      field('auth-password').value = '';
      field('auth-confirm').value = '';
    },
    authNotice: message => { field('auth-notice').textContent = message; },
    refreshAuth: async () => { calls.push({path: 'refreshAuth'}); },
    init: async () => { calls.push({path: 'init'}); },
    toast: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await context.submitAuth({preventDefault() {}});
  return {calls, fields, mode: context.authMode};
}

(async () => {
  const registered = {response: {ok: true}, data: {ok: true, data: {message: 'created'}}};
  const loggedIn = {response: {ok: true}, data: {ok: true}};
  const success = await scenario(registered, loggedIn);
  assert.deepEqual(success.calls.map(call => call.path), [
    '/api/auth/register', '/api/auth/login', 'refreshAuth', 'init',
  ]);
  assert.equal(success.calls[1].body.name, 'new-sales');
  assert.equal(success.calls[1].body.password, 'fixture-password');
  assert.equal(success.fields.get('auth-password').value, '');

  const duplicate = await scenario({response: {ok: false}, data: {ok: false, error: 'duplicate'}}, loggedIn);
  assert.deepEqual(duplicate.calls.map(call => call.path), ['/api/auth/register']);
  assert.equal(duplicate.fields.get('auth-error').textContent, 'duplicate');

  const failedLogin = await scenario(registered, {response: {ok: false}, data: {ok: false, error: 'busy'}});
  assert.deepEqual(failedLogin.calls.map(call => call.path), ['/api/auth/register', '/api/auth/login']);
  assert.equal(failedLogin.mode, 'login');
  assert.equal(failedLogin.fields.get('auth-name').value, 'new-sales');
  assert.match(failedLogin.fields.get('auth-notice').textContent, /账号已创建/);
  console.log('PASS: registration signs in immediately, while errors retain a usable login path.');
})().catch(error => { console.error(error); process.exitCode = 1; });
