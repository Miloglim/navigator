// Render both server generations; the new binding flow must not call absent APIs before release.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/admin-terminal.js','utf8');
const context={window:{},console,setTimeout,clearTimeout};
vm.createContext(context);
vm.runInContext(source.replace('return {enter:module=>', 'window.fixture={personBody,state};return {enter:module=>'),context);
context.window.fixture.state.tab='identity';
const account={name:'fixture',publisher_name:'来源姓名',publisher_groups:[],user_id:'usr_fixture',ownership_basis:'fixture'};
const old=context.window.fixture.personBody(account);
assert.ok(!old.includes('at-publisher-sources'));
assert.ok(!old.includes('pub_fixture'));
const upgraded=context.window.fixture.personBody({...account,publisher_id:'pub_fixture'});
assert.ok(upgraded.includes('at-publisher-sources'));
assert.ok(upgraded.includes('pub_fixture'));
assert.ok(upgraded.includes('同名不能作为同一人的依据'));
assert.ok(upgraded.includes('本人报价来源'));
assert.ok(!upgraded.includes('已绑定的报价来源群'));
assert.ok(upgraded.includes('按稳定报价编号筛选“我的条目”'));
assert.ok(upgraded.includes('不按群名筛选'));
console.log('PASS: legacy server remains usable; binding UI appears only after schema upgrade');

assert.ok(!source.includes('data-at-bind'));
assert.ok(upgraded.includes('换群发价无需再次关联'));
