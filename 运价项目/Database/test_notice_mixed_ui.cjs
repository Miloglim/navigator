// Cross-scope announcement fixture; all writes stay in the disposable SQLite server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const PYTHON = process.env.FREIGHT_TEST_PYTHON || 'C:/Users/YQN/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe';
const OUTPUT = path.join(__dirname,'ops/reports/notice-mixed-30-20261016');
const GROUPS = ['Fixture group','Fixture group 2','Fixture LATAM chat','Fixture Europe chat','Fixture Africa chat'];
const ROUTES = ['Fixture Europe','Fixture Americas','Fixture Africa','Fixture Intra-Asia'];
const TYPES = ['全体','单群','多群','单航线','群聊与航线','多群与多航线'];
const COUNT = 30;
const PAGE_SIZE = 20;

function scope(index) {
  const type=Math.floor(index/5),slot=index%5;
  if(type===0) return {groups:[],routes:[]};
  if(type===1) return {groups:[GROUPS[slot]],routes:[]};
  if(type===2) return {groups:[GROUPS[slot],GROUPS[(slot+1)%GROUPS.length]],routes:[]};
  if(type===3) return {groups:[],routes:[ROUTES[slot%ROUTES.length]]};
  if(type===4) return {groups:[GROUPS[slot]],routes:[ROUTES[slot%ROUTES.length]]};
  return {groups:[GROUPS[slot],GROUPS[(slot+2)%GROUPS.length]],
    routes:[ROUTES[slot%ROUTES.length],ROUTES[(slot+1)%ROUTES.length]]};
}

function expectedFor(rows,groups,routes) {
  return rows.filter(row=>(!row.groups.length&&!row.routes.length)
    ||row.groups.some(group=>groups.includes(group))
    ||row.routes.some(route=>routes.includes(route)));
}

async function fixtureServer() {
  const child=spawn(PYTHON,['-u','test_notices.py','--serve'],{
    cwd:__dirname,env:{...process.env,PYTHONIOENCODING:'utf-8'},windowsHide:true,
  });
  const fixture=await new Promise((resolve,reject)=>{
    let buffer='';
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),30000);
    child.once('exit',()=>{clearTimeout(timer);reject(new Error('Fixture exited before startup'));});
    child.stdout.on('data',chunk=>{
      buffer+=chunk;
      for(const line of buffer.split('\n')) {
        if(line.startsWith('{"url":')) {clearTimeout(timer);resolve(JSON.parse(line));return;}
      }
    });
    child.stderr.on('data',()=>{}); // The fixture can print session details on failure.
  });
  return {child,fixture};
}

