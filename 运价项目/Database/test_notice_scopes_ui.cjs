// Browser smoke checks against a disposable SQLite HTTP fixture only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const PYTHON = process.env.FREIGHT_TEST_PYTHON || 'C:/Users/YQN/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe';
const OUTPUT = path.join(__dirname,'ops/reports/notice-scopes-20261012');

(async()=>{
  const child=spawn(PYTHON,['-u','test_notices.py','--serve'],{cwd:__dirname,env:{...process.env,PYTHONIOENCODING:'utf-8'},windowsHide:true});
  let browser;
  try {
    const fixture=await new Promise((resolve,reject)=>{
      let buffer='';
      const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),30000);
      child.on('exit',()=>{clearTimeout(timer);reject(new Error('Fixture exited before startup'));});
      child.stdout.on('data',chunk=>{
        buffer+=chunk;
        for(const line of buffer.split('\n')) {
          if(line.startsWith('{"url":')) {clearTimeout(timer);resolve(JSON.parse(line));return;}
        }
      });
      child.stderr.on('data',()=>{}); // Fixture error output can contain session information.
    });
    browser=await chromium.launch({headless:true,channel:'msedge'});
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    const [name,value]=fixture.cookies.sales.split('=');
    await context.addCookies([{name,value,url:fixture.url}]);
    const page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const make=async(title,scope={})=>{
      const response=await context.request.post(fixture.url+'/api/notices/save',{data:{title,body_html:'<p>船期及价格更新，请核对本周有效期。详细内容完整保留，销售可按航线和群聊快速筛选。</p>',...scope}});
      assert.equal(response.status(),200);assert.equal((await response.json()).ok,true);
    };
    for(let i=0;i<10;i++) await make('航线更新 '+(i+1),{target_groups:['Fixture group'],target_routes:['Fixture Europe']});
    await make('其他航线',{target_routes:['Fixture Americas']});
    const image='data:image/png;base64,'+Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082','hex').toString('base64');
    await make('全体通知',{body_html:'<p>全体通知原图</p><img src="'+image+'">'});
    await page.goto(fixture.url);
    await page.locator('#cat-tabs [data-tab="dyn"]').click();
    await page.locator('.notice-post-lead').first().waitFor();
    await page.waitForFunction(()=>Math.abs(document.querySelector('#pager').getBoundingClientRect().bottom-innerHeight)<=2);
    assert.equal(await page.locator('.notice-post').count(),12);
    assert.equal(await page.locator('.notice-post-lead').count(),1);
    assert.equal(await page.locator('.notice-post-brief').count(),3);
    assert.equal(await page.locator('.notice-post-standard').count(),8);
    assert.equal(await page.locator('.notice-feature-secondary .notice-post-standard').count(),2);
    assert.equal(await page.locator('.notice-below .notice-post-standard').count(),6);
    assert.equal(await page.locator('.notice-story-marker').count(),0);
    const selector=dimension=>page.locator('[data-scope-context="filter"][data-scope-dimension="'+dimension+'"]');
    await selector('routes').locator('summary').click();
    await selector('routes').getByLabel('Fixture Europe',{exact:true}).check();
    await selector('routes').getByLabel('Fixture Americas',{exact:true}).check();
    assert.equal(await selector('routes').locator('.notice-scope-summary').textContent(),'已选 2');
    await selector('routes').getByLabel('Fixture Americas',{exact:true}).uncheck();
    await selector('routes').locator('summary').click();
    await page.getByRole('button',{name:'应用筛选并搜索',exact:true}).click();
    await page.waitForFunction(()=>NOTICE_UI.total===11);
    await page.locator('.notice-story summary').first().click();
    assert.equal(await page.locator('.notice-story[open] .notice-content').count(),1);
    await page.locator('.notice-story summary').first().click();
    const imagePost=page.locator('.notice-post').filter({has:page.getByRole('heading',{name:'全体通知',exact:true})});
    const featuredImage=imagePost.locator('.notice-lead-media img');
    if(await featuredImage.count() && await featuredImage.evaluate(image=>image.naturalWidth>=32)) await featuredImage.click();
    else {
      await imagePost.locator('summary').click();
      await imagePost.locator('.notice-content img').focus();
      await page.keyboard.press('Enter');
    }
    await page.locator('.notice-image-dialog[open]').waitFor();
    await page.keyboard.press('Escape');
    if(!(await imagePost.locator('.notice-story').evaluate(el=>el.open))) await imagePost.locator('summary').click();
    assert.equal(await imagePost.locator('.notice-content img').count(),1);
    await imagePost.locator('summary').click();
    fs.mkdirSync(OUTPUT,{recursive:true});
    await page.evaluate(()=>document.querySelector('.notice-paper').scrollTop=0);
    await page.screenshot({path:path.join(OUTPUT,'desktop.png')});
    await page.setViewportSize({width:820,height:860});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.setViewportSize({width:1440,height:900});
    await page.locator('#notice-fab').click();
    await page.locator('#notice-compose-title').fill('多范围发布测试');
    await page.locator('#notice-editor').fill('多群、多航线测试公告');
    for(const dimension of ['groups','routes']) {
      const menu=page.locator('[data-scope-context="compose"][data-scope-dimension="'+dimension+'"]');
      await menu.locator('summary').click();
      for(const checkbox of await menu.locator('input[type="checkbox"]').all()) await checkbox.check();
      await menu.locator('summary').click();
    }
    await page.locator('#notice-publish').click();
    await page.waitForFunction(()=>NOTICE_UI.rows.some(row=>row.title==='多范围发布测试'));
    const published=await page.evaluate(()=>NOTICE_UI.rows.find(row=>row.title==='多范围发布测试'));
    const catalogSize=await page.evaluate(()=>({groups:NOTICE_UI.groups.length,routes:NOTICE_UI.routes.length}));
    assert.equal(published.target_groups.length,catalogSize.groups);assert.equal(published.target_routes.length,catalogSize.routes);
    await page.locator('[data-notice-id="'+published.id+'"] [data-action="edit"]').click();
    assert.equal(await page.locator('#notice-compose-scope input:checked').count(),catalogSize.groups+catalogSize.routes);
    await page.screenshot({path:path.join(OUTPUT,'composer.png')});
    await page.evaluate(()=>noticeCollapse());
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(200);
    assert.equal(await page.locator('.notice-post').count(),12);
    assert.equal(await page.evaluate(()=>Math.abs(document.querySelector('#pager').getBoundingClientRect().bottom-innerHeight)<=2),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await selector('groups').locator('summary').click();
    const box=await selector('groups').locator('.notice-scope-menu').boundingBox();
    assert.ok(box.x>=0&&box.x+box.width<=390);
    await selector('groups').locator('summary').click();
    await page.screenshot({path:path.join(OUTPUT,'mobile.png'),fullPage:true});
    await page.setViewportSize({width:320,height:700});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.setViewportSize({width:390,height:844});
    await page.locator('#notice-search').fill('no-matching-notice-fixture');
    await page.getByRole('button',{name:'应用筛选并搜索',exact:true}).click();
    await page.waitForFunction(()=>NOTICE_UI.total===0);
    await page.waitForFunction(()=>Math.abs(document.querySelector('#pager').getBoundingClientRect().bottom-innerHeight)<=2);
    await page.setViewportSize({width:1440,height:900});
    await page.waitForFunction(()=>Math.abs(document.querySelector('#pager').getBoundingClientRect().bottom-innerHeight)<=2);
    assert.deepEqual(errors,[]);
    console.log('PASS: desktop/mobile density, multi-select filtering, publishing, editing and expansion; no browser errors');
  } finally {
    if(browser) await browser.close();
    child.kill();
  }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
