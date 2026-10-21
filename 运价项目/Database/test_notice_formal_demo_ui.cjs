const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const path=require('node:path');

const PYTHON=process.env.FREIGHT_TEST_PYTHON||'C:/Users/YQN/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe';

async function fixtureServer() {
  const child=spawn(PYTHON,['-u','test_notices.py','--serve'],{cwd:__dirname,env:{...process.env,PYTHONIOENCODING:'utf-8'},windowsHide:true});
  const fixture=await new Promise((resolve,reject)=>{
    let output='';
    const timer=setTimeout(()=>reject(new Error('fixture timeout')),30000);
    child.once('exit',()=>{clearTimeout(timer);reject(new Error('fixture exited'));});
    child.stdout.on('data',chunk=>{
      output+=chunk;
      const line=output.split('\n').find(value=>value.startsWith('{"url":'));
      if(line) {clearTimeout(timer);resolve(JSON.parse(line));}
    });
  });
  return {child,fixture};
}

(async()=>{
  const {child,fixture}=await fixtureServer();
  let browser;
  try {
    browser=await chromium.launch({headless:true,channel:'msedge'});
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    const [name,value]=fixture.cookies.admin.split('=');
    await context.addCookies([{name,value,url:fixture.url}]);
    const page=await context.newPage();
    const errors=[],writes=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.method()!=='GET'&&request.url().includes('/api/notices'))writes.push(request.url());});
    await page.goto(fixture.url+'/?notice_demo=30');
    await page.waitForFunction(()=>NOTICE_UI.total===30);
    await page.screenshot({path:path.join(__dirname,'ops/reports/notice-mixed-30-20261016/formal-demo.png')});
    assert.equal(await page.locator('#cat-tabs button.active').getAttribute('data-tab'),'dyn');
    assert.equal(await page.locator('.notice-post').count(),20);
    assert.equal(await page.locator('.notice-post-lead').count(),1);
    assert.equal(await page.locator('.notice-post-brief').count(),3);
    assert.equal(await page.locator('.notice-group-section').count(),0);
    assert.equal(await page.getByText('演示模式：30 条模拟公告').count(),1);
    assert.equal(await page.locator('.notice-post-actions').count(),0);
    await page.getByRole('button',{name:'下一页'}).click();
    assert.equal(await page.locator('.notice-post').count(),10);
    await page.getByRole('button',{name:'上一页'}).click();
    const groups=page.locator('[data-scope-context="filter"][data-scope-dimension="groups"]');
    await groups.locator('summary').click();
    await groups.getByLabel('演示·欧洲价格群',{exact:true}).check();
    await groups.locator('summary').click();
    const routes=page.locator('[data-scope-context="filter"][data-scope-dimension="routes"]');
    await routes.locator('summary').click();
    await routes.getByLabel('南美东',{exact:true}).check();
    await routes.locator('summary').click();
    await page.getByRole('button',{name:'应用筛选并搜索'}).click();
    assert.equal(await page.evaluate(()=>NOTICE_UI.total),15);
    await page.locator('.notice-story summary').first().click();
    assert.equal(await page.locator('.notice-story').first().getAttribute('open'),'');
    for(const width of [1440,390,320]) {
      await page.setViewportSize({width,height:800});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`overflow ${width}`);
    }
    assert.deepEqual(errors,[]);
    assert.deepEqual(writes,[]);
    const normal=await context.newPage();
    await normal.goto(fixture.url+'/');
    await normal.locator('#cat-tabs [data-tab="dyn"]').click();
    await normal.waitForFunction(()=>document.querySelector('.notice-paper'));
    assert.equal(await normal.locator('.notice-post').count(),0);
    assert.equal(await normal.getByText('演示模式：30 条模拟公告').count(),0);
    console.log('PASS: formal notice page demo 30; normal page untouched; no notice writes');
  } finally {if(browser)await browser.close();child.kill();}
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