(async()=>{
  const {child,fixture}=await fixtureServer();
  let browser;
  try {
    browser=await chromium.launch({headless:true,channel:'msedge'});
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    const [name,value]=fixture.cookies.sales.split('=');
    await context.addCookies([{name,value,url:fixture.url}]);
    const created=[];
    for(let index=0;index<COUNT;index++) {
      const {groups,routes}=scope(index);
      const title=`${TYPES[Math.floor(index/5)]}公告 ${String(index+1).padStart(2,'0')}`;
      const response=await context.request.post(fixture.url+'/api/notices/save',{data:{
        title,
        body_html:`<p>第 ${index+1} 条交叉范围测试消息。船期、有效期和报价变化以该公告的发布范围为准。</p>`,
        target_groups:groups,target_routes:routes,tags:[TYPES[Math.floor(index/5)]],
      }});
      const result=await response.json();
      assert.equal(response.status(),200,`${title}: ${JSON.stringify(result)}`);
      created.push({id:result.data.id,title,groups,routes});
    }
    assert.equal(new Set(created.map(row=>row.id)).size,COUNT);

    const list=async params=>{
      const response=await context.request.get(fixture.url+'/api/notices?'+new URLSearchParams(params));
      assert.equal(response.status(),200);
      const result=await response.json();
      assert.equal(result.ok,true);
      return result.data;
    };
    const first=await list({limit:PAGE_SIZE,offset:0});
    const second=await list({limit:PAGE_SIZE,offset:PAGE_SIZE});
    assert.equal(first.total,COUNT);
    assert.equal(first.rows.length,PAGE_SIZE);
    assert.equal(second.rows.length,COUNT-PAGE_SIZE);
    assert.deepEqual(new Set([...first.rows,...second.rows].map(row=>row.id)),new Set(created.map(row=>row.id)));
    assert.deepEqual(new Set(first.groups),new Set(GROUPS));
    assert.deepEqual(new Set(first.routes),new Set(ROUTES));

    const page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(fixture.url);
    await page.locator('#cat-tabs [data-tab="dyn"]').click();
    await page.waitForFunction(()=>NOTICE_UI.total===30);
    assert.equal(await page.locator('.notice-post').count(),PAGE_SIZE);
    assert.equal(await page.locator('.notice-page').textContent().then(text=>text.includes('1 / 2')),true);
    const verifyMixed=async()=>{
      const post=page.locator('.notice-post').filter({hasText:'多群与多航线公告'}).first();
      if(!(await post.count())) return false;
      assert.match(await post.locator('.notice-targets').textContent(),/航线 2 · 群聊 2/);
      assert.match(await post.locator('.notice-targets').getAttribute('title'),/航线 .*群聊 /);
      return true;
    };
    let mixedVerified=await verifyMixed();
    fs.mkdirSync(OUTPUT,{recursive:true});
    await page.screenshot({path:path.join(OUTPUT,'desktop-page-1.png')});
    await page.getByRole('button',{name:'下一页'}).click();
    await page.waitForFunction(()=>NOTICE_UI.page===2&&NOTICE_UI.rows.length===10);
    assert.equal(await page.locator('.notice-post').count(),10);
    mixedVerified=(await verifyMixed())||mixedVerified;
    assert.equal(mixedVerified,true);
    await page.screenshot({path:path.join(OUTPUT,'desktop-page-2.png')});
    await page.getByRole('button',{name:'上一页'}).click();
    await page.waitForFunction(()=>NOTICE_UI.page===1&&NOTICE_UI.rows.length===20);

    const selectedGroups=[GROUPS[0],GROUPS[2]],selectedRoutes=[ROUTES[1]];
    const expected=expectedFor(created,selectedGroups,selectedRoutes);
    const params={limit:PAGE_SIZE,groups:JSON.stringify(selectedGroups),routes:JSON.stringify(selectedRoutes)};
    const filtered=await list(params);
    assert.equal(filtered.total,expected.length);
    assert.deepEqual(new Set(filtered.rows.map(row=>row.id)),new Set(expected.map(row=>row.id)));
    const selector=dimension=>page.locator(`[data-scope-context="filter"][data-scope-dimension="${dimension}"]`);
    for(const [dimension,values] of [['groups',selectedGroups],['routes',selectedRoutes]]) {
      const menu=selector(dimension);
      await menu.locator('summary').click();
      for(const value of values) await menu.getByLabel(value,{exact:true}).check();
      await menu.locator('summary').click();
    }
    await page.getByRole('button',{name:'应用筛选并搜索'}).click();
    await page.waitForFunction(total=>NOTICE_UI.total===total,expected.length);
    assert.equal(await page.locator('.notice-post').count(),expected.length);
    const visibleIds=await page.locator('.notice-post').evaluateAll(nodes=>nodes.map(node=>node.dataset.noticeId));
    assert.deepEqual(new Set(visibleIds),new Set(expected.map(row=>row.id)));
    assert.equal(visibleIds.length,new Set(visibleIds).size);
    for(const width of [1440,820,390,320]) {
      await page.setViewportSize({width,height:width<=390?800:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`overflow at ${width}px`);
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(OUTPUT,'mobile-filtered.png')});
    assert.deepEqual(errors,[]);
    console.log(`PASS: 30 mixed notices; 6 scope types, 5 groups, 4 routes, 20+10 pages, union filters and 4 viewports`);
    console.log(`Filtered group x route view: ${expected.length} unique notices`);
  } finally {
    if(browser) await browser.close();
    child.kill();
  }
})().catch(error=>{console.error(error.stack||error.message);process.exitCode=1;});
