const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => route.fulfill({json:{ok:true,data:{rows:[]}}}));
    await page.route('http://127.0.0.1:8795/', route => route.fulfill({
      contentType:'text/html', body:fs.readFileSync('web/index.html','utf8')
    }));
    await page.goto('http://127.0.0.1:8795/', {waitUntil:'networkidle'});
    await page.evaluate(() => {
      S.auth={user_id:'quote-library-fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      setCatTab(document.querySelector('#cat-tabs [data-tab="quote"]'));
      quoteEditorToggleLibrary(true);
    });

    assert.equal(await page.locator('.quote-library-columns').count(),1);
    assert.deepEqual(await page.locator('.quote-library-columns > section h3').allInnerTexts(),['最近历史报价','报价表模板']);
    assert.equal(await page.getByText('暂无记录',{exact:true}).count(),2,'both library types must remain visible when empty');

    await page.evaluate(() => {
      const draft=quoteEditorLoad();
      localStorage.setItem(quoteEditorLibraryKey(),JSON.stringify({
        snapshots:[{id:'history-fixture',name:'历史报价一',createdAt:'2026-10-21T03:00:00.000Z',draft}],
        templates:[{id:'template-fixture',name:'报价模板一',createdAt:'2026-10-21T03:01:00.000Z',draft}]
      }));
      quoteEditorToggleLibrary(true);
    });

    const measure = () => page.locator('.quote-library-columns > section').evaluateAll(nodes => nodes.map(node => {
      const box=node.getBoundingClientRect();
      return {x:box.x,y:box.y,width:box.width,height:box.height};
    }));
    const desktopColumns=await measure();
    assert.equal(desktopColumns.length,2);
    assert(desktopColumns[1].x>desktopColumns[0].x&&Math.abs(desktopColumns[1].y-desktopColumns[0].y)<1,'history and templates must stay side by side');
    assert(desktopColumns.every(column => column.width>0));

    const itemOverlap=await page.locator('.quote-library-item').evaluateAll(items => items.map(item => {
      const text=item.querySelector('strong').getBoundingClientRect();
      const button=item.querySelector('button').getBoundingClientRect();
      return text.right>button.left;
    }));
    assert(itemOverlap.every(overlap => !overlap),'history/template text must not overlap its restore button');

    await page.screenshot({path:'.work/quote-library-columns-1440.png'});
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(() => {quoteEditorToggleLibrary(false); quoteEditorToggleLibrary(true);});
    const mobileColumns=await measure();
    assert(mobileColumns[1].x>mobileColumns[0].x&&Math.abs(mobileColumns[1].y-mobileColumns[0].y)<1,'the two library types must remain side by side on a phone');
    const mobilePanel=await page.locator('.quote-library-panel').boundingBox();
    assert(mobilePanel.x>=0&&mobilePanel.x+mobilePanel.width<=390,'the library must remain fully inside the phone viewport');
    assert(mobileColumns.every(column => column.x>=mobilePanel.x&&column.x+column.width<=mobilePanel.x+mobilePanel.width),'both library columns must be fully visible on a phone');
    assert(await page.evaluate(() => document.documentElement.scrollWidth<=innerWidth),'the library must not cause horizontal page overflow on a phone');
    await page.screenshot({path:'.work/quote-library-columns-390.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS: quotation history/templates render as fixed two-column libraries on desktop and mobile.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
