const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    await page.route('**/api/**',route=>route.fulfill({json:{ok:true,data:{rows:[]}}}));
    await page.route('http://127.0.0.1:8795/',route=>route.fulfill({contentType:'text/html',body:fs.readFileSync('web/index.html','utf8')}));
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'quote-selection-fixture',role:'sales',views:['sales']};
      S.role='sales';applyAccountAccess();$('auth-modal').hidden=true;switchRole('sales');
      $('sales-table-wrap').hidden=false;$('company-rate-results').hidden=false;
      S.sales.groups=[1,2,3,4].map(index=>({current:{content_key:'local-'+index,carrier:'PIL',pol:'Qingdao',pod:'Santos',price_usd:1000+index,message_time:'2026-10-16 10:00:00'},history:[]}));
      S.sales.groups.push({current:{content_key:'local-expired',carrier:'PIL',pol:'Qingdao',pod:'Santos',expired:true},history:[]});
      renderSalesFolded();
      $('sf-ctype').value='40HQ/HC';
      renderCompanyTask('company',{status:'complete',results:[{fresh:true,items:[1,2].map(index=>({
        product_id:index,carrier_info:{carrier_code:'MAERSK'},from_terminal_info:{terminal_en_name:'SHANGHAI'},to_terminal_info:{terminal_en_name:'SANTOS'},
        product_price:[{price_unit:'40HQ',currency_code:'USD',show_price:5000+index}]
      }))}]});
      renderCompanyTask('realtime',{status:'complete',query:{container_id:12},results:[{fresh:true,items:[{result:[1,2].map(index=>({
        carrierName:'CMA',pol:{terminalEnName:'SHANGHAI'},pod:{terminalEnName:'SANTOS'},etd:1792166400000+index,
        totalOceanPriceUsd:5000+index,obtainDate:1792166400000
      }))}]}]});
    });
    const local=page.locator('#table-container .sales-rate-table');
    const rows=local.locator('tbody .qrow .quote-pick');
    assert.equal(await rows.count(),5);
    assert.equal(await local.locator('thead .quote-pick-all').count(),1,'local header offers select all');
    assert.equal(await page.locator('#company-rate-company .quote-pick-all').count(),1,'company header offers select all');
    assert.equal(await page.locator('#company-rate-realtime .quote-pick-all').count(),1,'realtime header offers select all');
    await rows.nth(0).click();
    assert.equal(await local.locator('thead .quote-pick-all').evaluate(input=>input.indeterminate),true,'partial selection is visible in the header');
    await rows.nth(3).click({modifiers:['Shift']});
    assert.deepEqual(await rows.evaluateAll(inputs=>inputs.map(input=>input.checked)),[true,true,true,true,false],'Shift-click selects the visible eligible range');
    assert.equal(await page.evaluate(()=>quoteEditorLoad().rates.rows.length),4,'range selection enters the quote draft');
    await local.locator('thead .quote-pick-all').click();
    assert.deepEqual(await rows.evaluateAll(inputs=>inputs.map(input=>input.checked)),[false,false,false,false,false],'header clears this table without selecting disabled rows');
    await local.locator('thead .quote-pick-all').click();
    assert.deepEqual(await rows.evaluateAll(inputs=>inputs.map(input=>input.checked)),[true,true,true,true,false]);
    assert.equal(await page.evaluate(()=>quoteEditorLoad().rates.rows.length),4);
    await page.locator('#company-rate-company .quote-pick-all').click();
    assert.equal(await page.locator('#company-rate-company tbody .qrow .quote-pick:checked').count(),2);
    assert.equal(await local.locator('tbody .qrow .quote-pick:checked').count(),4,'company selection does not alter local selections');
    await page.locator('#company-rate-realtime .quote-pick-all').click();
    assert.equal(await page.locator('#company-rate-realtime tbody .qrow .quote-pick:checked').count(),2);
    assert.equal(await page.evaluate(()=>quoteEditorLoad().space.rows.length),2);
    await page.setViewportSize({width:390,height:844});
    assert(await page.locator('#company-rate-realtime .quote-pick-all').isVisible(),'select all remains visible on mobile');
    await page.locator('#company-rate-realtime .quote-pick-all').click();
    assert.equal(await page.locator('#company-rate-realtime tbody .qrow .quote-pick:checked').count(),0,'mobile select all is clickable');
    fs.mkdirSync('.work',{recursive:true});
    await page.locator('#company-rate-realtime .sales-rate-table').screenshot({path:'.work/quote-selection-mobile.png'});
    await page.setViewportSize({width:1440,height:900});
    await local.screenshot({path:'.work/quote-selection-desktop.png'});
    await page.evaluate(()=>{
      setSalesSourceCollapsed('company',true);
      renderCompanyTask('company',{status:'complete',results:[{fresh:true,items:companyRateRows.map(row=>row.item)}]});
    });
    await page.evaluate(()=>setSalesSourceCollapsed('company',false));
    assert.equal(await page.locator('#company-rate-company .quote-pick-all').isEnabled(),true,'select all is available again after expanding a source');
    console.log('PASS: Shift range, per-table select all, disabled rows, draft sync, and mobile control');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
