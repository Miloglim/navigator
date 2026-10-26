const {chromium}=require('playwright');
const assert=require('assert');
const fs=require('fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    fs.mkdirSync('.work/realtime-sort',{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    await page.goto('http://127.0.0.1:8795/',{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      S.auth={user_id:'fixture',role:'sales',views:['sales']};
      S.role='sales';
      applyAccountAccess();
      $('auth-modal').hidden=true;
      switchRole('sales');
      $('sales-table-wrap').hidden=false;
      $('company-rate-results').hidden=false;
      $('sales-source-local').hidden=true;
      renderCompanyTask('realtime',{status:'complete',query:{container_id:12},results:[{fresh:true,items:[{
        carrier:{carrierCode:'MAERSK'},result:[
          {carrierName:'ZIM',pol:{terminalEnName:'SHANGHAI'},pod:{terminalEnName:'SANTOS'},etd:1792166400000,eta:1792857600000,vessel:'ZIM A',voyage:'001W',totalOceanPriceUsd:5200,totalPrice:'5200USD+0CNY',obtainDate:1792166400000},
          {carrierName:'CMA',pol:{terminalEnName:'NINGBO'},pod:{terminalEnName:'PARANAGUA'},etd:1792252800000,eta:1792944000000,vessel:'CMA B',voyage:'002W',totalOceanPriceUsd:4800,totalPrice:'4800USD+0CNY',obtainDate:1792252800000},
          {carrierName:'COSCO',pol:{terminalEnName:'QINGDAO'},pod:{terminalEnName:'ITAPOA'},etd:1792339200000,eta:1793030400000,vessel:'COSCO C',voyage:'003W',totalOceanPriceUsd:null,totalPrice:null,obtainDate:1792339200000}
        ]
      }]}]});
    });

    const carrierOrder=()=>page.locator('#company-rate-realtime .qrow td:nth-child(2)').allTextContents();
    assert.equal(await page.locator('#company-rate-realtime th[data-realtime-sort]').count(),9,'all live-rate data columns are sortable');
    assert.equal(await page.locator('#company-rate-realtime th[data-realtime-sort="ocean_price"]').getAttribute('aria-sort'),'ascending');
    assert.deepEqual(await carrierOrder(),['CMA','ZIM','COSCO'],'live rates default to lowest ocean price');

    const oceanHeader=page.locator('#company-rate-realtime th[data-realtime-sort="ocean_price"] button');
    await oceanHeader.click();
    assert.deepEqual(await carrierOrder(),['ZIM','CMA','COSCO'],'live ocean price descending keeps missing price last');
    await page.locator('#company-rate-realtime th[data-realtime-sort="ocean_price"] button').click();
    assert.deepEqual(await carrierOrder(),['CMA','ZIM','COSCO'],'second ocean price click restores low-price first');
    await page.screenshot({path:'.work/realtime-sort/desktop.png',fullPage:true});
    assert.equal(await page.locator('#company-rate-realtime th[data-realtime-sort="ocean_price"]').getAttribute('aria-sort'),'ascending');

    await page.locator('#company-rate-realtime .sales-disclosure').first().click();
    assert.equal(await page.locator('#company-rate-realtime .company-detail').count(),1,'sorting preserves detail behavior');
    await page.setViewportSize({width:390,height:844});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
    assert.equal(overflow,false,'live-rate sorting must not cause mobile horizontal overflow');
    await page.screenshot({path:'.work/realtime-sort/mobile.png',fullPage:true});
    console.log('PASS: live carrier rates sort all data headers with visible tri-state order and preserve details.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
