const {chromium}=require('playwright');
const assert=require('assert'),fs=require('fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    let sent, requestCount=0;
    await page.route('**/api/**',async route=>{
      if(route.request().url().endsWith('/api/company-rates/realtime')) {
        sent=route.request().postDataJSON(); requestCount++;
      }
      await route.fulfill({json:{ok:true,data:{task_id:'fixture',status:'complete',results:[]}}});
    });
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'fixture',role:'sales',views:['sales']}; S.role='sales'; applyAccountAccess();
      $('auth-modal').hidden=true; S.sales.polTags=[];S.sales.podTags=[];S.sales.carrier='';
      $('sf-ctype').value=''; $('realtime-etd').value=''; $('realtime-weight').value=''; syncCompanySourceControls();
    });
    await page.getByRole('button',{name:'查询船司价格',exact:true}).click();
    assert.equal(requestCount,0);
    assert.equal(await page.locator('#view-sales .filter-group.realtime-required-missing').count(),6);
    fs.mkdirSync('.work/realtime-check',{recursive:true});
    await page.screenshot({path:'.work/realtime-check/required-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'.work/realtime-check/required-mobile.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>{
      S.sales.polTags=['Shanghai'];S.sales.podTags=['Santos'];S.sales.carrier='MSK';
      $('sf-carrier').value='MSK';
      const ctype=document.querySelector('.sales-ctype-options input[value="40HQ/HC"]');
      ctype.checked=true; syncSalesCtypes(ctype);
      $('realtime-etd').value='2026-10-17'; clearRealtimeRequired($('realtime-etd'));
      $('realtime-weight').value='10000'; $('realtime-weight').dispatchEvent(new Event('input'));
      ['sf-pol','sf-pod','sf-carrier'].forEach(id=>clearRealtimeRequired($(id)));
    });
    assert.equal(await page.locator('#view-sales .filter-group.realtime-required-missing').count(),0);
    await page.getByRole('button',{name:'查询船司价格',exact:true}).click();
    await page.waitForFunction(()=>S.sales.companyTasks.realtime?.status==='complete');
    assert.equal(requestCount,1);
    assert.equal(sent.valid_from,1792166400000);
    assert.equal(sent.weight,10000);
    await page.screenshot({path:'.work/realtime-check/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'.work/realtime-check/mobile.png',fullPage:true});
    console.log('PASS: rendered ETD selection sends October 17 independently from October 14 validity.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
