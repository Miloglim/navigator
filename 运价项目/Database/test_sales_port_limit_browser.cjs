const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/status') return route.fulfill({json:{ok:true,authenticated:true,auth_mode:'center',setup_required:false,features:{message_board:false},user:{user_id:'port-limit-admin',role:'admin',views:['sales','ops','config','account'],name:'port-limit-admin',display_name:'港口上限测试'}}});
      return route.fulfill({json:{ok:true,data:{rows:[],groups:[],total:0}}});
    });
    await page.goto('http://127.0.0.1:8795/', {waitUntil:'domcontentloaded'});

    assert.equal(await page.locator('label').filter({hasText:'起运港'}).first().textContent(), '起运港 最多 3 个');
    assert.equal(await page.locator('label').filter({hasText:'目的港'}).first().textContent(), '目的港 最多 3 个');
    const inputLayout = await page.evaluate(() => {
      const check = input => {
        const style = getComputedStyle(input);
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d');
        context.font = style.font;
        const textWidth = context.measureText(input.placeholder).width;
        const available = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        return {available, textWidth};
      };
      return {pol:check($('sf-pol')),pod:check($('sf-pod'))};
    });
    for (const layout of [inputLayout.pol,inputLayout.pod]) {
      assert.ok(layout.available >= layout.textWidth, 'port placeholder must fit inside the input');
    }

    const result = await page.evaluate(() => {
      S.auth={user_id:'port-limit-admin',role:'admin',views:['sales','ops','config','account'],name:'port-limit-admin',display_name:'港口上限测试'};
      S.role='admin'; S.opts={rates:{},space:{}};
      $('auth-modal').hidden=true;
      $('sf-pol').value='Shanghai,Ningbo,Santos,Paranagua';
      commitAllTags();
      const pol={tags:[...S.sales.polTags],chips:$('tags-pol').querySelectorAll('.tag-chip').length,message:$('toast').textContent};
      $('sf-pod').value='Los Angeles,Long Beach,Oakland,Seattle';
      commitAllTags();
      const pod={tags:[...S.sales.podTags],chips:$('tags-pod').querySelectorAll('.tag-chip').length,message:$('toast').textContent};
      return {pol,pod,polQuery:salesQueryParams().get('pol'),podQuery:salesQueryParams().get('pod')};
    });

    assert.deepEqual(result.pol.tags,['Shanghai','Ningbo','Santos']);
    assert.equal(result.pol.chips,3);
    assert.equal(result.pol.message,'起运港最多选择 3 个');
    assert.deepEqual(result.pod.tags,['Los Angeles','Long Beach','Oakland']);
    assert.equal(result.pod.chips,3);
    assert.equal(result.pod.message,'目的港最多选择 3 个');
    assert.equal(result.polQuery,'Shanghai,Ningbo,Santos');
    assert.equal(result.podQuery,'Los Angeles,Long Beach,Oakland');
    assert.deepEqual(errors,[]);
    console.log('PASS: browser enforces three POL/POD chips and sends only the first three ports.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
