const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1366,height:768}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/status') return route.fulfill({json:{ok:true,authenticated:true,auth_mode:'center',setup_required:false,features:{message_board:false},user:{user_id:'laptop-admin',role:'admin',views:['sales','ops','config','account'],name:'laptop-admin',display_name:'笔记本布局测试'}}});
      return route.fulfill({json:{ok:true,data:{rows:[],groups:[],total:0}}});
    });
    await page.goto('http://127.0.0.1:8795/', {waitUntil:'domcontentloaded'});
    await page.evaluate(() => {S.auth={user_id:'laptop-admin',role:'admin',views:['sales','ops','config','account'],name:'laptop-admin',display_name:'笔记本布局测试'};S.role='admin';S.opts={rates:{},space:{}};$('auth-modal').hidden=true;});

    async function layoutAt(width,height) {
      await page.setViewportSize({width,height});
      return page.evaluate(() => {
        const rail=$('sales-query-rail'),hero=$('sales-search-hero'),first=hero.querySelector('.filter-group');
        const railStyle=getComputedStyle(rail),heroStyle=getComputedStyle(hero);
        const heroRect=hero.getBoundingClientRect(),firstRect=first.getBoundingClientRect();
        return {
          railPosition:railStyle.position,
          heroOverflowY:heroStyle.overflowY,
          heroScrollTop:hero.scrollTop,
          heroClientHeight:hero.clientHeight,
          heroScrollHeight:hero.scrollHeight,
          firstTop:firstRect.top,
          heroTop:heroRect.top
        };
      });
    }

    for (const [width,height] of [[1366,768],[1280,720]]) {
      const layout=await layoutAt(width,height);
      assert.equal(layout.railPosition,'static',width+'x'+height+' query rail must follow page scroll');
      assert.equal(layout.heroOverflowY,'visible',width+'x'+height+' query form must not use clipped inner scrolling');
      assert.equal(layout.heroScrollTop,0);
      assert.ok(layout.firstTop>=layout.heroTop-1,'first query field must remain fully visible');
    }

    const large=await layoutAt(1440,1000);
    assert.equal(large.railPosition,'sticky','large desktop keeps sticky query rail');
    assert.equal(large.heroOverflowY,'auto','large desktop may scroll the rail when needed');
    assert.deepEqual(errors,[]);
    console.log('PASS: laptop query rail uses page scrolling without clipping its first field.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
