const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    const posts=[{id:'msg_existing',user_id:'usr_sales',author_name:'销售甲',content:'<b>不会当 HTML 执行</b>',created_at:'2026-10-22 16:00:00'}];
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.route('**/api/**', async route => {
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/auth/status')return route.fulfill({json:{ok:true,authenticated:false,auth_mode:'center',setup_required:false,features:{message_board:true},user:null}});
      if(path==='/api/message-board')return route.fulfill({json:{ok:true,data:{rows:posts,total:posts.length,limit:50,offset:0}}});
      if(path==='/api/message-board/create'){
        const body=route.request().postDataJSON();
        const post={id:'msg_new',user_id:'usr_sales',author_name:'销售甲',content:body.content,created_at:'2026-10-22 16:01:00'};
        posts.unshift(post);
        return route.fulfill({json:{ok:true,data:{post}}});
      }
      return route.fulfill({json:{ok:false,error:'fixture'}});
    });
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#auth-modal:not([hidden])');
    await page.evaluate(() => {
      S.auth={user_id:'usr_sales',role:'sales',views:['sales'],name:'sales-fixture',display_name:'销售甲'};
      S.role='sales';$('auth-modal').hidden=true;applyAccountAccess();switchRole('sales');
      setCatTab(document.querySelector('#cat-tabs [data-tab="board"]'));
    });
    await page.waitForSelector('.message-board');
    assert.equal(await page.locator('.message-board-post').count(),1);
    assert.equal(await page.locator('.message-board-post b').count(),0,'message content must render as text, not HTML');
    assert.match(await page.locator('.message-board-post').innerText(),/<b>不会当 HTML 执行<\/b>/);
    await page.locator('#message-board-content').fill('新留言');
    await page.getByRole('button',{name:'发布',exact:true}).click();
    await page.waitForFunction(() => document.querySelectorAll('.message-board-post').length===2);
    assert.equal(await page.locator('#message-board-content').inputValue(),'');
    assert.match(await page.locator('.message-board-list').innerText(),/新留言/);
    fs.mkdirSync('.work',{recursive:true});
    await page.screenshot({path:'.work/message-board-ui.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS: message board tab renders, escapes content, publishes, and refreshes.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
