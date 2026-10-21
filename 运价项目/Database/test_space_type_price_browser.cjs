const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**',route=>route.fulfill({json:{ok:true,data:{rows:[]}}}));
    await page.route('http://127.0.0.1:8795/',route=>route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    assert.equal(await page.evaluate(()=>realtimeQuoteSelection({quote:{},row:{item:{}},containerId:0},'unknown').containerType),'','an unknown container type must stay blank for review');
    await page.evaluate(()=>{
      S.auth={user_id:'space-type-price-browser',role:'sales',views:['sales']};
      S.role='sales';applyAccountAccess();$('auth-modal').hidden=true;
      setCatTab(document.querySelector('#cat-tabs button[data-tab="quote"]'));
      quoteEditorSelect('space',{quoteSource:'realtime',quoteKey:'first',carrier:'CMA',pol:'Ningbo',pod:'Santos',containerType:'20GP',totalOceanPriceUsd:1800},true);
      renderQuotePage();
    });
    const single=page.locator('.quote-single-editor');
    assert.deepEqual(await single.locator('tbody th > span[contenteditable]').allInnerTexts(),['CARRIER','POL','POD','TYPE','O/F','ETD','ETA','VESSEL No.','ROUTE','TT']);
    assert.equal(await single.locator('tbody tr').nth(3).locator('td').innerText(),'20GP');
    assert.equal(await single.locator('tbody tr').nth(4).locator('td').innerText(),'$1800');
    fs.mkdirSync('.work',{recursive:true});
    await single.screenshot({path:'.work/space-type-price-single.png'});
    await page.evaluate(()=>{
      quoteEditorSelect('space',{quoteSource:'realtime',quoteKey:'second',carrier:'CMA',pol:'Ningbo',pod:'Santos',containerType:'40HQ/HC',totalOceanPriceUsd:2900},true);
      renderQuotePage();
    });
    const multi=page.locator('.quote-edit-grid[data-quote-kind="space"]');
    assert.deepEqual(await multi.locator('.quote-field-row th span[contenteditable]').allInnerTexts(),['CARRIER','POL','POD','TYPE','O/F','ETD','ETA','VESSEL No.','ROUTE','TT']);
    assert.equal(await multi.locator('tbody tr').first().locator('td').nth(3).innerText(),'20GP');
    assert.equal(await multi.locator('tbody tr').first().locator('td').nth(4).innerText(),'$1800');
    const copied=await page.evaluate(()=>quotationClipboard());
    assert(copied.text.includes('POD\tTYPE\tO/F\tETD'));
    assert(copied.text.includes('20GP\t$1800'));
    assert(copied.text.includes('40HQ/HC\t$2900'));
    assert(copied.html.indexOf('>TYPE</th>')<copied.html.indexOf('>O/F</th>'));
    await multi.screenshot({path:'.work/space-type-price-multi.png'});
    await page.setViewportSize({width:390,height:844});
    assert(await multi.isVisible());
    const mobile=await multi.locator('..').evaluate(wrap=>({visibleWidth:wrap.clientWidth,tableWidth:wrap.querySelector('table').getBoundingClientRect().width,pageWidth:document.documentElement.scrollWidth}));
    assert(mobile.tableWidth>=920 && mobile.tableWidth>mobile.visibleWidth,'phone layout must preserve readable column widths inside its own scroll area');
    assert(mobile.pageWidth<=390,'quote table must not make the whole phone page scroll sideways');
    await page.locator('.quote-editor-section').screenshot({path:'.work/space-type-price-mobile.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS: single and multi space tables keep TYPE beside O/F on screen and in copied email');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
