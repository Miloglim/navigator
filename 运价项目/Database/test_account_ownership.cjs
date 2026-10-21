const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/web/index.html','utf8');
function source(name){return html.match(new RegExp('(?:async )?function '+name+'\\([^\\n]*\\}'))?.[0] || html.match(new RegExp('(?:async )?function '+name+'\\([^]*?\\n}'))[0];}
const els={},calls=[];const $=id=>els[id]||={value:'',innerHTML:'',querySelectorAll:()=>[]};
const scopeButtons=['mine','all'].map(value=>({dataset:{ownership:value},active:false,pressed:'false',classList:{toggle(name,on){assert.equal(name,'active');this.owner.active=on;},owner:null},setAttribute(name,value){assert.equal(name,'aria-pressed');this.pressed=value;}}));
scopeButtons.forEach(button=>button.classList.owner=button);
const ctx=vm.createContext({S:{auth:{role:'operator'},operators:[],chatGroups:[],master:{routes:[]},opts:{rates:{pol:[]}},operatorsAll:[{name:'Alice',role:'operator',routes_list:['南美东']}]},PD:{page:4,hints:{}},mgPage:3,$,esc:String,jsArg:JSON.stringify,document:{querySelectorAll:selector=>selector==='#ops-ownership [data-ownership]'?scopeButtons:[]},
pdSaveBeforeDisplayChange:async()=>true,initManageFilters:()=>calls.push('filters'),loadPending:async()=>calls.push('pending'),loadManage:async()=>calls.push('manage'),loadGroupStats:async()=>calls.push('stats'),refreshQueueDots:async()=>calls.push('dots')});
for(const name of ['opsOwnership','renderOpsOwnership','opsUrl','opsIdParams','setOpsOwnership','accountRoleLabel','renderOpList','renderAdminForms']) vm.runInContext(source(name),ctx);
(async()=>{
assert.equal(ctx.opsOwnership(),'mine');assert.ok(ctx.opsUrl('/api/pending').endsWith('ownership=mine'));
ctx.renderOpsOwnership();assert.equal(scopeButtons[0].active,true);assert.equal(scopeButtons[1].active,false);
$('mg-group').value='unrelated-group';
await ctx.setOpsOwnership('all');assert.equal(ctx.opsOwnership(),'all');assert.equal(scopeButtons[0].active,false);assert.equal(scopeButtons[1].active,true);assert.equal(scopeButtons[1].pressed,'true');assert.equal(ctx.PD.page,1);assert.equal(ctx.mgPage,1);assert.equal($('mg-group').value,'');assert.deepEqual(calls,['filters','pending','manage','stats','dots']);
ctx.renderOpList();assert.ok($('op-list').innerHTML.includes('涉及航线（自动）'));assert.ok(!$('op-list').innerHTML.includes('登录可见群'));assert.ok(!$('op-list').innerHTML.includes('群 或 航线'));
ctx.renderAdminForms();assert.ok(!$('op-form').innerHTML.includes('data-chipbox'));assert.ok(!$('op-form').innerHTML.includes('match_mode'));
for(const role of ['admin','sales']){ctx.S.auth.role=role;ctx.S.opsOwnership='mine';assert.equal(ctx.opsOwnership(),'all');}
assert.match(html,/id="ops-ownership" aria-label="条目范围">[\s\S]*data-ownership="mine"[\s\S]*data-ownership="all"/);
assert.doesNotMatch(html,/<select id="ops-ownership"/);
assert.match(html,/p\.set\('include_source_groups', '1'\)/);
assert.match(html,/const mgSourceGroupKey = \(kind=mgKind\) => `\$\{S\.auth\?\.user_id \|\| ''}:\$\{opsOwnership\(\)}:\$\{kind}`/);
assert.match(html,/if \(sequence !== mgLoadSequence\) return;/);
assert.match(html,/fillMgSelect\('mg-group', '来源群·全部', mgSourceGroups\[sourceGroupKey\]\)/);
console.log('PASS: structured operator scope control, mine/all behavior and retired columns removed');
})().catch(e=>{console.error(e);process.exitCode=1;});
